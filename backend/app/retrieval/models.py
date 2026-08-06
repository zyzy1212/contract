from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from math import isfinite
from types import MappingProxyType
from typing import Any, Literal, Mapping

from app.common.errors import InfrastructureError


def _immutable_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _immutable_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_immutable_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_immutable_value(item) for item in value)
    return value


def _immutable_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return _immutable_value(value)


@dataclass(frozen=True)
class EvidenceCandidate:
    chunk_id: str
    text: str
    source_snapshot_id: str
    score: float
    rank: int
    document_id: str | None = None
    source_text_sha256: str | None = None
    source_snapshot: Mapping[str, Any] = field(default_factory=dict)
    channel_scores: Mapping[str, float] = field(default_factory=dict)
    channel_ranks: Mapping[str, int] = field(default_factory=dict)
    retrieval_trace: RetrievalTrace | None = None
    knowledge_scope: Literal["public", "firm", "tenant_private"] | None = None
    tenant_id: str | None = None
    applicable_tenant_id: str | None = None
    as_of: date | None = None

    def __post_init__(self) -> None:
        if not self.chunk_id.strip():
            raise ValueError("chunk_id must not be empty")
        if not self.text.strip():
            raise ValueError("text must not be empty")
        if not self.source_snapshot_id.strip():
            raise ValueError("source_snapshot_id must not be empty")
        if self.document_id is not None and not self.document_id.strip():
            raise ValueError("document_id must not be empty")
        if self.source_text_sha256 is not None and not self.source_text_sha256.strip():
            raise ValueError("source_text_sha256 must not be empty")
        if self.knowledge_scope is None:
            if any(
                value is not None
                for value in (self.tenant_id, self.applicable_tenant_id, self.as_of)
            ):
                raise ValueError("scope is required with tenant or as_of context")
        else:
            if self.knowledge_scope not in {"public", "firm", "tenant_private"}:
                raise ValueError("knowledge_scope is invalid")
            if (
                self.applicable_tenant_id is None
                or not self.applicable_tenant_id.strip()
            ):
                raise ValueError("applicable tenant must not be empty")
            if not isinstance(self.as_of, date):
                raise ValueError("as_of must be a retrieval date")
            if self.knowledge_scope == "public":
                if self.tenant_id is not None:
                    raise ValueError("public scope must not have an owning tenant")
            elif (
                self.tenant_id is None
                or not self.tenant_id.strip()
                or self.tenant_id != self.applicable_tenant_id
            ):
                raise ValueError(
                    "private scope tenant must match the applicable tenant"
                )
        if not isfinite(self.score):
            raise ValueError("score must be finite")
        if self.rank < 1:
            raise ValueError("rank must be positive")
        for channel, score in self.channel_scores.items():
            if not channel.strip() or not isfinite(score):
                raise ValueError("channel scores require nonempty names and finite values")
        for channel, rank in self.channel_ranks.items():
            if not channel.strip() or rank < 1:
                raise ValueError("channel ranks require nonempty names and positive values")
        object.__setattr__(self, "source_snapshot", _immutable_mapping(self.source_snapshot))
        object.__setattr__(self, "channel_scores", _immutable_mapping(self.channel_scores))
        object.__setattr__(self, "channel_ranks", _immutable_mapping(self.channel_ranks))


@dataclass(frozen=True)
class RetrievalChannelFailure:
    channel: str
    error_code: str = "transient_retrieval_failure"

    def __post_init__(self) -> None:
        if not self.channel.strip() or not self.error_code.strip():
            raise ValueError("retrieval failure fields must not be empty")


@dataclass(frozen=True)
class RetrievalChannels:
    vector: tuple[EvidenceCandidate, ...] = ()
    keyword: tuple[EvidenceCandidate, ...] = ()
    reference: tuple[EvidenceCandidate, ...] = ()
    failures: tuple[RetrievalChannelFailure, ...] = ()


@dataclass(frozen=True)
class RetrievalHitTrace:
    chunk_id: str
    rank: int
    score: float
    knowledge_scope: Literal["public", "firm", "tenant_private"]
    tenant_id: str | None
    applicable_tenant_id: str
    as_of: date


@dataclass(frozen=True)
class RetrievalChannelTrace:
    name: str
    hits: tuple[RetrievalHitTrace, ...]


@dataclass(frozen=True)
class RetrievalTrace:
    query: str
    excluded_chunk_ids: tuple[str, ...]
    channels: tuple[RetrievalChannelTrace, ...]
    failures: tuple[RetrievalChannelFailure, ...]
    embedding_model: str
    validated_tenant_id: str
    as_of: date
    reranker_model: str | None = None


@dataclass(frozen=True)
class HybridSearchResult:
    candidates: tuple[EvidenceCandidate, ...]
    trace: RetrievalTrace


class TransientRetrievalError(InfrastructureError):
    """A bounded retrieval channel failed for a retryable infrastructure reason."""

    def __init__(self, message: str, *, channel: str | None = None) -> None:
        super().__init__(message)
        self.channel = channel
