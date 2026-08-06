from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from math import isfinite
from threading import BoundedSemaphore, Lock
from typing import Any, Protocol

from .models import EvidenceCandidate


MAX_RERANK_TOP_K = 1000
DEFAULT_CROSS_ENCODER_MODEL = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"


class Reranker(Protocol):
    def rerank(
        self,
        query: str,
        candidates: Sequence[EvidenceCandidate],
        top_k: int,
    ) -> list[EvidenceCandidate]: ...


class CrossEncoderModel(Protocol):
    def predict(self, pairs: Sequence[tuple[str, str]]) -> Sequence[float]: ...


class RerankerCapacityError(RuntimeError):
    """The reranker queue is full or the executor is closed."""


class BoundedRerankerExecutor:
    """One inference worker with a bounded waiting queue.

    Cancelling an awaiter cannot stop Python/native model code already running.
    The admitted task therefore retains capacity until it actually exits; waiting
    callers stay within the caller's own timeout instead of overlapping it.
    """

    def __init__(self, max_waiters: int = 16) -> None:
        if (
            isinstance(max_waiters, bool)
            or not isinstance(max_waiters, int)
            or max_waiters < 1
        ):
            raise ValueError("max_waiters must be a positive integer")
        self._max_waiters = max_waiters
        self._admission = BoundedSemaphore(1)
        self._waiters = BoundedSemaphore(max_waiters)
        self._state_lock = Lock()
        self._closed = False
        self._executor: ThreadPoolExecutor | None = None

    async def run(self, function: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        if not self._waiters.acquire(blocking=False):
            raise RerankerCapacityError("reranker queue is full")
        admitted = False
        try:
            while not self._admission.acquire(blocking=False):
                await asyncio.sleep(0.05)
            admitted = True
            with self._state_lock:
                if self._closed:
                    raise RerankerCapacityError("reranker executor is closed")
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(
                        max_workers=1, thread_name_prefix="contract-reranker"
                    )
                executor = self._executor
        except BaseException:
            if admitted:
                self._admission.release()
            self._waiters.release()
            raise

        call = functools.partial(function, *args, **kwargs)

        def admitted_call() -> Any:
            try:
                return call()
            finally:
                self._admission.release()
                self._waiters.release()

        loop = asyncio.get_running_loop()
        try:
            future = loop.run_in_executor(executor, admitted_call)
        except BaseException:
            # ``close`` may win the race after the executor leaves the state
            # lock but before submission. No worker will release capacity then.
            self._admission.release()
            self._waiters.release()
            raise
        return await asyncio.shield(future)

    def close(self, *, wait: bool = True) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            executor = self._executor
            self._executor = None
        if executor is not None:
            executor.shutdown(wait=wait, cancel_futures=True)

    async def aclose(self) -> None:
        await asyncio.to_thread(self.close)


def _validate_top_k(top_k: int) -> None:
    if (
        isinstance(top_k, bool)
        or not isinstance(top_k, int)
        or top_k < 1
        or top_k > MAX_RERANK_TOP_K
    ):
        raise ValueError(f"top_k must be between 1 and {MAX_RERANK_TOP_K}")


def _provenance(candidate: EvidenceCandidate) -> tuple[Any, ...]:
    return (
        candidate.chunk_id,
        candidate.document_id,
        candidate.text,
        candidate.source_snapshot_id,
        candidate.source_text_sha256,
        candidate.source_snapshot,
        candidate.channel_scores,
        candidate.channel_ranks,
        candidate.knowledge_scope,
        candidate.tenant_id,
        candidate.applicable_tenant_id,
        candidate.as_of,
    )


def _unique_candidates(
    candidates: Sequence[EvidenceCandidate],
) -> list[EvidenceCandidate]:
    unique: dict[str, EvidenceCandidate] = {}
    for candidate in candidates:
        existing = unique.get(candidate.chunk_id)
        if existing is not None and _provenance(existing) != _provenance(candidate):
            raise ValueError("reranker input contains inconsistent evidence records")
        unique.setdefault(candidate.chunk_id, candidate)
    return list(unique.values())


class IdentityReranker:
    model_version = "identity-v1"

    def rerank(
        self,
        query: str,
        candidates: Sequence[EvidenceCandidate],
        top_k: int,
    ) -> list[EvidenceCandidate]:
        _validate_top_k(top_k)
        unique = _unique_candidates(candidates)[:top_k]
        return [replace(item, rank=rank) for rank, item in enumerate(unique, start=1)]


class CrossEncoderReranker:
    def __init__(
        self,
        model_name: str = DEFAULT_CROSS_ENCODER_MODEL,
        *,
        loader: Callable[[str], CrossEncoderModel] | None = None,
    ) -> None:
        if not model_name.strip():
            raise ValueError("model_name must not be empty")
        self.model_name = model_name
        self.model_version = model_name
        self._loader = loader
        self._model: CrossEncoderModel | None = None
        self._inference_lock = Lock()

    def _load_model(self) -> CrossEncoderModel:
        if self._model is None:
            if self._loader is None:
                from sentence_transformers import CrossEncoder

                self._model = CrossEncoder(self.model_name)
            else:
                self._model = self._loader(self.model_name)
        return self._model

    def warm(self) -> None:
        """Load the model before the bounded reranking timeout budget starts."""
        with self._inference_lock:
            self._load_model()

    def rerank(
        self,
        query: str,
        candidates: Sequence[EvidenceCandidate],
        top_k: int,
    ) -> list[EvidenceCandidate]:
        _validate_top_k(top_k)
        unique = _unique_candidates(candidates)
        if not unique:
            return []
        with self._inference_lock:
            raw_scores = self._load_model().predict(
                [(query, candidate.text) for candidate in unique]
            )
        if len(raw_scores) != len(unique):
            raise ValueError("reranker returned an invalid score count")
        scored: list[EvidenceCandidate] = []
        for candidate, raw_score in zip(unique, raw_scores, strict=True):
            score = float(raw_score)
            if not isfinite(score):
                raise ValueError("reranker scores must be finite")
            scored.append(replace(candidate, score=score))
        scored.sort(key=lambda item: (-item.score, item.chunk_id))
        return [
            replace(item, rank=rank)
            for rank, item in enumerate(scored[:top_k], start=1)
        ]


def rerank_with_validation(
    reranker: Reranker,
    query: str,
    candidates: Sequence[EvidenceCandidate],
    *,
    top_k: int,
) -> list[EvidenceCandidate]:
    _validate_top_k(top_k)
    canonical = {item.chunk_id: item for item in _unique_candidates(candidates)}
    returned = reranker.rerank(query, tuple(canonical.values()), top_k)
    deduplicated: dict[str, EvidenceCandidate] = {}
    for item in returned:
        if not isinstance(item, EvidenceCandidate):
            raise ValueError("reranker must return EvidenceCandidate records")
        original = canonical.get(item.chunk_id)
        if original is None:
            raise ValueError("reranker returned an unknown evidence ID")
        if _provenance(original) != _provenance(item):
            raise ValueError("reranker changed evidence provenance")
        if not isfinite(item.score):
            raise ValueError("reranker score must be finite")
        existing = deduplicated.get(item.chunk_id)
        if existing is None or item.score > existing.score:
            deduplicated[item.chunk_id] = replace(original, score=item.score)
    ordered = sorted(
        deduplicated.values(), key=lambda item: (-item.score, item.chunk_id)
    )[:top_k]
    return [replace(item, rank=rank) for rank, item in enumerate(ordered, start=1)]
