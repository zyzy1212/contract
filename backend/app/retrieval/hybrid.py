from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from typing import Protocol
from uuid import UUID

from app.auth import Actor, require_tenant
from app.common.errors import InputValidationError
from app.documents.structure import article_number_references
from app.knowledge.ingestion import tokenize_keyword_text

from .models import (
    EvidenceCandidate,
    HybridSearchResult,
    RetrievalChannelFailure,
    RetrievalChannels,
    RetrievalChannelTrace,
    RetrievalHitTrace,
    RetrievalTrace,
    TransientRetrievalError,
)
from .reranker import (
    BoundedRerankerExecutor,
    Reranker,
    RerankerCapacityError,
    rerank_with_validation,
)


MAX_RRF_TOP_K = 1000
MAX_QUERY_LENGTH = 4096
MAX_EXCLUDED_CHUNK_IDS = 1000


class QueryEmbedder(Protocol):
    model_name: str
    dimension: int

    async def embed_query(self, query: str) -> Sequence[float]: ...


class HybridCandidateRepository(Protocol):
    async def retrieve_hybrid_candidates(
        self,
        actor: Actor,
        *,
        query_embedding: list[float] | None,
        embedding_model: str | None,
        keyword_search_text: str,
        excluded_chunk_ids: list[str],
        limit: int,
        embedding_dimension: int,
        timeout_ms: int,
        as_of: date,
        article_numbers: Sequence[str] = (),
    ) -> RetrievalChannels: ...


def _same_evidence(left: EvidenceCandidate, right: EvidenceCandidate) -> bool:
    return (
        left.chunk_id,
        left.document_id,
        left.text,
        left.source_snapshot_id,
        left.source_text_sha256,
        left.source_snapshot,
        left.knowledge_scope,
        left.tenant_id,
        left.applicable_tenant_id,
        left.as_of,
    ) == (
        right.chunk_id,
        right.document_id,
        right.text,
        right.source_snapshot_id,
        right.source_text_sha256,
        right.source_snapshot,
        right.knowledge_scope,
        right.tenant_id,
        right.applicable_tenant_id,
        right.as_of,
    )


def reciprocal_rank_fusion(
    channels: Sequence[Sequence[EvidenceCandidate]],
    *,
    rrf_k: int = 60,
    top_k: int | None = None,
    channel_names: Sequence[str] | None = None,
) -> list[EvidenceCandidate]:
    if isinstance(rrf_k, bool) or not isinstance(rrf_k, int) or rrf_k < 1:
        raise ValueError("rrf_k must be a positive integer")
    if top_k is not None and (
        isinstance(top_k, bool)
        or not isinstance(top_k, int)
        or top_k < 1
        or top_k > MAX_RRF_TOP_K
    ):
        raise ValueError(f"top_k must be between 1 and {MAX_RRF_TOP_K}")
    names = (
        tuple(channel_names)
        if channel_names is not None
        else tuple(f"channel_{index}" for index in range(len(channels)))
    )
    if len(names) != len(channels):
        raise ValueError("channel_names must match the number of channels")
    if any(not name.strip() for name in names) or len(set(names)) != len(names):
        raise ValueError("channel_names must be nonempty and unique")

    scores: dict[str, float] = {}
    canonical: dict[str, EvidenceCandidate] = {}
    channel_ranks: dict[str, dict[str, int]] = {}
    channel_scores: dict[str, dict[str, float]] = {}
    for channel, candidates in zip(names, channels, strict=True):
        deduplicated: dict[str, EvidenceCandidate] = {}
        for candidate in candidates:
            existing = canonical.get(candidate.chunk_id)
            if existing is not None and not _same_evidence(existing, candidate):
                raise ValueError(
                    f"inconsistent evidence records for chunk_id {candidate.chunk_id}"
                )
            canonical.setdefault(candidate.chunk_id, candidate)
            current = deduplicated.get(candidate.chunk_id)
            if current is None or (candidate.rank, -candidate.score) < (
                current.rank,
                -current.score,
            ):
                deduplicated[candidate.chunk_id] = candidate
        for candidate in deduplicated.values():
            scores[candidate.chunk_id] = scores.get(candidate.chunk_id, 0.0) + 1 / (
                rrf_k + candidate.rank
            )
            channel_ranks.setdefault(candidate.chunk_id, {})[channel] = candidate.rank
            channel_scores.setdefault(candidate.chunk_id, {})[channel] = candidate.score
    ordered = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))
    if top_k is not None:
        ordered = ordered[:top_k]
    return [
        replace(
            canonical[chunk_id],
            score=scores[chunk_id],
            rank=index,
            channel_ranks=channel_ranks[chunk_id],
            channel_scores=channel_scores[chunk_id],
        )
        for index, chunk_id in enumerate(ordered, start=1)
    ]


class HybridRetriever:
    def __init__(
        self,
        *,
        repository: HybridCandidateRepository,
        embedder: QueryEmbedder,
        reranker: Reranker | None = None,
        top_k: int = 10,
        channel_candidate_pool: int = 50,
        rrf_k: int = 60,
        timeout_ms: int = 10_000,
        embedding_timeout_ms: int | None = None,
        channel_timeout_ms: int | None = None,
        rerank_timeout_ms: int | None = None,
    ) -> None:
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 100:
            raise ValueError("top_k must be between 1 and 100")
        if (
            isinstance(channel_candidate_pool, bool)
            or not isinstance(channel_candidate_pool, int)
            or not 1 <= channel_candidate_pool <= 200
        ):
            raise ValueError("channel_candidate_pool must be between 1 and 200")
        if isinstance(rrf_k, bool) or not isinstance(rrf_k, int) or not 1 <= rrf_k <= 10_000:
            raise ValueError("rrf_k must be between 1 and 10000")
        if (
            isinstance(timeout_ms, bool)
            or not isinstance(timeout_ms, int)
            or not 1 <= timeout_ms <= 60_000
        ):
            raise ValueError("timeout_ms must be between 1 and 60000")
        default_stage_timeout = max(1, timeout_ms // 5)
        stage_timeouts = {
            "embedding_timeout_ms": (
                default_stage_timeout
                if embedding_timeout_ms is None
                else embedding_timeout_ms
            ),
            "channel_timeout_ms": (
                default_stage_timeout
                if channel_timeout_ms is None
                else channel_timeout_ms
            ),
            "rerank_timeout_ms": (
                default_stage_timeout
                if rerank_timeout_ms is None
                else rerank_timeout_ms
            ),
        }
        for name, value in stage_timeouts.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= 30_000
            ):
                raise ValueError(f"{name} must be between 1 and 30000")
        required_budget = (
            stage_timeouts["embedding_timeout_ms"]
            + 2 * stage_timeouts["channel_timeout_ms"]
            + (
                stage_timeouts["rerank_timeout_ms"]
                if reranker is not None
                else 0
            )
        )
        if required_budget > timeout_ms:
            raise ValueError(
                "total timeout must cover embedding, both channels, and reranking"
            )
        model_name = getattr(embedder, "model_name", "")
        dimension = getattr(embedder, "dimension", 0)
        if not isinstance(model_name, str) or not model_name.strip() or len(model_name) > 255:
            raise ValueError("embedder model_name must be between 1 and 255 characters")
        if (
            isinstance(dimension, bool)
            or not isinstance(dimension, int)
            or not 1 <= dimension <= 4096
        ):
            raise ValueError("embedder dimension must be between 1 and 4096")
        self.repository = repository
        self.embedder = embedder
        self.reranker = reranker
        self.top_k = top_k
        self.channel_candidate_pool = channel_candidate_pool
        self.rrf_k = rrf_k
        self.timeout_ms = timeout_ms
        self.embedding_timeout_ms = stage_timeouts["embedding_timeout_ms"]
        self.channel_timeout_ms = stage_timeouts["channel_timeout_ms"]
        self.rerank_timeout_ms = stage_timeouts["rerank_timeout_ms"]
        self._reranker_executor = (
            BoundedRerankerExecutor() if reranker is not None else None
        )

    async def search(
        self,
        actor: Actor,
        query: str,
        excluded_chunk_ids: Sequence[str] = (),
    ) -> list[EvidenceCandidate]:
        result = await self.search_with_trace(actor, query, excluded_chunk_ids)
        return list(result.candidates)

    async def search_with_trace(
        self,
        actor: Actor,
        query: str,
        excluded_chunk_ids: Sequence[str] = (),
    ) -> HybridSearchResult:
        if not isinstance(actor, Actor):
            raise InputValidationError("retrieval requires a validated Actor")
        validated_tenant_id = require_tenant(actor)
        if not isinstance(query, str):
            raise InputValidationError("query must be text")
        normalized_query = query.strip()
        if not normalized_query or len(normalized_query) > MAX_QUERY_LENGTH:
            raise InputValidationError(
                f"query must be between 1 and {MAX_QUERY_LENGTH} characters"
            )
        if len(excluded_chunk_ids) > MAX_EXCLUDED_CHUNK_IDS:
            raise InputValidationError(
                f"excluded chunk IDs must not exceed {MAX_EXCLUDED_CHUNK_IDS}"
            )
        if any(
            not isinstance(chunk_id, str)
            or not chunk_id.strip()
            or len(chunk_id) > 128
            for chunk_id in excluded_chunk_ids
        ):
            raise InputValidationError("excluded chunk IDs are invalid")
        try:
            for chunk_id in excluded_chunk_ids:
                UUID(chunk_id)
        except (ValueError, AttributeError, TypeError) as exc:
            raise InputValidationError("excluded chunk IDs must be valid UUIDs") from exc
        exclusions = list(dict.fromkeys(excluded_chunk_ids))

        try:
            return await asyncio.wait_for(
                self._search_validated(
                    actor,
                    normalized_query,
                    exclusions,
                    validated_tenant_id,
                    date.today(),
                ),
                timeout=self.timeout_ms / 1000,
            )
        except TimeoutError as exc:
            raise TransientRetrievalError("hybrid retrieval timed out") from exc

    async def _search_validated(
        self,
        actor: Actor,
        normalized_query: str,
        exclusions: list[str],
        validated_tenant_id: str,
        as_of: date,
    ) -> HybridSearchResult:
        embedding: list[float] | None = None
        pre_failures = []
        try:
            raw_embedding = await asyncio.wait_for(
                self.embedder.embed_query(normalized_query),
                timeout=self.embedding_timeout_ms / 1000,
            )
        except TimeoutError:
            pre_failures.append(RetrievalChannelFailure("vector"))
        except TransientRetrievalError as exc:
            if exc.channel not in {None, "vector"}:
                raise
            pre_failures.append(RetrievalChannelFailure("vector"))
        else:
            if len(raw_embedding) != self.embedder.dimension:
                raise InputValidationError("query embedding dimension is invalid")
            embedding = [float(value) for value in raw_embedding]
            if any(not math.isfinite(value) for value in embedding):
                raise InputValidationError(
                    "query embedding must contain only finite values"
                )
        keyword_search_text = await asyncio.to_thread(
            tokenize_keyword_text, normalized_query
        )
        article_numbers = article_number_references(normalized_query)
        channels = await self.repository.retrieve_hybrid_candidates(
            actor,
            query_embedding=embedding,
            embedding_model=self.embedder.model_name if embedding is not None else None,
            embedding_dimension=self.embedder.dimension,
            keyword_search_text=keyword_search_text,
            excluded_chunk_ids=exclusions,
            limit=self.channel_candidate_pool,
            timeout_ms=self.channel_timeout_ms,
            as_of=as_of,
            article_numbers=article_numbers,
        )
        if not isinstance(channels, RetrievalChannels):
            raise InputValidationError("repository returned invalid retrieval channels")
        for candidate_list in (channels.vector, channels.keyword, channels.reference):
            if any(not isinstance(item, EvidenceCandidate) for item in candidate_list):
                raise InputValidationError("repository returned invalid evidence records")
            if any(
                item.knowledge_scope is None
                or item.applicable_tenant_id != validated_tenant_id
                or item.as_of != as_of
                for item in candidate_list
            ):
                raise InputValidationError(
                    "repository returned evidence for a different tenant or as_of date"
                )
        failures = tuple(
            {
                failure.channel: failure
                for failure in (*pre_failures, *channels.failures)
            }.values()
        )
        failed_names = {failure.channel for failure in failures}
        if failed_names - {"vector", "keyword", "reference"}:
            raise InputValidationError("repository returned an unknown retrieval channel")
        if failed_names == {"vector", "keyword", "reference"}:
            raise TransientRetrievalError("all retrieval channels failed")
        if ("vector" in failed_names and channels.vector) or (
            "keyword" in failed_names and channels.keyword
        ) or ("reference" in failed_names and channels.reference):
            raise InputValidationError("failed retrieval channel returned candidates")

        fusion_limit = (
            min(MAX_RRF_TOP_K, self.channel_candidate_pool * 3)
            if self.reranker is not None
            else self.top_k
        )
        fused = reciprocal_rank_fusion(
            [channels.vector, channels.keyword, channels.reference],
            channel_names=("vector", "keyword", "reference"),
            rrf_k=self.rrf_k,
            top_k=fusion_limit,
        )
        if self.reranker is not None and fused:
            if self._reranker_executor is None:
                raise RuntimeError("reranker executor is unavailable")
            try:
                fused = await asyncio.wait_for(
                    self._reranker_executor.run(
                        rerank_with_validation,
                        self.reranker,
                        normalized_query,
                        fused,
                        top_k=self.top_k,
                    ),
                    timeout=self.rerank_timeout_ms / 1000,
                )
            except TimeoutError as exc:
                raise TransientRetrievalError("reranking timed out") from exc
            except RerankerCapacityError as exc:
                raise TransientRetrievalError(
                    "reranker capacity is busy"
                ) from exc
        trace = RetrievalTrace(
            query=normalized_query,
            excluded_chunk_ids=tuple(exclusions),
            channels=(
                self._channel_trace("vector", channels.vector),
                self._channel_trace("keyword", channels.keyword),
                self._channel_trace("reference", channels.reference),
            ),
            failures=failures,
            embedding_model=self.embedder.model_name,
            validated_tenant_id=validated_tenant_id,
            as_of=as_of,
            reranker_model=(
                str(getattr(self.reranker, "model_version", type(self.reranker).__name__))
                if self.reranker is not None
                else None
            ),
        )
        candidates = tuple(replace(item, retrieval_trace=trace) for item in fused)
        return HybridSearchResult(candidates=candidates, trace=trace)

    def close(self, *, wait: bool = True) -> None:
        if self._reranker_executor is not None:
            self._reranker_executor.close(wait=wait)

    async def aclose(self) -> None:
        if self._reranker_executor is not None:
            await self._reranker_executor.aclose()

    @staticmethod
    def _channel_trace(
        name: str, candidates: Sequence[EvidenceCandidate]
    ) -> RetrievalChannelTrace:
        return RetrievalChannelTrace(
            name=name,
            hits=tuple(
                RetrievalHitTrace(
                    chunk_id=item.chunk_id,
                    rank=item.rank,
                    score=item.score,
                    knowledge_scope=item.knowledge_scope,
                    tenant_id=item.tenant_id,
                    applicable_tenant_id=item.applicable_tenant_id,
                    as_of=item.as_of,
                )
                for item in candidates
            ),
        )
