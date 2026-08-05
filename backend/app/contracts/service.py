from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Actor
from app.common.errors import DomainError, InputValidationError
from app.config import REPOSITORY_ROOT, Settings, get_settings
from app.db import tenant_transaction
from app.storage.objects import LocalObjectStore, ObjectStore


JobStatus = Literal["queued", "running", "complete", "partial", "failed"]


class ReviewJobDTO(BaseModel):
    id: str
    status: JobStatus
    contract_id: str
    total_clauses: int = 0
    completed_clauses: int = 0
    unreviewed_clause_ids: list[str] = Field(default_factory=list)
    failure_reason: str | None = None
    created_at: datetime | None = None


class EvidenceDTO(BaseModel):
    id: str
    text: str
    rank: int
    source_snapshot: dict[str, Any] = Field(default_factory=dict)
    source_content_sha256: str = ""


class FindingDTO(BaseModel):
    id: str
    clause_id: str
    risk_level: Literal["high", "medium", "low"]
    problem: str
    reason: str
    suggestion: str
    proposed_clause: str
    evidence: list[EvidenceDTO] = Field(default_factory=list)


class ReviewJobDetail(ReviewJobDTO):
    findings: list[FindingDTO] = Field(default_factory=list)
    source_clauses: list[dict[str, Any]] = Field(default_factory=list)
    insufficient_clause_count: int = 0


class ReviewHistoryItem(BaseModel):
    id: str
    contract_id: str
    filename: str
    content_type: str
    status: JobStatus
    total_clauses: int = 0
    completed_clauses: int = 0
    failure_reason: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class JobNotFound(DomainError):
    """A review job does not exist for the authenticated tenant."""


class ActiveJobConflict(DomainError):
    """The contract already has a review job that is still active."""


_JOB_SELECT = """
    SELECT id::text AS id,
           status::text AS status,
           contract_id::text AS contract_id,
           total_clauses,
           completed_clauses,
           failure_reason,
           created_at
    FROM review_jobs
"""


def _job_dto(row: Any, unreviewed: list[str]) -> ReviewJobDTO:
    return ReviewJobDTO(
        id=str(row["id"]),
        status=row["status"],
        contract_id=str(row["contract_id"]),
        total_clauses=int(row["total_clauses"]),
        completed_clauses=int(row["completed_clauses"]),
        unreviewed_clause_ids=unreviewed,
        failure_reason=row["failure_reason"],
        created_at=row["created_at"],
    )


def _clause_locator(locator: Any) -> dict[str, Any]:
    locator = locator or {}
    return {
        "article_number": locator.get("article_number"),
        "paragraph_index": locator.get("paragraph_index"),
        "page_start": locator.get("page_start"),
        "page_end": locator.get("page_end"),
        "bboxes": locator.get("bboxes", []),
    }


class ContractService:
    def __init__(
        self,
        object_store: ObjectStore,
        *,
        transaction_factory=tenant_transaction,
        enqueue=None,
        settings: Settings | None = None,
        review_config: dict[str, Any] | None = None,
    ) -> None:
        self._object_store = object_store
        self._transaction_factory = transaction_factory
        self._enqueue = enqueue
        self._settings = settings or get_settings()
        self._review_config = review_config or {"version": 1}

    async def create_review(
        self,
        actor: Actor,
        filename: str,
        content_type: str,
        stream,
    ) -> ReviewJobDTO:
        data = await self._read_bounded(stream)
        content_sha256 = hashlib.sha256(data).hexdigest()
        object_key = f"tenants/{actor.tenant_id}/contracts/{content_sha256}"
        await self._object_store.put(object_key, data)
        idempotency_key = self._idempotency_key(actor.tenant_id, content_sha256)
        async with self._transaction_factory(actor) as session:
            existing = await self._find_existing_job(
                session, idempotency_key, actor.tenant_id
            )
            if existing is not None:
                unreviewed = await self._unreviewed_clause_ids(
                    session, existing["id"]
                )
                return _job_dto(existing, unreviewed)
            contract_id = await self._insert_contract(
                session,
                actor,
                filename,
                content_type,
                content_sha256,
                object_key,
            )
            row = await self._insert_job(
                session, actor, contract_id, idempotency_key
            )
        if self._enqueue is not None:
            result = self._enqueue(row["id"], actor.tenant_id)
            if inspect.isawaitable(result):
                await result
        return _job_dto(row, [])

    async def retry_review(self, actor: Actor, job_id: str) -> ReviewJobDTO:
        async with self._transaction_factory(actor) as session:
            row = await self._find_job_by_id(session, job_id, actor.tenant_id)
            if row is None:
                raise JobNotFound("review job does not exist")
            if row["status"] != "failed" or int(row["completed_clauses"]) != 0:
                raise InputValidationError(
                    "only failed jobs without completed clauses can be retried"
                )
            await session.execute(
                text(
                    """
                    UPDATE review_clause_checkpoints
                    SET status = 'queued', failure_reason = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE job_id = :job_id
                    """
                ),
                {"job_id": job_id},
            )
            await session.execute(
                text(
                    """
                    UPDATE review_jobs
                    SET status = 'queued', failure_reason = NULL,
                        total_clauses = 0, completed_clauses = 0,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :job_id
                    """
                ),
                {"job_id": job_id},
            )
        if self._enqueue is not None:
            result = self._enqueue(job_id, actor.tenant_id)
            if inspect.isawaitable(result):
                await result
        return ReviewJobDTO(
            id=job_id,
            status="queued",
            contract_id=str(row["contract_id"]),
        )

    async def rerun_review(self, actor: Actor, job_id: str) -> ReviewJobDTO:
        async with self._transaction_factory(actor) as session:
            row = await self._find_job_by_id(session, job_id, actor.tenant_id)
            if row is None:
                raise JobNotFound("review job does not exist")
            contract_id = str(row["contract_id"])
            active = await self._find_active_job(
                session, contract_id, actor.tenant_id
            )
            if active is not None:
                raise ActiveJobConflict(
                    "contract already has an active review job"
                )
            idempotency_key = self._rerun_idempotency_key(
                actor.tenant_id, contract_id
            )
            new_row = await self._insert_job(
                session, actor, contract_id, idempotency_key
            )
        if self._enqueue is not None:
            result = self._enqueue(new_row["id"], actor.tenant_id)
            if inspect.isawaitable(result):
                await result
        return _job_dto(new_row, [])

    async def get_contract_file(
        self,
        actor: Actor,
        job_id: str,
    ) -> tuple[str, str, bytes]:
        async with self._transaction_factory(actor) as session:
            row = await self._find_job_file(session, job_id, actor.tenant_id)
            if row is None:
                raise JobNotFound("review job does not exist")
            object_key = str(row["object_key"])
            filename = str(row["filename"])
            content_type = str(row["content_type"])
        data = await self._object_store.get(object_key)
        return filename, content_type, data

    async def get_review(self, actor: Actor, job_id: str) -> ReviewJobDetail:
        async with self._transaction_factory(actor) as session:
            row = await self._find_job_by_id(session, job_id, actor.tenant_id)
            if row is None:
                raise JobNotFound("review job does not exist")
            unreviewed = await self._unreviewed_clause_ids(session, job_id)
            findings: list[FindingDTO] = []
            for finding_row in await self._findings(session, job_id):
                evidence = [
                    EvidenceDTO(
                        id=str(item["id"]),
                        text=str(item["text"]),
                        rank=int(item["rank"]),
                        source_snapshot=dict(item["source_snapshot"]),
                        source_content_sha256=str(
                            item["source_content_sha256"]
                        ),
                    )
                    for item in await self._evidence(session, finding_row["id"])
                ]
                findings.append(
                    FindingDTO(
                        id=str(finding_row["id"]),
                        clause_id=str(finding_row["clause_id"]),
                        risk_level=finding_row["risk_level"],
                        problem=str(finding_row["problem"]),
                        reason=str(finding_row["reason"]),
                        suggestion=str(finding_row["suggestion"]),
                        proposed_clause=str(finding_row["proposed_clause"]),
                        evidence=evidence,
                    )
                )
            source_rows = await self._source_clauses(session, job_id)
            source_clauses = [
                {
                    "id": str(item["clause_id"]),
                    "text": str(item["clause_text"]),
                    **_clause_locator(item["locator"]),
                }
                for item in source_rows
            ]
            insufficient_clause_count = sum(
                1 for item in source_rows if item["status"] == "insufficient"
            )
            return ReviewJobDetail(
                **(_job_dto(row, unreviewed).model_dump()),
                findings=findings,
                source_clauses=source_clauses,
                insufficient_clause_count=insufficient_clause_count,
            )

    async def list_review_history(
        self,
        actor: Actor,
        *,
        limit: int = 100,
    ) -> list[ReviewHistoryItem]:
        async with self._transaction_factory(actor) as session:
            result = await session.execute(
                text(
                    """
                    SELECT job.id::text AS id,
                           job.contract_id::text AS contract_id,
                           contract.filename AS filename,
                           contract.content_type AS content_type,
                           job.status::text AS status,
                           job.total_clauses,
                           job.completed_clauses,
                           job.failure_reason,
                           job.created_at,
                           job.updated_at
                    FROM review_jobs job
                    JOIN contracts contract
                      ON contract.id = job.contract_id
                     AND contract.tenant_id = job.tenant_id
                    WHERE job.tenant_id = :tenant_id
                      AND (
                        job.requested_by IS NULL
                        OR EXISTS (
                            SELECT 1
                            FROM users user_record
                            WHERE user_record.id = job.requested_by
                              AND user_record.tenant_id = job.tenant_id
                              AND user_record.external_subject = :external_subject
                        )
                    )
                    ORDER BY job.created_at DESC, job.id DESC
                    """
                ),
                {
                    "tenant_id": actor.tenant_id,
                    "external_subject": actor.user_id,
                },
            )
            rows = result.mappings().all()

        history: list[ReviewHistoryItem] = []
        seen_contracts: set[str] = set()
        for row in rows:
            contract_id = str(row["contract_id"])
            if contract_id in seen_contracts:
                continue
            seen_contracts.add(contract_id)
            history.append(
                ReviewHistoryItem(
                    id=str(row["id"]),
                    contract_id=contract_id,
                    filename=str(row["filename"]),
                    content_type=str(row["content_type"]),
                    status=row["status"],
                    total_clauses=int(row["total_clauses"]),
                    completed_clauses=int(row["completed_clauses"]),
                    failure_reason=row["failure_reason"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            )
            if len(history) >= limit:
                break
        return history

    async def _read_bounded(self, stream) -> bytes:
        max_bytes = self._settings.document_parser_max_source_bytes
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = stream.read(1024 * 1024)
            if inspect.isawaitable(chunk):
                chunk = await chunk
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise InputValidationError(
                    f"contract source must not exceed {max_bytes} bytes"
                )
            chunks.append(chunk)
        return b"".join(chunks)

    def _idempotency_key(self, tenant_id: str, content_sha256: str) -> str:
        config_json = json.dumps(
            self._review_config, sort_keys=True, ensure_ascii=False
        )
        config_digest = hashlib.sha256(config_json.encode("utf-8")).hexdigest()[:8]
        return f"{tenant_id}:{content_sha256}:{config_digest}"

    def _rerun_idempotency_key(self, tenant_id: str, contract_id: str) -> str:
        return f"{tenant_id}:{contract_id}:rerun:{uuid.uuid4()}"

    @staticmethod
    async def _find_existing_job(
        session: AsyncSession,
        idempotency_key: str,
        tenant_id: str,
    ):
        result = await session.execute(
            text(
                _JOB_SELECT
                + " WHERE idempotency_key = :idempotency_key"
                + " AND tenant_id = :tenant_id"
            ),
            {"idempotency_key": idempotency_key, "tenant_id": tenant_id},
        )
        return result.mappings().first()

    @staticmethod
    async def _find_active_job(
        session: AsyncSession,
        contract_id: str,
        tenant_id: str,
    ):
        result = await session.execute(
            text(
                """
                SELECT id::text AS id
                FROM review_jobs
                WHERE contract_id = :contract_id
                  AND tenant_id = :tenant_id
                  AND status IN ('queued', 'running')
                LIMIT 1
                """
            ),
            {"contract_id": contract_id, "tenant_id": tenant_id},
        )
        return result.mappings().first()

    @staticmethod
    async def _find_job_by_id(
        session: AsyncSession,
        job_id: str,
        tenant_id: str,
    ):
        result = await session.execute(
            text(
                _JOB_SELECT
                + " WHERE id = :job_id"
                + " AND tenant_id = :tenant_id"
            ),
            {"job_id": job_id, "tenant_id": tenant_id},
        )
        return result.mappings().first()

    @staticmethod
    async def _unreviewed_clause_ids(
        session: AsyncSession, job_id: str
    ) -> list[str]:
        result = await session.execute(
            text(
                """
                SELECT clause_id
                FROM review_clause_checkpoints
                WHERE job_id = :job_id
                  AND status IN ('queued', 'needs_retrieval')
                ORDER BY
                    created_at,
                    CAST(regexp_replace(clause_id, '[^0-9]', '', 'g') AS integer),
                    clause_id
                """
            ),
            {"job_id": job_id},
        )
        return [str(row["clause_id"]) for row in result.mappings().all()]

    @staticmethod
    async def _source_clauses(session: AsyncSession, job_id: str):
        result = await session.execute(
            text(
                """
                SELECT clause_id, clause_text, locator, status::text AS status
                FROM review_clause_checkpoints
                WHERE job_id = :job_id
                ORDER BY
                    created_at,
                    CAST(regexp_replace(clause_id, '[^0-9]', '', 'g') AS integer),
                    clause_id
                """
            ),
            {"job_id": job_id},
        )
        return result.mappings().all()

    @staticmethod
    async def _find_job_file(
        session: AsyncSession,
        job_id: str,
        tenant_id: str,
    ):
        result = await session.execute(
            text(
                """
                SELECT contract.object_key,
                       contract.filename,
                       contract.content_type
                FROM review_jobs job
                JOIN contracts contract
                  ON contract.id = job.contract_id
                 AND contract.tenant_id = job.tenant_id
                WHERE job.id = :job_id
                  AND job.tenant_id = :tenant_id
                """
            ),
            {"job_id": job_id, "tenant_id": tenant_id},
        )
        return result.mappings().first()

    @staticmethod
    async def _insert_contract(
        session: AsyncSession,
        actor: Actor,
        filename: str,
        content_type: str,
        content_sha256: str,
        object_key: str,
    ) -> str:
        result = await session.execute(
            text(
                """
                INSERT INTO contracts (
                    tenant_id, filename, content_type, content_sha256,
                    object_key, status, uploaded_by
                ) VALUES (
                    :tenant_id, :filename, :content_type, :content_sha256,
                    :object_key, 'uploaded',
                    (
                        SELECT id
                        FROM users
                        WHERE tenant_id = :tenant_id
                          AND external_subject = :external_subject
                        LIMIT 1
                    )
                )
                RETURNING id::text AS id
                """
            ),
            {
                "tenant_id": actor.tenant_id,
                "filename": filename,
                "content_type": content_type,
                "content_sha256": content_sha256,
                "object_key": object_key,
                "external_subject": actor.user_id,
            },
        )
        row = result.mappings().first()
        if row is None:
            raise RuntimeError("contract insert returned no id")
        return str(row["id"])

    @staticmethod
    async def _insert_job(
        session: AsyncSession,
        actor: Actor,
        contract_id: str,
        idempotency_key: str,
    ):
        result = await session.execute(
            text(
                """
                INSERT INTO review_jobs (
                    tenant_id, contract_id, requested_by, idempotency_key,
                    review_config, status
                ) VALUES (
                    :tenant_id, :contract_id,
                    (
                        SELECT id
                        FROM users
                        WHERE tenant_id = :tenant_id
                          AND external_subject = :external_subject
                        LIMIT 1
                    ),
                    :idempotency_key,
                    CAST(:review_config AS jsonb), 'queued'
                )
                RETURNING id::text AS id,
                          status::text AS status,
                          contract_id::text AS contract_id,
                          total_clauses,
                          completed_clauses,
                          failure_reason,
                          created_at
                """
            ),
            {
                "tenant_id": actor.tenant_id,
                "contract_id": contract_id,
                "idempotency_key": idempotency_key,
                "external_subject": actor.user_id,
                "review_config": json.dumps(
                    {"version": 1}, ensure_ascii=False
                ),
            },
        )
        return result.mappings().first()

    @staticmethod
    async def _findings(session: AsyncSession, job_id: str):
        result = await session.execute(
            text(
                """
                SELECT id::text AS id,
                       clause_id,
                       risk_level::text AS risk_level,
                       problem,
                       reason,
                       suggestion,
                       proposed_clause
                FROM review_findings
                WHERE job_id = :job_id AND status = 'passed'
                ORDER BY
                    created_at,
                    CAST(regexp_replace(clause_id, '[^0-9]', '', 'g') AS integer),
                    clause_id
                """
            ),
            {"job_id": job_id},
        )
        return result.mappings().all()

    @staticmethod
    async def _evidence(session: AsyncSession, finding_id: str):
        result = await session.execute(
            text(
                """
                SELECT id::text AS id,
                       exact_excerpt AS text,
                       rank,
                       source_snapshot,
                       source_content_sha256
                FROM evidence_snapshots
                WHERE finding_id = :finding_id
                ORDER BY rank
                """
            ),
            {"finding_id": finding_id},
        )
        return result.mappings().all()


def get_contract_service() -> ContractService:
    from app.tasks.review_contract import enqueue_review_task

    return ContractService(
        LocalObjectStore(REPOSITORY_ROOT / "storage" / "objects"),
        enqueue=enqueue_review_task,
    )
