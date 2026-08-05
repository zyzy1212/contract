from __future__ import annotations

import asyncio
import inspect
import json
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import text

from app.auth import Actor
from app.config import REPOSITORY_ROOT, get_settings
from app.contracts.service import JobNotFound
from app.db import tenant_transaction
from app.documents.models import ParsedDocument
from app.documents.parser import parse_document
from app.documents.structure import is_page_number_like
from app.embeddings import load_sentence_transformer
from app.retrieval.models import EvidenceCandidate
from app.review.schemas import GeneratedFinding
from app.storage.objects import LocalObjectStore, ObjectStore
from app.tasks.celery_app import celery_app


TERMINAL_CLAUSE_STATUSES = frozenset(
    {"complete", "insufficient", "failed", "needs_retrieval"}
)

_NUMBERED_CLAUSE_HEADING_RE = re.compile(
    r"^(?:"
    r"第\s*[〇零一二三四五六七八九十百千万\d]+\s*条"
    r"|[一二三四五六七八九十百]+、"
    r"|\d+(?:\.\d+)*[、.．]"
    r"|\d{1,3}(?:\.\d{1,3})+(?=\s|$)"
    r"|[（(]\s*[一二三四五六七八九十百\d]+\s*[）)]"
    r")"
)

_SECTION_TITLE_SUFFIX_RE = re.compile(
    r"(?:"
    r"基本情况|主要内容|概述|简介|介绍|情况|内容|背景|意义|目的|依据|"
    r"范围|定义|原则|要求|安排|措施|方式|程序|条件|标准|约定|事项|"
    r"风险|提示|影响|其他|附则|附件|文件"
    r")$"
)

_FOOTER_LINE_RE = re.compile(
    r"^(?:"
    r"特此公告[。．\.]?"
    r"|特此通知[。．\.]?"
    r"|董\s*事\s*会"
    r"|监\s*事\s*会"
    r"|《[^《》]{1,80}》"
    r"|[\u4e00-\u9fff]{2,20}(?:股份有限公司|有限责任公司|集团有限公司|有限公司)"
    r"|[〇零一二三四五六七八九十百\d]+年\d{1,2}月\d{1,2}日"
    r"|[二〇○两〇一二三四五六七八九十百\d]+年[〇零一二三四五六七八九十百\d]+月[〇零一二三四五六七八九十百\d]+日"
    r")$"
)


def _is_closing_footer(text: str) -> bool:
    """True for standalone document closing lines such as 特此公告 or a date."""
    return bool(_FOOTER_LINE_RE.fullmatch(text.strip()))


def _is_pure_section_title(text: str) -> bool:
    """True for short numbered headings such as 一、交易对方基本情况.

    Announcements and contracts commonly structure sections with Chinese
    numeral headings.  A heading that only names a section (no clause body,
    no colon, short title) should be treated as a boundary, not a clause.
    """
    stripped = text.strip()
    if not _NUMBERED_CLAUSE_HEADING_RE.match(stripped):
        return False
    if any(mark in stripped for mark in "：:，,。！？；;（）()"):
        return False
    body = _NUMBERED_CLAUSE_HEADING_RE.sub("", stripped, count=1).strip()
    return (
        bool(body)
        and len(body) <= 25
        and bool(_SECTION_TITLE_SUFFIX_RE.search(body))
    )


@dataclass(frozen=True)
class ClauseRecord:
    clause_id: str
    text: str
    locator: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.clause_id.strip():
            raise ValueError("clause_id must not be empty")
        if not self.text.strip():
            raise ValueError("clause text must not be empty")


@dataclass(frozen=True)
class ClauseReviewResult:
    status: str
    findings: list[GeneratedFinding] = field(default_factory=list)
    failure_reason: str | None = None
    evidence: list[EvidenceCandidate] = field(default_factory=list)


@dataclass(frozen=True)
class ReviewJobContext:
    job_id: str
    contract_id: str
    object_key: str
    filename: str
    status: str


class ReviewJobRepository(Protocol):
    async def load_job(self, actor: Actor, job_id: str) -> ReviewJobContext: ...

    async def save_checkpoints(
        self,
        actor: Actor,
        job_id: str,
        contract_id: str,
        clauses: Sequence[ClauseRecord],
    ) -> None: ...

    async def list_checkpoints(
        self, actor: Actor, job_id: str
    ) -> dict[str, str]: ...

    async def mark_clause(
        self,
        actor: Actor,
        job_id: str,
        contract_id: str,
        clause: ClauseRecord,
        status: str,
        *,
        failure_reason: str | None = None,
        findings: Sequence[GeneratedFinding] | None = None,
        evidence: Sequence[EvidenceCandidate] | None = None,
    ) -> None: ...

    async def finalize(
        self,
        actor: Actor,
        job_id: str,
        status: str,
        *,
        failure_reason: str | None = None,
    ) -> None: ...


class ClauseReviewer(Protocol):
    async def review_clause(
        self,
        actor: Actor,
        clause: ClauseRecord,
    ) -> ClauseReviewResult: ...


def split_contract_clauses(parsed: ParsedDocument) -> list[ClauseRecord]:
    clauses = _group_article_clauses(parsed)
    if clauses:
        return clauses
    return _split_numbered_clauses(parsed.blocks)


def _append_clause(
    clauses: list[ClauseRecord],
    pending: list[ParsedBlock],
) -> None:
    if not pending:
        return
    text = "\n".join(
        block.text.strip() for block in pending if block.text.strip()
    )
    if text.strip():
        first = pending[0]
        clauses.append(
            ClauseRecord(
                clause_id=f"clause-{len(clauses) + 1}",
                text=text,
                locator=first.locator.model_dump(),
            )
        )
    pending.clear()


def _split_numbered_clauses(
    blocks: Sequence[ParsedBlock],
) -> list[ClauseRecord]:
    """Group numbered/section headings with their following lines.

    PDF extraction often emits each wrapped line as its own block.  When a
    document has no ``第X条`` articles, group blocks by common contract
    headings such as ``一、``, ``1、`` and ``1.1`` so wrapped lines stay in
    the same clause instead of becoming one clause per line.
    """
    usable = [
        block
        for block in blocks
        if block.text.strip()
        and not is_page_number_like(block.text)
        and not _is_closing_footer(block.text)
    ]
    has_heading = any(
        _NUMBERED_CLAUSE_HEADING_RE.match(block.text.strip())
        for block in usable
    )
    if not has_heading:
        return [
            ClauseRecord(
                clause_id=f"clause-{index}",
                text=block.text,
                locator=block.locator.model_dump(),
            )
            for index, block in enumerate(usable, start=1)
        ]

    first_section_title = next(
        (
            index
            for index, block in enumerate(usable)
            if _is_pure_section_title(block.text)
        ),
        None,
    )
    start = first_section_title if first_section_title is not None else 0
    clauses: list[ClauseRecord] = []
    pending: list[ParsedBlock] = []
    for block in usable[start:]:
        if block.block_type == "heading":
            _append_clause(clauses, pending)
            continue
        if _is_pure_section_title(block.text):
            _append_clause(clauses, pending)
            continue
        if _NUMBERED_CLAUSE_HEADING_RE.match(block.text.strip()):
            _append_clause(clauses, pending)
            pending.append(block)
        else:
            pending.append(block)
    _append_clause(clauses, pending)
    return clauses


def _group_article_clauses(parsed: ParsedDocument) -> list[ClauseRecord]:
    """Group main-body article headings with their following paragraphs.

    PDF text extraction often emits every line as a separate block, and the
    table of contents repeats article headings.  This groups the real body
    articles into complete clauses and skips TOC entries and preamble noise.
    """
    clauses: list[ClauseRecord] = []
    pending: list[ParsedBlock] = []

    def flush() -> None:
        _append_clause(clauses, pending)

    blocks = parsed.blocks
    article_run_indices: set[int] = set()
    run_start: int | None = None
    for index, block in enumerate(blocks):
        is_article = block.block_type == "article"
        if is_article and run_start is None:
            run_start = index
        elif not is_article and run_start is not None:
            if index - run_start >= 2:
                article_run_indices.update(range(run_start, index))
            run_start = None
    if run_start is not None and len(blocks) - run_start >= 2:
        article_run_indices.update(range(run_start, len(blocks)))

    for index, block in enumerate(blocks):
        if is_page_number_like(block.text):
            continue
        if block.block_type == "heading":
            flush()
            continue
        if block.block_type == "article" and index in article_run_indices:
            flush()
            continue
        if block.block_type == "article":
            flush()
            pending.append(block)
        elif pending:
            pending.append(block)
        else:
            flush()
    flush()
    return clauses


class SqlJobRepository:
    def __init__(self, transaction_factory=tenant_transaction) -> None:
        self._transaction_factory = transaction_factory

    async def load_job(self, actor: Actor, job_id: str) -> ReviewJobContext:
        async with self._transaction_factory(actor) as session:
            result = await session.execute(
                text(
                    """
                    SELECT job.id::text AS job_id,
                           job.contract_id::text AS contract_id,
                           job.status::text AS status,
                           contract.object_key,
                           contract.filename
                    FROM review_jobs job
                    JOIN contracts contract
                      ON contract.id = job.contract_id
                     AND contract.tenant_id = job.tenant_id
                    WHERE job.id = :job_id
                    """
                ),
                {"job_id": job_id},
            )
            row = result.mappings().first()
            if row is None:
                raise JobNotFound("review job does not exist")
            return ReviewJobContext(
                job_id=str(row["job_id"]),
                contract_id=str(row["contract_id"]),
                object_key=str(row["object_key"]),
                filename=str(row["filename"]),
                status=str(row["status"]),
            )

    async def save_checkpoints(
        self,
        actor: Actor,
        job_id: str,
        contract_id: str,
        clauses: Sequence[ClauseRecord],
    ) -> None:
        async with self._transaction_factory(actor) as session:
            await session.execute(
                text(
                    """
                    UPDATE review_jobs
                    SET status = 'running',
                        total_clauses = :total,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :job_id
                    """
                ),
                {"job_id": job_id, "total": len(clauses)},
            )
            for clause in clauses:
                await session.execute(
                    text(
                        """
                        INSERT INTO review_clause_checkpoints (
                            tenant_id, job_id, contract_id, clause_id,
                            clause_text, status, locator
                        ) VALUES (
                            :tenant_id, :job_id, :contract_id, :clause_id,
                            :clause_text, 'queued', CAST(:locator AS jsonb)
                        )
                        ON CONFLICT (job_id, clause_id) DO NOTHING
                        """
                    ),
                    {
                        "tenant_id": actor.tenant_id,
                        "job_id": job_id,
                        "contract_id": contract_id,
                        "clause_id": clause.clause_id,
                        "clause_text": clause.text,
                        "locator": json.dumps(
                            clause.locator or {},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    },
                )

    async def list_checkpoints(
        self, actor: Actor, job_id: str
    ) -> dict[str, str]:
        async with self._transaction_factory(actor) as session:
            result = await session.execute(
                text(
                    """
                    SELECT clause_id, status::text AS status
                    FROM review_clause_checkpoints
                    WHERE job_id = :job_id
                    """
                ),
                {"job_id": job_id},
            )
            return {
                str(row["clause_id"]): str(row["status"])
                for row in result.mappings().all()
            }

    async def mark_clause(
        self,
        actor: Actor,
        job_id: str,
        contract_id: str,
        clause: ClauseRecord,
        status: str,
        *,
        failure_reason: str | None = None,
        findings: Sequence[GeneratedFinding] | None = None,
        evidence: Sequence[EvidenceCandidate] | None = None,
    ) -> None:
        async with self._transaction_factory(actor) as session:
            await session.execute(
                text(
                    """
                    INSERT INTO review_clause_checkpoints (
                        tenant_id, job_id, contract_id, clause_id,
                        clause_text, status, failure_reason, locator
                    ) VALUES (
                        :tenant_id, :job_id, :contract_id, :clause_id,
                        :clause_text, :status, :failure_reason,
                        CAST(:locator AS jsonb)
                    )
                    ON CONFLICT (job_id, clause_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        failure_reason = EXCLUDED.failure_reason,
                        updated_at = CURRENT_TIMESTAMP
                    """
                ),
                {
                    "tenant_id": actor.tenant_id,
                    "job_id": job_id,
                    "contract_id": contract_id,
                    "clause_id": clause.clause_id,
                    "clause_text": clause.text,
                    "status": status,
                    "failure_reason": failure_reason,
                    "locator": json.dumps(
                        clause.locator or {},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                },
            )
            evidence_by_id = {
                item.chunk_id: item for item in (evidence or ())
            }
            for finding in findings or ():
                result = await session.execute(
                    text(
                        """
                        INSERT INTO review_findings (
                            tenant_id, job_id, contract_id, clause_id,
                            risk_level, problem, reason, suggestion,
                            proposed_clause, status, model_name, model_version
                        ) VALUES (
                            :tenant_id, :job_id, :contract_id, :clause_id,
                            :risk_level, :problem, :reason, :suggestion,
                            :proposed_clause, 'passed', :model_name, :model_version
                        )
                        RETURNING id::text AS id
                        """
                    ),
                    {
                        "tenant_id": actor.tenant_id,
                        "job_id": job_id,
                        "contract_id": contract_id,
                        "clause_id": clause.clause_id,
                        "risk_level": finding.risk_level,
                        "problem": finding.problem,
                        "reason": finding.reason,
                        "suggestion": finding.suggestion,
                        "proposed_clause": finding.proposed_clause,
                        "model_name": "contract-review-v1",
                        "model_version": "v1",
                    },
                )
                finding_row = result.mappings().first()
                if finding_row is None:
                    raise RuntimeError("finding insert returned no id")
                finding_id = str(finding_row["id"])
                for rank, evidence_id in enumerate(
                    finding.evidence_ids, start=1
                ):
                    candidate = evidence_by_id.get(evidence_id)
                    if candidate is None:
                        continue
                    await session.execute(
                        text(
                            """
                            INSERT INTO evidence_snapshots (
                                tenant_id, finding_id, knowledge_chunk_id,
                                source_content_sha256, source_document_sha256,
                                rank, exact_excerpt, source_snapshot,
                                retrieval_trace
                            ) VALUES (
                                :tenant_id, :finding_id,
                                CAST(:knowledge_chunk_id AS uuid),
                                :source_content_sha256,
                                :source_document_sha256,
                                :rank, :exact_excerpt,
                                CAST(:source_snapshot AS jsonb),
                                CAST(:retrieval_trace AS jsonb)
                            )
                            """
                        ),
                        {
                            "tenant_id": actor.tenant_id,
                            "finding_id": finding_id,
                            "knowledge_chunk_id": candidate.chunk_id,
                            "source_content_sha256": (
                                candidate.source_text_sha256 or "0" * 64
                            ),
                            "source_document_sha256": (
                                candidate.source_snapshot.get(
                                    "source_document_sha256"
                                )
                                or "0" * 64
                            ),
                            "rank": rank,
                            "exact_excerpt": candidate.text,
                            "source_snapshot": json.dumps(
                                _jsonable(candidate.source_snapshot),
                                ensure_ascii=False,
                            ),
                            "retrieval_trace": json.dumps(
                                _candidate_trace(candidate),
                                ensure_ascii=False,
                                default=str,
                            ),
                        },
                    )
            await session.execute(
                text(
                    """
                    UPDATE review_jobs
                    SET completed_clauses = (
                        SELECT count(*)
                        FROM review_clause_checkpoints
                        WHERE job_id = :job_id
                          AND status IN (
                              'complete', 'insufficient', 'failed',
                              'needs_retrieval'
                          )
                    ),
                    updated_at = CURRENT_TIMESTAMP
                    WHERE id = :job_id
                    """
                ),
                {"job_id": job_id},
            )

    async def finalize(
        self,
        actor: Actor,
        job_id: str,
        status: str,
        *,
        failure_reason: str | None = None,
    ) -> None:
        async with self._transaction_factory(actor) as session:
            checkpoints = await self.list_checkpoints(actor, job_id)
            total = len(checkpoints)
            completed = sum(
                1
                for value in checkpoints.values()
                if value in TERMINAL_CLAUSE_STATUSES
            )
            await session.execute(
                text(
                    """
                    UPDATE review_jobs
                    SET status = :status,
                        failure_reason = :failure_reason,
                        total_clauses = :total,
                        completed_clauses = :completed,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :job_id
                    """
                ),
                {
                    "status": status,
                    "failure_reason": failure_reason,
                    "total": total,
                    "completed": completed,
                    "job_id": job_id,
                },
            )


def _candidate_trace(candidate: EvidenceCandidate) -> dict[str, Any]:
    trace = candidate.retrieval_trace
    if trace is None:
        return {}
    return {
        "query": trace.query,
        "channels": [
            {
                "name": channel.name,
                "hits": [
                    {
                        "chunk_id": hit.chunk_id,
                        "rank": hit.rank,
                        "score": hit.score,
                    }
                    for hit in channel.hits
                ],
            }
            for channel in trace.channels
        ],
    }


def _jsonable(value: Any) -> Any:
    """Recursively convert immutable mapping/tuple structures to JSON types."""
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, (list, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


async def _parse_contract(
    data: bytes,
    filename: str,
) -> ParsedDocument:
    suffix = Path(filename).suffix.lower()
    if suffix not in {".pdf", ".docx"}:
        raise ValueError("contract object is not a supported file type")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        handle.write(data)
        path = Path(handle.name)
    try:
        return parse_document(path)
    finally:
        path.unlink(missing_ok=True)


async def run_review_job(
    job_id: str,
    tenant_id: str,
    *,
    repository: ReviewJobRepository | None = None,
    object_store: ObjectStore | None = None,
    clause_splitter: Callable[[ParsedDocument], list[ClauseRecord]] | None = None,
    clause_reviewer: ClauseReviewer | None = None,
    contract_parser: Callable[[bytes, str], Any] | None = None,
) -> None:
    repository = repository or SqlJobRepository()
    object_store = object_store or build_object_store()
    clause_splitter = clause_splitter or split_contract_clauses
    clause_reviewer = clause_reviewer or build_default_clause_reviewer()
    contract_parser = contract_parser or _parse_contract
    actor = Actor(user_id="system", tenant_id=tenant_id, role="admin")
    job = await repository.load_job(actor, job_id)
    if job.status in {"complete", "failed", "partial"}:
        return
    data = await object_store.get(job.object_key)
    parsed = contract_parser(data, job.filename)
    if inspect.isawaitable(parsed):
        parsed = await parsed
    clauses = clause_splitter(parsed)
    await repository.save_checkpoints(actor, job_id, job.contract_id, clauses)
    checkpoints = await repository.list_checkpoints(actor, job_id)
    pending = [
        clause
        for clause in clauses
        if checkpoints.get(clause.clause_id) not in TERMINAL_CLAUSE_STATUSES
    ]
    outcomes = dict(checkpoints)
    failure_reasons: dict[str, str] = {}
    concurrency = max(1, get_settings().review_clause_concurrency)
    semaphore = asyncio.Semaphore(concurrency)

    async def review_one(
        clause: ClauseRecord,
    ) -> tuple[str, str | None, str | None]:
        async with semaphore:
            try:
                result = await clause_reviewer.review_clause(actor, clause)
                await repository.mark_clause(
                    actor,
                    job_id,
                    job.contract_id,
                    clause,
                    result.status,
                    failure_reason=result.failure_reason,
                    findings=result.findings,
                    evidence=result.evidence,
                )
                return clause.clause_id, result.status, None
            except Exception as exc:
                await repository.mark_clause(
                    actor,
                    job_id,
                    job.contract_id,
                    clause,
                    "failed",
                    failure_reason=str(exc),
                )
                return clause.clause_id, "failed", str(exc)

    results = await asyncio.gather(*(review_one(clause) for clause in pending))
    for clause_id, status, failure_reason in results:
        outcomes[clause_id] = status
        if failure_reason is not None:
            failure_reasons[clause_id] = failure_reason
    statuses = {outcomes.get(clause.clause_id, "queued") for clause in clauses}
    if not clauses or statuses == {"complete"}:
        final_status = "complete"
    elif statuses <= {"failed"}:
        final_status = "failed"
    else:
        final_status = "partial"
    summary_failure_reason = (
        next(iter(failure_reasons.values()), "review failed")
        if final_status == "failed"
        else None
    )
    await repository.finalize(
        actor,
        job_id,
        final_status,
        failure_reason=summary_failure_reason,
    )


def build_object_store() -> LocalObjectStore:
    return LocalObjectStore(REPOSITORY_ROOT / "storage" / "objects")


class SentenceTransformerQueryEmbedder:
    model_name = "BAAI/bge-small-zh-v1.5"
    dimension = 512

    def __init__(self, *, precision: str | None = None) -> None:
        settings = get_settings()
        self.precision = (
            precision if precision is not None else settings.embedding_precision
        )
        self._model = None

    async def embed_query(self, query: str) -> Sequence[float]:
        if self._model is None:
            self._model = load_sentence_transformer(self.model_name, self.precision)
        vector = self._model.encode(query, normalize_embeddings=True)
        return [float(value) for value in vector]


class ClauseReviewerPipeline:
    def __init__(
        self,
        retriever,
        judge,
        generator,
        reviewer,
        query_expander=None,
        retrieval_max_rounds: int = 3,
    ) -> None:
        self._retriever = retriever
        self._judge = judge
        self._generator = generator
        self._reviewer = reviewer
        self._query_expander = query_expander
        self._retrieval_max_rounds = retrieval_max_rounds

    async def review_clause(
        self,
        actor: Actor,
        clause: ClauseRecord,
    ) -> ClauseReviewResult:
        from app.review.orchestrator import (
            collect_sufficient_evidence,
            generate_and_review,
        )

        collection = await collect_sufficient_evidence(
            actor,
            clause.text,
            self._retriever,
            self._judge,
            query_expander=self._query_expander,
            max_rounds=self._retrieval_max_rounds,
        )
        if collection.status != "sufficient":
            return ClauseReviewResult(status="insufficient")
        final = await generate_and_review(
            {"clause_id": clause.clause_id, "text": clause.text},
            collection.candidates,
            self._generator,
            self._reviewer,
        )
        if final.status == "complete":
            return ClauseReviewResult(
                status="complete",
                findings=final.findings,
                evidence=list(collection.candidates),
            )
        if final.status == "needs_retrieval":
            return ClauseReviewResult(status="needs_retrieval")
        return ClauseReviewResult(
            status="failed",
            failure_reason="independent review rejected the draft",
        )


def build_default_clause_reviewer() -> ClauseReviewer:
    from app.knowledge.repository import KnowledgeRepository
    from app.llm.deepseek import DeepSeekClient
    from app.review.evidence_judge import DeepSeekEvidenceJudge
    from app.review.generator import DeepSeekFindingGenerator
    from app.review.query_expansion import DeepSeekQueryExpander
    from app.review.reviewer import DeepSeekResultReviewer
    from app.retrieval.hybrid import HybridRetriever

    settings = get_settings()
    client = DeepSeekClient(settings)
    query_expander = (
        DeepSeekQueryExpander(
            client,
            max_queries=settings.review_query_expansion_max_queries,
            min_characters=settings.review_query_expansion_min_characters,
        )
        if settings.review_query_expansion_enabled
        else None
    )
    repository = KnowledgeRepository(transaction_factory=tenant_transaction)
    retriever = HybridRetriever(
        repository=repository,
        embedder=SentenceTransformerQueryEmbedder(),
    )
    return ClauseReviewerPipeline(
        retriever,
        DeepSeekEvidenceJudge(client),
        DeepSeekFindingGenerator(client),
        DeepSeekResultReviewer(client),
        query_expander=query_expander,
        retrieval_max_rounds=settings.review_retrieval_max_rounds,
    )


async def enqueue_review_task(job_id: str, tenant_id: str) -> None:
    review_contract_task.delay(job_id, tenant_id)


@celery_app.task(
    name="contract_review.review_contract",
    bind=True,
    max_retries=3,
)
def review_contract_task(self, job_id: str, tenant_id: str) -> None:
    try:
        asyncio.run(run_review_job(job_id, tenant_id))
    except Exception as exc:
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)
