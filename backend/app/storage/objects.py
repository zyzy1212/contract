from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ObjectPutResult:
    """Result of immutable publication; ownership never implies reference exclusivity."""

    key: str
    created: bool
    ownership_token: str | None


class ObjectStore(Protocol):
    """Immutable object publication.

    Published content-addressed objects may be adopted concurrently. Immediate deletion is
    therefore fail-closed; only a future reference-aware collector may remove them.
    """

    async def put(self, key: str, data: bytes) -> ObjectPutResult: ...

    async def get(self, key: str) -> bytes: ...

    async def delete_if_owned(self, key: str, ownership_token: str | None) -> bool: ...

    async def open_url(self, key: str, *, expires_seconds: int = 300) -> str: ...


def normalize_object_key(key: str) -> str:
    if not key or "\\" in key:
        raise ValueError("object key must be a non-empty POSIX path")
    path = PurePosixPath(key)
    parts = path.parts
    if path.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("object key contains an unsafe path component")
    if not parts or parts[0] not in {"public", "tenants"}:
        raise ValueError("object key must be public or tenant/scope prefixed")
    if parts[0] == "tenants" and len(parts) < 4:
        raise ValueError("tenant object key must include tenant and scope prefixes")
    return path.as_posix()


class LocalObjectStore:
    def __init__(self, root: Path, *, max_read_bytes: int = 25 * 1024 * 1024):
        if max_read_bytes <= 0:
            raise ValueError("max_read_bytes must be positive")
        configured_root = Path(root).absolute()
        configured_root.mkdir(parents=True, exist_ok=True)
        if self._is_link_or_reparse(configured_root):
            raise ValueError("local object storage root must not be a symlink or reparse point")
        self.root = configured_root.resolve(strict=True)
        self.max_read_bytes = max_read_bytes

    @staticmethod
    def _is_link_or_reparse(path: Path) -> bool:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        try:
            attributes = getattr(os.lstat(path), "st_file_attributes", 0)
        except FileNotFoundError:
            return False
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))

    def _ensure_inside_root(self, path: Path) -> None:
        try:
            path.resolve(strict=True).relative_to(self.root)
        except ValueError as exc:
            raise ValueError("object key escapes storage root") from exc

    def _path(self, key: str, *, create_parents: bool = False) -> tuple[str, Path]:
        normalized = normalize_object_key(key)
        if self._is_link_or_reparse(self.root) or not self.root.is_dir():
            raise ValueError("local object storage root became a symlink or reparse point")
        parts = PurePosixPath(normalized).parts
        current = self.root
        missing: list[Path] = []
        for part in parts[:-1]:
            current = current / part
            if os.path.lexists(current):
                if self._is_link_or_reparse(current):
                    raise ValueError("object key ancestor is a symlink or reparse point")
                if not current.is_dir():
                    raise ValueError("object key ancestor is not a directory")
                self._ensure_inside_root(current)
            else:
                missing.append(current)
        if create_parents:
            for directory in missing:
                try:
                    directory.mkdir()
                except FileExistsError:
                    pass
                if self._is_link_or_reparse(directory):
                    raise ValueError("object key ancestor is a symlink or reparse point")
                if not directory.is_dir():
                    raise ValueError("object key ancestor is not a directory")
                self._ensure_inside_root(directory)
        target = self.root.joinpath(*parts)
        if os.path.lexists(target) and self._is_link_or_reparse(target):
            raise ValueError("object key resolves to a symlink or reparse point")
        return normalized, target

    def _existing_matches(self, target: Path, data: bytes) -> bool:
        if not target.is_file():
            return False
        size = target.stat().st_size
        if size > self.max_read_bytes:
            raise ValueError("object exceeds maximum bounded duplicate comparison size")
        if size != len(data):
            return False
        offset = 0
        with target.open("rb") as stream:
            while offset < len(data):
                chunk = stream.read(min(64 * 1024, len(data) - offset))
                if not chunk or chunk != data[offset : offset + len(chunk)]:
                    return False
                offset += len(chunk)
            return stream.read(1) == b""

    async def put(self, key: str, data: bytes) -> ObjectPutResult:
        if not isinstance(data, bytes):
            raise TypeError("object data must be bytes")
        if len(data) > self.max_read_bytes:
            raise ValueError("object exceeds maximum local object size")
        normalized, target = self._path(key, create_parents=True)
        if target.exists():
            if self._existing_matches(target, data):
                return ObjectPutResult(normalized, False, None)
            raise FileExistsError(f"object key already contains different data: {normalized}")

        token = secrets.token_urlsafe(24)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                if self._is_link_or_reparse(target):
                    raise ValueError("object key resolves to a symlink or reparse point")
                if self._existing_matches(target, data):
                    return ObjectPutResult(normalized, False, None)
                raise FileExistsError(
                    f"object key already contains different data: {normalized}"
                )
            return ObjectPutResult(normalized, True, token)
        finally:
            temporary.unlink(missing_ok=True)

    async def get(self, key: str) -> bytes:
        _, target = self._path(key)
        size = target.stat().st_size
        if size > self.max_read_bytes:
            raise ValueError("object exceeds maximum bounded read size")
        with target.open("rb") as stream:
            data = stream.read(self.max_read_bytes + 1)
        if len(data) > self.max_read_bytes:
            raise ValueError("object exceeds maximum bounded read size")
        return data

    async def delete_if_owned(self, key: str, ownership_token: str | None) -> bool:
        del ownership_token
        self._path(key)
        return False

    async def open_url(self, key: str, *, expires_seconds: int = 300) -> str:
        del expires_seconds
        _, target = self._path(key)
        if not target.is_file():
            raise FileNotFoundError(target)
        return target.as_uri()


class S3ObjectStore:
    """Private immutable S3-compatible content-addressed storage."""

    def __init__(
        self,
        bucket: str,
        *,
        client=None,
        max_read_bytes: int = 25 * 1024 * 1024,
    ):
        if not bucket.strip():
            raise ValueError("bucket must not be empty")
        if max_read_bytes <= 0:
            raise ValueError("max_read_bytes must be positive")
        if client is None:
            import boto3

            client = boto3.client("s3")
        self.bucket = bucket
        self.client = client
        self.max_read_bytes = max_read_bytes

    def _head(self, key: str):
        from botocore.exceptions import ClientError

        try:
            return self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {
                "404",
                "NoSuchKey",
                "NotFound",
            }:
                return None
            raise

    async def put(self, key: str, data: bytes) -> ObjectPutResult:
        normalized = normalize_object_key(key)
        digest = hashlib.sha256(data).hexdigest()
        from botocore.exceptions import ClientError

        try:
            response = self.client.put_object(
                Bucket=self.bucket,
                Key=normalized,
                Body=data,
                IfNoneMatch="*",
                Metadata={"content-sha256": digest},
            )
        except ClientError as exc:
            if not self._is_precondition_failure(exc):
                raise
            existing = self._head(normalized)
            if (
                existing is not None
                and existing.get("Metadata", {}).get("content-sha256") == digest
            ):
                return ObjectPutResult(normalized, False, None)
            raise FileExistsError(
                f"object key already contains different data: {normalized}"
            ) from exc
        etag = response.get("ETag")
        version_id = response.get("VersionId")
        if not etag and not version_id:
            raise RuntimeError(
                "conditional object create returned no ETag or version ownership"
            )
        ownership = json.dumps(
            {"etag": etag, "version_id": version_id},
            separators=(",", ":"),
            sort_keys=True,
        )
        return ObjectPutResult(normalized, True, ownership)

    async def get(self, key: str) -> bytes:
        normalized = normalize_object_key(key)
        response = self.client.get_object(Bucket=self.bucket, Key=normalized)
        length = int(response.get("ContentLength", 0))
        if length > self.max_read_bytes:
            raise ValueError("object exceeds maximum bounded read size")
        data = response["Body"].read(self.max_read_bytes + 1)
        if len(data) > self.max_read_bytes:
            raise ValueError("object exceeds maximum bounded read size")
        return data

    async def delete_if_owned(self, key: str, ownership_token: str | None) -> bool:
        del ownership_token
        normalize_object_key(key)
        return False

    @staticmethod
    def _is_precondition_failure(error) -> bool:
        return str(error.response.get("Error", {}).get("Code")) in {
            "409",
            "412",
            "ConditionalRequestConflict",
            "PreconditionFailed",
        }

    async def open_url(self, key: str, *, expires_seconds: int = 300) -> str:
        normalized = normalize_object_key(key)
        if expires_seconds <= 0 or expires_seconds > 3600:
            raise ValueError("expires_seconds must be between 1 and 3600")
        url = self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": normalized},
            ExpiresIn=expires_seconds,
        )
        parsed = urlsplit(url)
        if parsed.username or parsed.password:
            raise ValueError("signed object URL must not contain credentials")
        credentials = getattr(
            getattr(self.client, "_request_signer", None), "_credentials", None
        )
        secret = getattr(credentials, "secret_key", None)
        if secret and secret in url:
            raise ValueError("signed object URL exposed a secret credential")
        return url
