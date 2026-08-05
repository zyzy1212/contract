import hashlib
from pathlib import Path


_HASH_CHUNK_BYTES = 1024 * 1024


def validate_and_hash_source(path: Path, max_source_bytes: int) -> str:
    if not path.exists():
        raise FileNotFoundError(f"document does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"document path is not a file: {path}")
    try:
        source_size = path.stat().st_size
        if source_size > max_source_bytes:
            raise ValueError(
                f"document exceeds maximum source size of {max_source_bytes} bytes: {path}"
            )

        digest = hashlib.sha256()
        bytes_read = 0
        with path.open("rb") as source:
            while chunk := source.read(_HASH_CHUNK_BYTES):
                bytes_read += len(chunk)
                if bytes_read > max_source_bytes:
                    raise ValueError(
                        f"document exceeds maximum source size of {max_source_bytes} bytes: {path}"
                    )
                digest.update(chunk)
        return digest.hexdigest()
    except PermissionError as exc:
        raise PermissionError(f"document is not readable: {path}") from exc
