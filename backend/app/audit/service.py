from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text

from app.auth import Actor
from app.db import tenant_transaction


SENSITIVE_KEYS = {"api_key", "authorization", "password", "token"}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


@dataclass(frozen=True)
class AuditEvent:
    id: str
    tenant_id: str
    event_type: str
    entity_type: str
    entity_id: str | None
    payload: dict[str, Any]
    payload_json: str
    created_at: datetime


class AuditService:
    def __init__(self, transaction_factory=tenant_transaction) -> None:
        self._transaction_factory = transaction_factory

    async def record(
        self,
        actor: Actor,
        event_type: str,
        payload: dict[str, Any],
        *,
        entity_type: str = "",
        entity_id: str | None = None,
    ) -> AuditEvent:
        redacted = redact(payload)
        async with self._transaction_factory(actor) as session:
            result = await session.execute(
                text(
                    """
                    INSERT INTO audit_events (
                        tenant_id, event_type, entity_type, entity_id, payload
                    ) VALUES (
                        :tenant_id, :event_type, :entity_type, :entity_id,
                        CAST(:payload AS jsonb)
                    )
                    RETURNING id::text AS id, created_at
                    """
                ),
                {
                    "tenant_id": actor.tenant_id,
                    "event_type": event_type,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "payload": json.dumps(redacted, ensure_ascii=False),
                },
            )
            row = result.mappings().first()
            if row is None:
                raise RuntimeError("audit insert returned no id")
            return AuditEvent(
                id=str(row["id"]),
                tenant_id=actor.tenant_id,
                event_type=event_type,
                entity_type=entity_type,
                entity_id=entity_id,
                payload=redacted,
                payload_json=json.dumps(redacted, ensure_ascii=False),
                created_at=row["created_at"],
            )
