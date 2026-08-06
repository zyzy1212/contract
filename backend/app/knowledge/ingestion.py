from __future__ import annotations

import hashlib
import json
import math
import re
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol
from uuid import UUID

from app.auth import Actor, require_tenant
from app.common.errors import TenantAccessError
from app.config import get_settings
from app.documents.models import ParsedBlock, ParsedDocument
from app.documents.parser import parse_document
from app.documents.structure import extract_article_number
from app.storage.objects import ObjectStore

from .models import (
    IngestionBatch,
    KnowledgeChunk,
    KnowledgeDocumentRecord,
    KnowledgePersistResult,
    KnowledgeScope,
    KnowledgeSource,
)


EMBEDDING_DIMENSION = 512


def tokenize_keyword_text(value: str) -> str:
    """Build stable simple-dictionary lexemes plus Chinese character fallbacks."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        import jieba

    tokens: list[str] = []
    for segmented in jieba.cut(value, cut_all=False, HMM=False):
        for token in re.findall(r"[A-Za-z0-9_]+|[\u3400-\u9fff]+", segmented):
            normalized = token.lower()
            tokens.append(normalized)
            if re.fullmatch(r"[\u3400-\u9fff]+", normalized):
                tokens.extend(normalized)
    return " ".join(tokens)


def domain_keyword_search_text(
    content: str,
    *,
    article_number: str | None,
    section_title: str | None,
    source: KnowledgeSource,
) -> str:
    """Index legal metadata together with content for domain-aware keyword recall."""
    metadata = " ".join(
        part
        for part in (
            source.title,
            source.issuing_authority,
            source.document_number,
            section_title or "",
        )
        if part
    )
    raw = f"{metadata} {content}" if metadata else content
    tokens = tokenize_keyword_text(raw)
    normalized_article = re.sub(r"\s+", "", article_number or "").lower()
    if normalized_article:
        tokens = f"{tokens} {normalized_article}"
    return tokens


class DocumentParser(Protocol):
    def __call__(self, path: Path) -> ParsedDocument: ...


class EmbeddingProvider(Protocol):
    model_name: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class KnowledgeWriter(Protocol):
    async def find_existing(
        self,
        actor: Actor,
        batch: IngestionBatch,
        requested_tenant_id: str | None = None,
    ) -> KnowledgeDocumentRecord | None: ...

    async def persist(
        self,
        actor: Actor,
        batch: IngestionBatch,
        requested_tenant_id: str | None = None,
    ) -> KnowledgePersistResult: ...


def scope_for_source(source_type: str) -> KnowledgeScope:
    mapping: dict[str, KnowledgeScope] = {
        "law": "public",
        "firm_rule": "firm",
        "tenant_private": "tenant_private",
    }
    try:
        return mapping[source_type]
    except KeyError as exc:
        raise ValueError(f"unsupported knowledge source type: {source_type}") from exc


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_uuid(canonical: bytes) -> UUID:
    """Build an RFC-compatible version-8 UUID directly from SHA-256 bytes."""
    digest = bytearray(hashlib.sha256(canonical).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x80
    digest[8] = (digest[8] & 0x3F) | 0x80
    return UUID(bytes=bytes(digest))


def _canonical_identifier(*parts: object) -> UUID:
    canonical = json.dumps(
        list(parts), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return sha256_uuid(canonical)


def _object_key(
    file_sha256: str,
    scope: KnowledgeScope,
    tenant_id: str | None,
) -> str:
    if scope == "public":
        return f"public/law/{file_sha256}"
    if tenant_id is None:
        raise ValueError(f"{scope} knowledge requires a validated tenant")
    return f"tenants/{tenant_id}/{scope}/{file_sha256}"


def _locator_value(block: ParsedBlock, source: KnowledgeSource, name: str):
    block_value = getattr(block.locator, name)
    return block_value if block_value is not None else getattr(source, name)


def _canonical_bbox(bbox: dict[str, float]) -> dict[str, float]:
    if "x0" not in bbox or "x1" not in bbox:
        raise ValueError("effective locator bbox requires x0 and x1")
    if "top" in bbox and "y0" in bbox and float(bbox["top"]) != float(bbox["y0"]):
        raise ValueError("effective locator bbox has conflicting top and y0")
    if (
        "bottom" in bbox
        and "y1" in bbox
        and float(bbox["bottom"]) != float(bbox["y1"])
    ):
        raise ValueError("effective locator bbox has conflicting bottom and y1")
    if "top" not in bbox and "y0" not in bbox:
        raise ValueError("effective locator bbox requires top")
    if "bottom" not in bbox and "y1" not in bbox:
        raise ValueError("effective locator bbox requires bottom")
    canonical = {
        "x0": float(bbox["x0"]),
        "top": float(bbox["top"] if "top" in bbox else bbox["y0"]),
        "x1": float(bbox["x1"]),
        "bottom": float(bbox["bottom"] if "bottom" in bbox else bbox["y1"]),
    }
    coordinates = tuple(canonical.values())
    if not all(math.isfinite(value) for value in coordinates):
        raise ValueError("effective locator bbox coordinates must be finite")
    if any(value < 0 for value in coordinates):
        raise ValueError("effective locator bbox coordinates must not be negative")
    if canonical["x1"] < canonical["x0"]:
        raise ValueError("effective locator bbox x1 must not precede x0")
    if canonical["bottom"] < canonical["top"]:
        raise ValueError("effective locator bbox bottom must not precede top")
    return canonical


def _effective_locator(
    block: ParsedBlock, source: KnowledgeSource
) -> dict[str, object]:
    locator: dict[str, object] = {
        "article_number": _locator_value(block, source, "article_number"),
        "section_title": _locator_value(block, source, "section_title"),
        "page_start": _locator_value(block, source, "page_start"),
        "page_end": _locator_value(block, source, "page_end"),
        "paragraph_index": _locator_value(block, source, "paragraph_index"),
        "bboxes": [
            _canonical_bbox(bbox)
            for bbox in (block.locator.bboxes or source.bboxes)
        ],
    }
    page_start = locator["page_start"]
    page_end = locator["page_end"]
    paragraph_index = locator["paragraph_index"]
    if page_start is not None and page_start < 1:
        raise ValueError("effective locator page_start must be at least 1")
    if page_end is not None:
        if page_start is None:
            raise ValueError("effective locator page_end requires page_start")
        if page_end < page_start:
            raise ValueError("effective locator page_end must not precede page_start")
    if paragraph_index is not None and paragraph_index < 0:
        raise ValueError("effective locator paragraph_index must not be negative")
    return locator


def _snapshot(
    parsed: ParsedDocument,
    source: KnowledgeSource,
    spans: tuple[tuple[ParsedBlock, int, int], ...],
    content_sha256: str,
) -> dict[str, object]:
    locators = [_effective_locator(block, source) for block, _, _ in spans]
    page_starts = [
        locator["page_start"]
        for locator in locators
        if locator["page_start"] is not None
    ]
    page_ends = [
        locator["page_end"] or locator["page_start"]
        for locator in locators
        if locator["page_end"] is not None or locator["page_start"] is not None
    ]
    locator = {
        "article_number": next(
            (
                value
                for value in (item["article_number"] for item in locators)
                if value is not None
            ),
            None,
        ),
        "section_title": next(
            (
                value
                for value in (item["section_title"] for item in locators)
                if value is not None
            ),
            None,
        ),
        "page_start": min(page_starts) if page_starts else None,
        "page_end": max(page_ends) if page_ends else None,
        "paragraph_index": next(
            (
                value
                for value in (item["paragraph_index"] for item in locators)
                if value is not None
            ),
            None,
        ),
        "bboxes": [bbox for item in locators for bbox in item["bboxes"]],
    }
    return {
        "source_text_sha256": content_sha256,
        "title": source.title,
        "source_type": source.source_type,
        "issuing_authority": source.issuing_authority,
        "document_number": source.document_number,
        "source_url": source.source_url,
        **locator,
        "version": source.version,
        "effective_date": (
            source.effective_date.isoformat() if source.effective_date else None
        ),
        "expiry_date": source.expiry_date.isoformat() if source.expiry_date else None,
        "source_filename": parsed.filename,
        "file_sha256": parsed.sha256,
        "source_spans": [
            {
                "block_id": block.block_id,
                "text_sha256": _sha256(block.text),
                "character_start": character_start,
                "character_end": character_end,
                "locator": _effective_locator(block, source),
            }
            for block, character_start, character_end in spans
        ],
    }


def _raw_child_parts(text: str, max_chars: int) -> Iterator[str]:
    current = ""
    for match in re.finditer(r".*?(?:\n|$)", text):
        paragraph = match.group(0)
        if not paragraph:
            continue
        if current and len(current) + len(paragraph) > max_chars:
            yield current
            current = ""
        while len(paragraph) > max_chars:
            available = max_chars - len(current)
            if available:
                current += paragraph[:available]
                paragraph = paragraph[available:]
                yield current
                current = ""
            else:
                yield paragraph[:max_chars]
                paragraph = paragraph[max_chars:]
        current += paragraph
    if current:
        yield current


def _is_orphan_article_header(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped and extract_article_number(stripped) == stripped)


def _block_article_number(block: ParsedBlock) -> str | None:
    return block.locator.article_number or extract_article_number(block.text)


def _coalesced_block_groups(
    blocks: list[ParsedBlock],
) -> list[tuple[ParsedBlock, ...]]:
    groups: list[tuple[ParsedBlock, ...]] = []
    index = 0
    while index < len(blocks):
        group = [blocks[index]]
        article_number = _block_article_number(blocks[index])
        if _is_orphan_article_header(blocks[index].text) and article_number:
            following = index + 1
            while (
                following < len(blocks)
                and _block_article_number(blocks[following]) == article_number
            ):
                group.append(blocks[following])
                following += 1
            if all(_is_orphan_article_header(block.text) for block in group):
                raise ValueError("article header block requires a following body block")
            index = following
        else:
            index += 1
        groups.append(tuple(group))
    return groups


def _child_parts(text: str, max_chars: int) -> Iterator[str]:
    if max_chars <= 0:
        raise ValueError("child_max_chars must be positive")
    if not text:
        raise ValueError("parsed blocks must not contain empty text")
    leading_whitespace = len(text) - len(text.lstrip())
    article_number = extract_article_number(text)
    protected_end = 0
    if article_number:
        protected_end = leading_whitespace + len(article_number)
        while protected_end < len(text) and text[protected_end].isspace():
            protected_end += 1
    protected = text[:protected_end]
    raw_parts = iter(_raw_child_parts(text[protected_end:], max_chars))

    def protected_parts() -> Iterator[str]:
        if protected:
            yield protected
        yield from raw_parts

    parts = iter(protected_parts())
    try:
        pending = next(parts)
    except StopIteration as exc:
        raise RuntimeError("child chunking produced no source text") from exc
    source_offset = 0
    while True:
        if _is_orphan_article_header(pending):
            try:
                pending += next(parts)
            except StopIteration:
                pass
        child_end = source_offset + len(pending)
        if text[source_offset:child_end] != pending:
            raise RuntimeError("child chunking did not preserve exact source text")
        source_offset = child_end
        yield pending
        try:
            pending = next(parts)
        except StopIteration:
            break
    if source_offset != len(text):
        raise RuntimeError("child chunking did not preserve exact source text")


def build_chunk_plan(
    parsed: ParsedDocument,
    source: KnowledgeSource,
    *,
    child_max_chars: int = 600,
    max_child_chunks: int = 5_000,
    max_total_embedding_elements: int = 5_120_000,
    tenant_id: str | None = None,
) -> IngestionBatch:
    if not parsed.blocks:
        raise ValueError("parsed knowledge document must contain at least one block")
    if len(parsed.sha256) != 64 or any(
        character not in "0123456789abcdef" for character in parsed.sha256.lower()
    ):
        raise ValueError("parser SHA-256 must contain 64 hexadecimal characters")
    if source.source_filename and source.source_filename != parsed.filename:
        raise ValueError("source filename does not match parsed source")
    if source.source_sha256 and source.source_sha256 != parsed.sha256:
        raise ValueError("source SHA-256 does not match parsed source")
    if max_child_chunks <= 0:
        raise ValueError("max_child_chunks must be positive")
    if max_total_embedding_elements <= 0:
        raise ValueError("max_total_embedding_elements must be positive")
    scope = scope_for_source(source.source_type)
    if scope == "public":
        tenant_id = None
    elif tenant_id is None:
        raise ValueError(f"{scope} knowledge requires a validated tenant")
    document_id = str(
        _canonical_identifier(
            "knowledge-document",
            scope,
            tenant_id or "public",
            parsed.sha256,
            source.version or "",
        )
    )
    parents: list[KnowledgeChunk] = []
    children: list[KnowledgeChunk] = []
    ordinal = 0
    for block_index, blocks in enumerate(_coalesced_block_groups(parsed.blocks)):
        block = blocks[0]
        parent_text = "".join(original.text for original in blocks)
        parent_spans = tuple((original, 0, len(original.text)) for original in blocks)
        block_ranges: list[tuple[ParsedBlock, int, int]] = []
        block_offset = 0
        for original in blocks:
            block_ranges.append(
                (original, block_offset, block_offset + len(original.text))
            )
            block_offset += len(original.text)
        content_sha256 = _sha256(parent_text)
        block_identity: object = (
            block.block_id
            if len(blocks) == 1
            else tuple(original.block_id for original in blocks)
        )
        parent_id = str(
            _canonical_identifier(
                "knowledge-parent",
                document_id,
                block_index,
                block_identity,
                content_sha256,
            )
        )
        snapshot = _snapshot(parsed, source, parent_spans, content_sha256)
        bboxes = tuple(dict(bbox) for bbox in snapshot["bboxes"])
        parent = KnowledgeChunk(
            id=parent_id,
            document_id=document_id,
            parent_chunk_id=None,
            ordinal=ordinal,
            content=parent_text,
            content_sha256=content_sha256,
            article_number=snapshot["article_number"],
            section_title=snapshot["section_title"],
            page_start=snapshot["page_start"],
            page_end=snapshot["page_end"],
            paragraph_index=snapshot["paragraph_index"],
            bboxes=bboxes,
            source_snapshot=snapshot,
            keyword_search_text=domain_keyword_search_text(
                parent_text,
                article_number=snapshot["article_number"],
                section_title=snapshot["section_title"],
                source=source,
            ),
        )
        parents.append(parent)
        ordinal += 1
        child_offset = 0
        for child_index, child_text in enumerate(
            _child_parts(parent_text, child_max_chars)
        ):
            next_child_count = len(children) + 1
            if next_child_count > max_child_chunks:
                raise ValueError("document exceeds maximum child chunks")
            if (
                next_child_count * EMBEDDING_DIMENSION
                > max_total_embedding_elements
            ):
                raise ValueError("document exceeds maximum total embedding elements")
            child_end = child_offset + len(child_text)
            child_spans = tuple(
                (
                    original,
                    max(child_offset, block_start) - block_start,
                    min(child_end, block_end) - block_start,
                )
                for original, block_start, block_end in block_ranges
                if block_start < child_end and block_end > child_offset
            )
            child_sha256 = _sha256(child_text)
            child_snapshot = _snapshot(parsed, source, child_spans, child_sha256)
            child_id = str(
                _canonical_identifier(
                    "knowledge-child",
                    document_id,
                    parent_id,
                    child_index,
                    child_sha256,
                )
            )
            children.append(
                KnowledgeChunk(
                    id=child_id,
                    document_id=document_id,
                    parent_chunk_id=parent_id,
                    ordinal=ordinal,
                    content=child_text,
                    content_sha256=child_sha256,
                    article_number=child_snapshot["article_number"],
                    section_title=child_snapshot["section_title"],
                    page_start=child_snapshot["page_start"],
                    page_end=child_snapshot["page_end"],
                    paragraph_index=child_snapshot["paragraph_index"],
                    bboxes=tuple(dict(bbox) for bbox in child_snapshot["bboxes"]),
                    source_snapshot=child_snapshot,
                    keyword_search_text=domain_keyword_search_text(
                        child_text,
                        article_number=child_snapshot["article_number"],
                        section_title=child_snapshot["section_title"],
                        source=source,
                    ),
                )
            )
            ordinal += 1
            child_offset = child_end
    return IngestionBatch(
        document_id=document_id,
        tenant_id=tenant_id,
        scope=scope,
        source=source,
        file_sha256=parsed.sha256,
        object_key=_object_key(parsed.sha256, scope, tenant_id),
        parent_chunks=tuple(parents),
        child_chunks=tuple(children),
    )


async def prepare_ingestion(
    parsed: ParsedDocument,
    source: KnowledgeSource,
    embedding_provider: EmbeddingProvider,
    *,
    child_max_chars: int = 600,
    embedding_batch_size: int = 128,
    max_child_chunks: int = 5_000,
    max_total_embedding_elements: int = 5_120_000,
    tenant_id: str | None = None,
) -> IngestionBatch:
    if embedding_batch_size <= 0:
        raise ValueError("embedding_batch_size must be positive")
    if max_child_chunks <= 0:
        raise ValueError("max_child_chunks must be positive")
    if max_total_embedding_elements <= 0:
        raise ValueError("max_total_embedding_elements must be positive")
    model_name = embedding_provider.model_name.strip()
    if not model_name:
        raise ValueError("embedding provider model_name must not be empty")
    if len(model_name) > 255:
        raise ValueError("embedding provider model_name must not exceed 255 characters")
    plan = build_chunk_plan(
        parsed,
        source,
        child_max_chars=child_max_chars,
        max_child_chunks=max_child_chunks,
        max_total_embedding_elements=max_total_embedding_elements,
        tenant_id=tenant_id,
    )
    if len(plan.child_chunks) > max_child_chunks:
        raise ValueError("document exceeds maximum child chunks")
    if len(plan.child_chunks) * EMBEDDING_DIMENSION > max_total_embedding_elements:
        raise ValueError("document exceeds maximum total embedding elements")
    embedded_children: list[KnowledgeChunk] = []
    embedded_elements = 0
    for start in range(0, len(plan.child_chunks), embedding_batch_size):
        pending = plan.child_chunks[start : start + embedding_batch_size]
        vectors = await embedding_provider.embed([chunk.content for chunk in pending])
        if len(vectors) != len(pending):
            raise ValueError("embedding provider returned an invalid vector count")
        for chunk, vector in zip(pending, vectors, strict=True):
            if len(vector) != EMBEDDING_DIMENSION:
                raise ValueError(
                    f"embedding vectors must have exactly {EMBEDDING_DIMENSION} dimensions"
                )
            if not all(math.isfinite(float(value)) for value in vector):
                raise ValueError("embedding vectors must contain only finite values")
            embedded_elements += len(vector)
            if embedded_elements > max_total_embedding_elements:
                raise ValueError("document exceeds maximum total embedding elements")
            embedded_children.append(
                chunk.with_embedding(model_name, [float(value) for value in vector])
            )
    return IngestionBatch(
        document_id=plan.document_id,
        tenant_id=plan.tenant_id,
        scope=plan.scope,
        source=plan.source,
        file_sha256=plan.file_sha256,
        object_key=plan.object_key,
        parent_chunks=plan.parent_chunks,
        child_chunks=tuple(embedded_children),
    )


class KnowledgeIngestionService:
    def __init__(
        self,
        *,
        embedding_provider: EmbeddingProvider,
        repository: KnowledgeWriter,
        object_store: ObjectStore,
        parser: DocumentParser = parse_document,
        child_max_chars: int = 600,
        embedding_batch_size: int | None = None,
        max_child_chunks: int | None = None,
        max_total_embedding_elements: int | None = None,
    ):
        settings = get_settings()
        self.parser = parser
        self.embedding_provider = embedding_provider
        self.repository = repository
        self.object_store = object_store
        self.child_max_chars = child_max_chars
        self.embedding_batch_size = (
            embedding_batch_size
            if embedding_batch_size is not None
            else settings.embedding_batch_size
        )
        self.max_child_chunks = (
            settings.knowledge_max_child_chunks
            if max_child_chunks is None
            else max_child_chunks
        )
        self.max_total_embedding_elements = (
            settings.knowledge_max_total_embedding_elements
            if max_total_embedding_elements is None
            else max_total_embedding_elements
        )

    async def ingest(
        self,
        path: Path,
        source: KnowledgeSource,
        actor: Actor,
        requested_tenant_id: str | None = None,
    ) -> KnowledgeDocumentRecord:
        target_tenant = require_tenant(actor, requested_tenant_id)
        if source.source_type == "law" and actor.role != "admin":
            raise TenantAccessError(
                "public knowledge writes require an administrator"
            )
        parsed = self.parser(Path(path))
        original = Path(path).read_bytes()
        actual_sha256 = hashlib.sha256(original).hexdigest()
        if actual_sha256 != parsed.sha256:
            raise ValueError("parser SHA-256 does not match the original file")
        tenant_id = None if source.source_type == "law" else target_tenant
        unembedded = build_chunk_plan(
            parsed,
            source,
            child_max_chars=self.child_max_chars,
            max_child_chunks=self.max_child_chunks,
            max_total_embedding_elements=self.max_total_embedding_elements,
            tenant_id=tenant_id,
        )
        existing = await self.repository.find_existing(
            actor, unembedded, requested_tenant_id=requested_tenant_id
        )
        if existing is not None:
            return existing
        await self.object_store.put(unembedded.object_key, original)
        batch = await prepare_ingestion(
            parsed,
            source,
            self.embedding_provider,
            child_max_chars=self.child_max_chars,
            embedding_batch_size=self.embedding_batch_size,
            max_child_chunks=self.max_child_chunks,
            max_total_embedding_elements=self.max_total_embedding_elements,
            tenant_id=tenant_id,
        )
        persisted = await self.repository.persist(
            actor, batch, requested_tenant_id=requested_tenant_id
        )
        return persisted.record
