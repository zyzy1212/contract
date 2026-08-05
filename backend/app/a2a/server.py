from __future__ import annotations

import base64
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

from fastapi import FastAPI, HTTPException

from a2a.server.apps.jsonrpc.fastapi_app import A2AFastAPIApplication
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers.request_handler import RequestHandler
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    Artifact,
    DataPart,
    DeleteTaskPushNotificationConfigParams,
    FileWithBytes,
    GetTaskPushNotificationConfigParams,
    ListTaskPushNotificationConfigParams,
    MessageSendParams,
    Part,
    Task,
    TaskIdParams,
    TaskNotFoundError,
    TaskPushNotificationConfig,
    TaskQueryParams,
    TaskState,
    TaskStatus,
    UnsupportedOperationError,
)
from a2a.utils.errors import ServerError

from app.auth import Actor
from app.config import REPOSITORY_ROOT
from app.contracts.service import ContractService, JobNotFound
from app.storage.objects import LocalObjectStore


CONTRACT_REVIEW_SKILL = AgentSkill(
    id="contract_review",
    name="合同审核",
    description="依据调用方租户可访问的知识库审核 PDF 或 DOCX 合同",
    input_modes=[
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ],
    output_modes=["application/json", "text/markdown"],
    tags=["contract-review"],
)

AGENT_CARD = AgentCard(
    name="合同审核 Agent",
    description="为调用方租户提供可追溯证据的合同审核任务",
    url="/",
    version="1.0.0",
    protocol_version="0.2.0",
    capabilities=AgentCapabilities(streaming=False, push_notifications=False),
    default_input_modes=[
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ],
    default_output_modes=["application/json", "text/markdown"],
    skills=[CONTRACT_REVIEW_SKILL],
)

TASK_STATE_BY_JOB = {
    "queued": TaskState.submitted,
    "running": TaskState.working,
    "partial": TaskState.completed,
    "complete": TaskState.completed,
    "failed": TaskState.failed,
}


def _default_contract_service() -> ContractService:
    from app.tasks.review_contract import enqueue_review_task

    return ContractService(
        LocalObjectStore(REPOSITORY_ROOT / "storage" / "objects"),
        enqueue=enqueue_review_task,
    )


def _actor_from_context(context: ServerCallContext | None) -> Actor:
    if context is None:
        raise HTTPException(status_code=401, detail="missing actor identity")
    headers = (context.state or {}).get("headers", {})
    user_id = headers.get("x-actor-user")
    tenant_id = headers.get("x-actor-tenant")
    role = headers.get("x-actor-role")
    if not user_id or not tenant_id or role not in {"customer", "admin"}:
        raise HTTPException(status_code=401, detail="missing actor identity")
    return Actor(user_id=user_id, tenant_id=tenant_id, role=role)  # type: ignore[arg-type]


def _task_from_job(job) -> Task:
    state = TASK_STATE_BY_JOB.get(job.status, TaskState.working)
    return Task(
        id=job.id,
        context_id=job.id,
        status=TaskStatus(
            state=state,
            timestamp=datetime.now(timezone.utc).isoformat(),
        ),
        artifacts=[
            Artifact(
                artifact_id=f"{job.id}-job",
                name="review_job",
                parts=[DataPart(data=job.model_dump(mode="json"))],
            )
        ],
    )


def _contract_file(params: MessageSendParams) -> tuple[str, str, bytes]:
    for part in params.message.parts:
        if isinstance(part, Part):
            part = part.root
        if getattr(part, "kind", None) != "file":
            continue
        file = getattr(part, "file", None)
        if file is None:
            continue
        if not isinstance(file, FileWithBytes):
            raise HTTPException(
                status_code=422,
                detail="contract file URIs are not accepted; send file bytes",
            )
        try:
            data = base64.b64decode(file.bytes, validate=True)
        except (ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=422, detail="contract file bytes are not valid base64"
            ) from exc
        return (
            file.name or "contract",
            file.mime_type or "application/pdf",
            data,
        )
    raise HTTPException(
        status_code=422, detail="contract file part is required"
    )


class ContractReviewRequestHandler(RequestHandler):
    def __init__(self, service_factory=None) -> None:
        self._service_factory = service_factory or _default_contract_service

    async def on_get_task(
        self,
        params: TaskQueryParams,
        context: ServerCallContext | None = None,
    ) -> Task | None:
        actor = _actor_from_context(context)
        service = self._service_factory()
        try:
            job = await service.get_review(actor, params.id)
        except JobNotFound as exc:
            raise ServerError(error=TaskNotFoundError()) from exc
        return _task_from_job(job)

    async def on_cancel_task(
        self,
        params: TaskIdParams,
        context: ServerCallContext | None = None,
    ) -> Task | None:
        raise ServerError(error=UnsupportedOperationError())

    async def on_message_send(
        self,
        params: MessageSendParams,
        context: ServerCallContext | None = None,
    ) -> Task:
        actor = _actor_from_context(context)
        claimed_tenant = (
            params.message.metadata or {}
        ).get("tenant_id")
        if claimed_tenant and claimed_tenant != actor.tenant_id:
            raise HTTPException(
                status_code=403, detail="cross-tenant access denied"
            )
        filename, content_type, data = _contract_file(params)
        if not data:
            raise HTTPException(
                status_code=422, detail="contract file is empty"
            )
        service = self._service_factory()
        job = await service.create_review(
            actor,
            filename,
            content_type,
            BytesIO(data),
        )
        return _task_from_job(job)

    async def on_message_send_stream(
        self,
        params: MessageSendParams,
        context: ServerCallContext | None = None,
    ) -> AsyncGenerator[Any, None]:
        raise ServerError(error=UnsupportedOperationError())
        yield  # pragma: no cover

    async def on_set_task_push_notification_config(
        self,
        params: TaskPushNotificationConfig,
        context: ServerCallContext | None = None,
    ) -> TaskPushNotificationConfig:
        raise ServerError(error=UnsupportedOperationError())

    async def on_get_task_push_notification_config(
        self,
        params: TaskIdParams | GetTaskPushNotificationConfigParams,
        context: ServerCallContext | None = None,
    ) -> TaskPushNotificationConfig:
        raise ServerError(error=UnsupportedOperationError())

    async def on_resubscribe_to_task(
        self,
        params: TaskIdParams,
        context: ServerCallContext | None = None,
    ) -> AsyncGenerator[Any, None]:
        raise ServerError(error=UnsupportedOperationError())
        yield  # pragma: no cover

    async def on_list_task_push_notification_config(
        self,
        params: ListTaskPushNotificationConfigParams,
        context: ServerCallContext | None = None,
    ) -> list[TaskPushNotificationConfig]:
        raise ServerError(error=UnsupportedOperationError())

    async def on_delete_task_push_notification_config(
        self,
        params: DeleteTaskPushNotificationConfigParams,
        context: ServerCallContext | None = None,
    ) -> None:
        raise ServerError(error=UnsupportedOperationError())


def build_a2a_application(
    handler: RequestHandler | None = None,
) -> A2AFastAPIApplication:
    return A2AFastAPIApplication(
        agent_card=AGENT_CARD,
        http_handler=handler or ContractReviewRequestHandler(),
    )


def add_a2a_routes(app: FastAPI, handler: RequestHandler | None = None) -> None:
    build_a2a_application(handler).add_routes_to_app(app, rpc_url="/a2a")
