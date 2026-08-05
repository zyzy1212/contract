from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import date
from typing import Any, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, PendingRollbackError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Actor, require_tenant
from app.common.errors import InputValidationError, KnowledgeConflict, TenantAccessError
from app.db import tenant_transaction
from app.retrieval.models import (
    EvidenceCandidate,
    RetrievalChannelFailure,
    RetrievalChannels,
    TransientRetrievalError,
)

from .ingestion import scope_for_source
from .models import (
    IngestionBatch,
    KnowledgeChunk,
    KnowledgeChunkRecord,
    KnowledgeDocumentRecord,
    KnowledgePersistResult,
    KnowledgeScope,
    SourceType,
)


TransactionFactory = Callable[..., AbstractAsyncContextManager[AsyncSession]]


def _database_failure_scope(error: DBAPIError) -> str:
    sqlstate = str(getattr(error.orig, "sqlstate", "") or "")
    message = str(error.orig).lower()
    if (
        sqlstate == "57014"
        and "statement timeout" in message
        and not error.connection_invalidated
    ):
        return "statement"
    if (
        error.connection_invalidated
        or sqlstate.startswith("08")
        or sqlstate.startswith("40")
        or sqlstate.startswith("57")
    ):
        return "transaction"
    return "unknown"


async def _execute_retrieval_query(
    session: AsyncSession,
    statement: Any,
    parameters: dict[str, object],
    channel: str,
) -> Any:
    try:
        begin_nested = getattr(session, "begin_nested", None)
        if begin_nested is None:
            return await session.execute(statement, parameters)
        async with begin_nested():
            return await session.execute(statement, parameters)
    except TransientRetrievalError:
        raise
    except PendingRollbackError as exc:
        raise TransientRetrievalError(
            "retrieval transaction is unavailable", channel=None
        ) from exc
    except DBAPIError as exc:
        failure_scope = _database_failure_scope(exc)
        if failure_scope == "statement":
            raise TransientRetrievalError(
                f"{channel} retrieval temporarily unavailable", channel=channel
            ) from exc
        if failure_scope == "transaction":
            raise TransientRetrievalError(
                "retrieval transaction is temporarily unavailable", channel=None
            ) from exc
        raise


def _validated_tenant(actor: Actor, requested_tenant_id: str | None) -> str:
    if not isinstance(actor, Actor):
        raise InputValidationError("repository access requires a validated Actor")
    return require_tenant(actor, requested_tenant_id)


def _document_record(row: Any) -> KnowledgeDocumentRecord:
    return KnowledgeDocumentRecord(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]) if row["tenant_id"] is not None else None,
        scope=cast(KnowledgeScope, str(row["scope"])),
        title=str(row["title"]),
        source_type=cast(SourceType, str(row["source_type"])),
        content_sha256=str(row["content_sha256"]),
        object_key=str(row["object_key"]),
        source_metadata=dict(row["source_metadata"]),
    )


def _chunk_record(row: Any) -> KnowledgeChunkRecord:
    return KnowledgeChunkRecord(
        id=str(row["id"]),
        document_id=str(row["document_id"]),
        parent_chunk_id=(
            str(row["parent_chunk_id"])
            if row["parent_chunk_id"] is not None
            else None
        ),
        tenant_id=str(row["tenant_id"]) if row["tenant_id"] is not None else None,
        scope=cast(KnowledgeScope, str(row["scope"])),
        content=str(row["content"]),
        content_sha256=str(row["content_sha256"]),
        source_snapshot=dict(row["source_snapshot"]),
    )


class KnowledgeRepository:
    def __init__(
        self, *, transaction_factory: TransactionFactory = tenant_transaction
    ):
        self.transaction_factory = transaction_factory

    async def list_visible_chunks(
        self,
        actor: Actor,
        requested_tenant_id: str | None = None,
        *,
        limit: int = 100,
    ) -> list[KnowledgeChunkRecord]:
        tenant_id = _validated_tenant(actor, requested_tenant_id)
        if limit < 1 or limit > 1000:
            raise InputValidationError("limit must be between 1 and 1000")
        async with self.transaction_factory(
            actor,
            requested_tenant_id,
            enable_public_knowledge_write=False,
        ) as session:
            result = await session.execute(
                text(
                    """
                    SELECT
                        chunk.id::text AS id,
                        chunk.document_id::text AS document_id,
                        chunk.parent_chunk_id::text AS parent_chunk_id,
                        chunk.tenant_id::text AS tenant_id,
                        chunk.scope::text AS scope,
                        chunk.content,
                        chunk.content_sha256,
                        chunk.source_snapshot
                    FROM knowledge_chunks chunk
                    WHERE
                        chunk.scope = 'public'
                        OR (
                            chunk.tenant_id = :tenant_id
                            AND chunk.scope IN ('firm', 'tenant_private')
                        )
                    ORDER BY chunk.document_id, chunk.ordinal
                    LIMIT :limit
                    """
                ),
                {"tenant_id": tenant_id, "limit": limit},
            )
            return [_chunk_record(row) for row in result.mappings().all()]

    async def retrieve_hybrid_candidates(
        self,
        actor: Actor,
        *,
        query_embedding: list[float] | None,
        embedding_model: str | None,
        keyword_search_text: str,
        excluded_chunk_ids: list[str],
        limit: int,
        embedding_dimension: int = 512,
        timeout_ms: int = 2_000,
        as_of: date | None = None,
    ) -> RetrievalChannels:
        """Run indexed vector and Chinese-aware keyword recall in one tenant transaction.

        Keyword ranking uses PostgreSQL ``ts_rank_cd`` over a stored, GIN-indexed
        ``simple`` tsvector. RRF is intentionally applied by the retrieval layer.
        """
        tenant_id = _validated_tenant(actor, None)
        retrieval_date = as_of or date.today()
        if not isinstance(retrieval_date, date):
            raise InputValidationError("as_of must be a retrieval date")
        if (
            not isinstance(embedding_dimension, int)
            or isinstance(embedding_dimension, bool)
            or embedding_dimension < 1
        ):
            raise InputValidationError("query embedding dimension is invalid")
        if (query_embedding is None) != (embedding_model is None):
            raise InputValidationError(
                "query embedding and embedding_model must be provided together"
            )
        if query_embedding is not None:
            if len(query_embedding) != embedding_dimension:
                raise InputValidationError("query embedding dimension is invalid")
            if any(not math.isfinite(float(value)) for value in query_embedding):
                raise InputValidationError(
                    "query embedding must contain only finite values"
                )
            if (
                embedding_model is None
                or not embedding_model.strip()
                or len(embedding_model) > 255
            ):
                raise InputValidationError(
                    "embedding_model must be between 1 and 255 characters"
                )
        if not keyword_search_text.strip() or len(keyword_search_text) > 8_192:
            raise InputValidationError("keyword search text must be between 1 and 8192 characters")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 200:
            raise InputValidationError("limit must be between 1 and 200")
        if (
            not isinstance(timeout_ms, int)
            or isinstance(timeout_ms, bool)
            or timeout_ms < 1
            or timeout_ms > 10_000
        ):
            raise InputValidationError("timeout_ms must be between 1 and 10000")
        if len(excluded_chunk_ids) > 1_000:
            raise InputValidationError("excluded chunk IDs must not exceed 1000")
        if any(not isinstance(chunk_id, str) or not chunk_id.strip() for chunk_id in excluded_chunk_ids):
            raise InputValidationError("excluded chunk IDs must not be empty")
        try:
            for chunk_id in excluded_chunk_ids:
                UUID(chunk_id)
        except (ValueError, AttributeError, TypeError) as exc:
            raise InputValidationError("excluded chunk IDs must be valid UUIDs") from exc
        exclusions = list(dict.fromkeys(excluded_chunk_ids))
        keyword_terms = re.findall(r"[A-Za-z0-9_]+|[\u3400-\u9fff]+", keyword_search_text.lower())
        if not keyword_terms:
            raise InputValidationError("keyword search text has no searchable terms")

        shared_filters = """
            (chunk.scope = 'public' OR (
                chunk.tenant_id = :tenant_id
                AND chunk.scope IN ('firm', 'tenant_private')
            ))
            AND document.status = 'active'
            AND (document.effective_date IS NULL OR document.effective_date <= :as_of)
            AND (document.expiry_date IS NULL OR document.expiry_date >= :as_of)
            AND chunk.id <> ALL(CAST(:excluded_chunk_ids AS uuid[]))
        """
        shared_parameters: dict[str, object] = {
            "tenant_id": tenant_id,
            "excluded_chunk_ids": exclusions,
            "keyword_tsquery": " | ".join(keyword_terms),
            "limit": limit,
            "as_of": retrieval_date,
        }
        async with self.transaction_factory(
            actor, None, enable_public_knowledge_write=False
        ) as session:
            await session.execute(
                text("SELECT set_config('statement_timeout', :timeout, true)"),
                {"timeout": f"{timeout_ms}ms"},
            )
            failures: list[RetrievalChannelFailure] = []
            vector_result = None
            if query_embedding is not None:
                vector_parameters = {
                    **shared_parameters,
                    "query_embedding": json.dumps(
                        [float(value) for value in query_embedding]
                    ),
                    "embedding_model": embedding_model,
                    "embedding_dimension": embedding_dimension,
                }
                try:
                    vector_result = await _execute_retrieval_query(
                        session,
                        text(
                            f"""
                            SELECT chunk.id::text AS id,
                                   chunk.document_id::text AS document_id,
                                   chunk.scope::text AS knowledge_scope,
                                   chunk.tenant_id::text AS tenant_id,
                                   chunk.content, chunk.content_sha256,
                                   chunk.source_snapshot,
                                   document.content_sha256 AS source_document_sha256,
                                   1 - (chunk.embedding <=> CAST(:query_embedding AS vector)) AS score
                            FROM knowledge_chunks chunk
                            JOIN knowledge_documents document ON document.id = chunk.document_id
                            WHERE {shared_filters}
                              AND chunk.embedding IS NOT NULL
                              AND chunk.embedding_model = :embedding_model
                              AND vector_dims(chunk.embedding) = :embedding_dimension
                            ORDER BY chunk.embedding <=> CAST(:query_embedding AS vector), chunk.id
                            LIMIT :limit
                            """
                        ),
                        vector_parameters,
                        "vector",
                    )
                except TransientRetrievalError as exc:
                    if exc.channel != "vector":
                        raise
                    failures.append(RetrievalChannelFailure("vector"))
            try:
                keyword_result = await _execute_retrieval_query(
                    session,
                    text(
                    f"""
                    SELECT chunk.id::text AS id,
                           chunk.document_id::text AS document_id,
                           chunk.scope::text AS knowledge_scope,
                           chunk.tenant_id::text AS tenant_id,
                           chunk.content, chunk.content_sha256,
                           chunk.source_snapshot,
                           document.content_sha256 AS source_document_sha256,
                           ts_rank_cd(
                               chunk.keyword_tsv,
                               to_tsquery('simple', :keyword_tsquery)
                           ) AS score
                    FROM knowledge_chunks chunk
                    JOIN knowledge_documents document ON document.id = chunk.document_id
                    WHERE {shared_filters}
                      AND chunk.keyword_tsv @@ to_tsquery('simple', :keyword_tsquery)
                    ORDER BY score DESC, chunk.id
                    LIMIT :limit
                    """
                    ),
                    shared_parameters,
                    "keyword",
                )
            except TransientRetrievalError as exc:
                if exc.channel != "keyword":
                    raise
                keyword_result = None
                failures.append(RetrievalChannelFailure("keyword"))
        return RetrievalChannels(
            vector=tuple(
                self._retrieval_candidate(
                    row, "vector", rank, tenant_id, retrieval_date
                )
                for rank, row in enumerate(
                    vector_result.mappings().all() if vector_result is not None else (),
                    start=1,
                )
            ),
            keyword=tuple(
                self._retrieval_candidate(
                    row, "keyword", rank, tenant_id, retrieval_date
                )
                for rank, row in enumerate(
                    keyword_result.mappings().all() if keyword_result is not None else (),
                    start=1,
                )
            ),
            failures=tuple(failures),
        )

    @staticmethod
    def _retrieval_candidate(
        row: Any,
        channel: str,
        rank: int,
        applicable_tenant_id: str,
        as_of: date,
    ) -> EvidenceCandidate:
        snapshot = dict(row["source_snapshot"])
        source_text_sha256 = str(snapshot.get("source_text_sha256") or "")
        if not source_text_sha256:
            raise RuntimeError("retrieved knowledge is missing source snapshot identity")
        try:
            source_document_sha256 = row["source_document_sha256"]
        except KeyError:
            source_document_sha256 = snapshot.get("source_document_sha256")
        if source_document_sha256:
            snapshot["source_document_sha256"] = str(source_document_sha256)
        score = float(row["score"])
        return EvidenceCandidate(
            chunk_id=str(row["id"]),
            document_id=str(row["document_id"]),
            text=str(row["content"]),
            source_snapshot_id=str(row["content_sha256"]),
            source_text_sha256=source_text_sha256,
            source_snapshot=snapshot,
            score=score,
            rank=rank,
            channel_scores={channel: score},
            channel_ranks={channel: rank},
            knowledge_scope=str(row["knowledge_scope"]),
            tenant_id=(
                str(row["tenant_id"]) if row["tenant_id"] is not None else None
            ),
            applicable_tenant_id=applicable_tenant_id,
            as_of=as_of,
        )

    async def find_existing(
        self,
        actor: Actor,
        batch: IngestionBatch,
        requested_tenant_id: str | None = None,
    ) -> KnowledgeDocumentRecord | None:
        stored_tenant_id, _ = self._validated_write_context(
            actor, batch, requested_tenant_id
        )
        async with self.transaction_factory(
            actor,
            requested_tenant_id,
            enable_public_knowledge_write=False,
        ) as session:
            existing = await session.execute(
                text(
                    """
                    SELECT
                        id::text AS id,
                        tenant_id::text AS tenant_id,
                        scope::text AS scope,
                        title,
                        source_type::text AS source_type,
                        content_sha256,
                        object_key,
                        source_metadata
                    FROM knowledge_documents
                    WHERE content_sha256 = :content_sha256
                      AND scope = :scope
                      AND version = :version
                      AND (
                        (CAST(:tenant_id AS uuid) IS NULL AND tenant_id IS NULL)
                        OR tenant_id = CAST(:tenant_id AS uuid)
                      )
                    """
                ),
                {
                    "content_sha256": batch.file_sha256,
                    "scope": batch.scope,
                    "version": batch.source.version,
                    "tenant_id": stored_tenant_id,
                },
            )
            row = existing.mappings().first()
            if row is None:
                return None
            return self._matching_existing(row, batch, stored_tenant_id)

    async def persist(
        self,
        actor: Actor,
        batch: IngestionBatch,
        requested_tenant_id: str | None = None,
    ) -> KnowledgePersistResult:
        stored_tenant_id, created_by = self._validated_write_context(
            actor, batch, requested_tenant_id
        )

        source_metadata = self._source_metadata(batch)
        async with self.transaction_factory(
            actor,
            requested_tenant_id,
            enable_public_knowledge_write=batch.scope == "public",
        ) as session:
            inserted = await session.execute(
                text(
                    """
                    INSERT INTO knowledge_documents (
                        id, tenant_id, scope, title, source_type,
                        issuing_authority, document_number, source_url,
                        version, effective_date, expiry_date,
                        content_sha256, object_key, source_metadata, created_by
                    ) VALUES (
                        :id, :tenant_id, :scope, :title, :source_type,
                        :issuing_authority, :document_number, :source_url,
                        :version, :effective_date, :expiry_date,
                        :content_sha256, :object_key,
                        CAST(:source_metadata AS jsonb), :created_by
                    )
                    ON CONFLICT DO NOTHING
                    RETURNING
                        id::text AS id,
                        tenant_id::text AS tenant_id,
                        scope::text AS scope,
                        title,
                        source_type::text AS source_type,
                        content_sha256,
                        object_key,
                        source_metadata
                    """
                ),
                {
                    "id": batch.document_id,
                    "tenant_id": stored_tenant_id,
                    "scope": batch.scope,
                    "title": batch.source.title,
                    "source_type": batch.source.source_type,
                    "issuing_authority": batch.source.issuing_authority,
                    "document_number": batch.source.document_number,
                    "source_url": batch.source.source_url,
                    "version": batch.source.version,
                    "effective_date": batch.source.effective_date,
                    "expiry_date": batch.source.expiry_date,
                    "content_sha256": batch.file_sha256,
                    "object_key": batch.object_key,
                    "source_metadata": json.dumps(
                        source_metadata, ensure_ascii=False, sort_keys=True
                    ),
                    "created_by": created_by,
                },
            )
            row = inserted.mappings().first()
            if row is None:
                existing = await session.execute(
                    text(
                        """
                        SELECT
                            id::text AS id,
                            tenant_id::text AS tenant_id,
                            scope::text AS scope,
                            title,
                            source_type::text AS source_type,
                            content_sha256,
                            object_key,
                            source_metadata
                        FROM knowledge_documents
                        WHERE content_sha256 = :content_sha256
                          AND scope = :scope
                          AND version = :version
                          AND (
                            (CAST(:tenant_id AS uuid) IS NULL AND tenant_id IS NULL)
                            OR tenant_id = CAST(:tenant_id AS uuid)
                          )
                        """
                    ),
                    {
                        "content_sha256": batch.file_sha256,
                        "scope": batch.scope,
                        "version": batch.source.version,
                        "tenant_id": stored_tenant_id,
                    },
                )
                row = existing.mappings().first()
                if row is None:
                    raise RuntimeError("document conflict could not be resolved")
                record = self._matching_existing(row, batch, stored_tenant_id)
                return KnowledgePersistResult(record=record, inserted=False)

            for chunk in batch.parent_chunks:
                await self._insert_chunk(
                    session, chunk, batch.scope, stored_tenant_id
                )
            for chunk in batch.child_chunks:
                await self._insert_chunk(
                    session, chunk, batch.scope, stored_tenant_id
                )
            return KnowledgePersistResult(record=_document_record(row), inserted=True)

    @staticmethod
    def _validated_write_context(
        actor: Actor,
        batch: IngestionBatch,
        requested_tenant_id: str | None,
    ) -> tuple[str | None, str | None]:
        tenant_id = _validated_tenant(actor, requested_tenant_id)
        expected_scope = scope_for_source(batch.source.source_type)
        if batch.scope != expected_scope:
            raise InputValidationError("batch scope does not match source type")
        if batch.scope == "public":
            if actor.role != "admin":
                raise TenantAccessError(
                    "public knowledge writes require an administrator"
                )
            if batch.tenant_id is not None:
                raise TenantAccessError("public batch tenant must be null")
            return None, None
        if batch.tenant_id != tenant_id:
            raise TenantAccessError("batch tenant does not match validated actor tenant")
        created_by = actor.user_id if actor.tenant_id == tenant_id else None
        return tenant_id, created_by

    @classmethod
    def _matching_existing(
        cls,
        row: Any,
        batch: IngestionBatch,
        tenant_id: str | None,
    ) -> KnowledgeDocumentRecord:
        record = _document_record(row)
        expected = KnowledgeDocumentRecord(
            id=batch.document_id,
            tenant_id=tenant_id,
            scope=batch.scope,
            title=batch.source.title,
            source_type=batch.source.source_type,
            content_sha256=batch.file_sha256,
            object_key=batch.object_key,
            source_metadata=cls._source_metadata(batch),
        )
        if record != expected:
            raise KnowledgeConflict(
                "existing knowledge has different immutable provenance"
            )
        return record

    @staticmethod
    def _source_metadata(batch: IngestionBatch) -> dict[str, object]:
        source = batch.source
        return {
            "title": source.title,
            "source_type": source.source_type,
            "issuing_authority": source.issuing_authority,
            "document_number": source.document_number,
            "source_url": source.source_url,
            "version": source.version,
            "effective_date": (
                source.effective_date.isoformat() if source.effective_date else None
            ),
            "expiry_date": source.expiry_date.isoformat() if source.expiry_date else None,
            "source_filename": (
                batch.parent_chunks[0].source_snapshot["source_filename"]
                if batch.parent_chunks
                else source.source_filename
            ),
            "file_sha256": batch.file_sha256,
        }

    @staticmethod
    async def _insert_chunk(
        session: AsyncSession,
        chunk: KnowledgeChunk,
        scope: KnowledgeScope,
        tenant_id: str | None,
    ) -> None:
        inserted = await session.execute(
            text(
                """
                INSERT INTO knowledge_chunks (
                    id, tenant_id, scope, document_id, parent_chunk_id,
                    ordinal, content, content_sha256, article_number,
                    section_title, page_start, page_end, paragraph_index,
                    bboxes, source_snapshot, keyword_search_text,
                    embedding_model, embedding
                ) VALUES (
                    :id, :tenant_id, :scope, :document_id, :parent_chunk_id,
                    :ordinal, :content, :content_sha256, :article_number,
                    :section_title, :page_start, :page_end, :paragraph_index,
                    CAST(:bboxes AS jsonb), CAST(:source_snapshot AS jsonb),
                    :keyword_search_text,
                    :embedding_model, CAST(:embedding AS vector)
                )
                RETURNING id::text AS id
                """
            ),
            {
                "id": chunk.id,
                "tenant_id": tenant_id,
                "scope": scope,
                "document_id": chunk.document_id,
                "parent_chunk_id": chunk.parent_chunk_id,
                "ordinal": chunk.ordinal,
                "content": chunk.content,
                "content_sha256": chunk.content_sha256,
                "article_number": chunk.article_number,
                "section_title": chunk.section_title,
                "page_start": chunk.page_start,
                "page_end": chunk.page_end,
                "paragraph_index": chunk.paragraph_index,
                "bboxes": json.dumps(chunk.bboxes, ensure_ascii=False),
                "source_snapshot": json.dumps(
                    chunk.source_snapshot, ensure_ascii=False, sort_keys=True
                ),
                "keyword_search_text": chunk.keyword_search_text,
                "embedding_model": chunk.embedding_model,
                "embedding": (
                    json.dumps(chunk.embedding) if chunk.embedding is not None else None
                ),
            },
        )
        row = inserted.mappings().first()
        if row is None or str(row["id"]) != chunk.id:
            raise RuntimeError(f"chunk insert did not persist expected id {chunk.id}")
