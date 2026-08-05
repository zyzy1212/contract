from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.admin.service import default_ingestion_service
from app.auth import Actor
from app.knowledge.models import KnowledgeSource
from app.tasks.celery_app import celery_app


def enqueue_knowledge_ingestion(
    source: KnowledgeSource,
    actor: Actor,
    path: Path,
    requested_tenant_id: str | None = None,
) -> None:
    ingest_knowledge_task.delay(
        source.model_dump(mode="json"),
        {
            "user_id": actor.user_id,
            "tenant_id": actor.tenant_id,
            "role": actor.role,
            "allowed_tenants": sorted(actor.allowed_tenants),
        },
        str(path),
        requested_tenant_id,
    )


@celery_app.task(
    name="contract_review.ingest_knowledge",
    bind=True,
    max_retries=3,
)
def ingest_knowledge_task(
    self,
    source_data: dict[str, Any],
    actor_data: dict[str, Any],
    path: str,
    requested_tenant_id: str | None = None,
) -> str:
    source = KnowledgeSource.model_validate(source_data)
    actor = Actor(**actor_data)
    try:
        record = asyncio.run(
            default_ingestion_service().ingest(
                Path(path),
                source,
                actor,
                requested_tenant_id,
            )
        )
        Path(path).unlink(missing_ok=True)
        return record.id
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            Path(path).unlink(missing_ok=True)
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)
