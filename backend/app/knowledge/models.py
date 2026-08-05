from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator


SourceType = Literal["law", "firm_rule", "tenant_private"]
KnowledgeScope = Literal["public", "firm", "tenant_private"]


class KnowledgeSource(BaseModel):
    title: str = Field(max_length=500)
    source_type: SourceType
    issuing_authority: str = Field(default="", max_length=500)
    document_number: str = Field(default="", max_length=255)
    source_url: str = ""
    article_number: str | None = Field(default=None, max_length=100)
    section_title: str | None = Field(default=None, max_length=500)
    paragraph_index: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    bboxes: list[dict[str, float]] = Field(default_factory=list)
    version: str = Field(default="", max_length=255)
    effective_date: date | None = None
    expiry_date: date | None = None
    source_filename: str = ""
    source_sha256: str = ""
    chunk_sha256: str = ""

    @field_validator("title")
    @classmethod
    def title_must_not_be_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be empty")
        return value

    @field_validator("paragraph_index")
    @classmethod
    def paragraph_must_not_be_negative(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("paragraph_index must not be negative")
        return value

    @field_validator("page_start", "page_end")
    @classmethod
    def page_must_be_positive(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("page numbers must be positive")
        return value

    @field_validator("source_sha256", "chunk_sha256")
    @classmethod
    def validate_optional_sha256(cls, value: str) -> str:
        normalized = value.lower().strip()
        if normalized and (
            len(normalized) != 64
            or any(character not in "0123456789abcdef" for character in normalized)
        ):
            raise ValueError("SHA-256 values must contain 64 hexadecimal characters")
        return normalized

    @model_validator(mode="after")
    def validate_formal_law_traceability(self) -> "KnowledgeSource":
        if self.page_end is not None and self.page_start is not None:
            if self.page_end < self.page_start:
                raise ValueError("page_end must not precede page_start")
        if self.expiry_date is not None and self.effective_date is not None:
            if self.expiry_date < self.effective_date:
                raise ValueError("expiry_date must not precede effective_date")
        if self.source_url:
            parsed_url = urlparse(self.source_url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                raise ValueError("source_url must use a valid http(s) URL")
            if parsed_url.username is not None or parsed_url.password is not None:
                raise ValueError("source_url must not contain credentials")
            if parsed_url.fragment:
                raise ValueError("source_url must not contain a fragment")
        if self.source_type == "law":
            parsed_url = urlparse(self.source_url)
            if not self.issuing_authority.strip():
                raise ValueError("law requires an issuing authority")
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                raise ValueError("law requires an official http(s) source URL")
            if not self.version.strip() and self.effective_date is None:
                raise ValueError("law requires an explicit version or effective date")
        return self


@dataclass(frozen=True)
class KnowledgeChunk:
    id: str
    document_id: str
    parent_chunk_id: str | None
    ordinal: int
    content: str
    content_sha256: str
    article_number: str | None
    section_title: str | None
    page_start: int | None
    page_end: int | None
    paragraph_index: int | None
    bboxes: tuple[dict[str, float], ...]
    source_snapshot: dict[str, Any]
    keyword_search_text: str
    embedding_model: str | None = None
    embedding: tuple[float, ...] | None = None

    def with_embedding(self, model: str, values: list[float]) -> "KnowledgeChunk":
        return replace(self, embedding_model=model, embedding=tuple(values))


@dataclass(frozen=True)
class IngestionBatch:
    document_id: str
    tenant_id: str | None
    scope: KnowledgeScope
    source: KnowledgeSource
    file_sha256: str
    object_key: str
    parent_chunks: tuple[KnowledgeChunk, ...]
    child_chunks: tuple[KnowledgeChunk, ...]

    @property
    def all_chunks(self) -> tuple[KnowledgeChunk, ...]:
        return self.parent_chunks + self.child_chunks

    def children_for(self, parent_id: str) -> tuple[KnowledgeChunk, ...]:
        return tuple(
            chunk for chunk in self.child_chunks if chunk.parent_chunk_id == parent_id
        )


@dataclass(frozen=True)
class KnowledgeDocumentRecord:
    id: str
    tenant_id: str | None
    scope: KnowledgeScope
    title: str
    source_type: SourceType
    content_sha256: str
    object_key: str
    source_metadata: dict[str, Any]


@dataclass(frozen=True)
class KnowledgePersistResult:
    record: KnowledgeDocumentRecord
    inserted: bool


@dataclass(frozen=True)
class KnowledgeChunkRecord:
    id: str
    document_id: str
    parent_chunk_id: str | None
    tenant_id: str | None
    scope: KnowledgeScope
    content: str
    content_sha256: str
    source_snapshot: dict[str, Any]
