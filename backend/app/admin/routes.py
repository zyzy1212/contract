from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field, ValidationError

from app.admin.service import AdminService, extract_law_metadata
from app.auth import Actor, require_admin
from app.common.errors import DomainError
from app.config import get_settings
from app.knowledge.models import KnowledgeSource


router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])


def get_admin_service() -> AdminService:
    return AdminService()


class TenantCreate(BaseModel):
    slug: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=255)


class UserAssign(BaseModel):
    external_subject: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=1, max_length=320)
    role: Literal["customer", "admin"] = "customer"


@router.get("/tenants")
async def list_tenants(
    actor: Actor = Depends(require_admin),
    service: AdminService = Depends(get_admin_service),
):
    return await service.list_tenants(actor)


@router.post("/tenants", status_code=201)
async def create_tenant(
    payload: TenantCreate,
    actor: Actor = Depends(require_admin),
    service: AdminService = Depends(get_admin_service),
):
    return await service.create_tenant(actor, payload.slug, payload.name)


@router.post("/tenants/{tenant_id}/users", status_code=201)
async def assign_user(
    tenant_id: str,
    payload: UserAssign,
    actor: Actor = Depends(require_admin),
    service: AdminService = Depends(get_admin_service),
):
    return await service.assign_user(
        actor,
        tenant_id,
        payload.external_subject,
        payload.email,
        payload.role,
    )


@router.get("/knowledge")
async def list_knowledge(
    scope: str | None = None,
    status: str | None = None,
    actor: Actor = Depends(require_admin),
    service: AdminService = Depends(get_admin_service),
):
    return await service.list_knowledge(actor, scope=scope, status=status)


@router.post("/knowledge/metadata")
async def extract_knowledge_metadata(
    file: UploadFile = File(...),
    actor: Actor = Depends(require_admin),
):
    data = await file.read()
    if len(data) > get_settings().document_parser_max_source_bytes:
        raise HTTPException(
            status_code=422,
            detail="knowledge source must not exceed the configured size limit",
        )
    return extract_law_metadata(
        file.filename or "source",
        data,
        head_pages=10,
        tail_pages=10,
    )


@router.post("/knowledge", status_code=202)
async def upload_knowledge(
    file: UploadFile = File(...),
    title: str = Form(""),
    source_type: Literal["law", "firm_rule", "tenant_private"] = Form(...),
    issuing_authority: str = Form(""),
    document_number: str = Form(""),
    source_url: str = Form(""),
    article_number: str | None = Form(None),
    section_title: str | None = Form(None),
    paragraph_index: int | None = Form(None),
    page_start: int | None = Form(None),
    page_end: int | None = Form(None),
    version: str = Form(""),
    effective_date: date | None = Form(None),
    expiry_date: date | None = Form(None),
    actor: Actor = Depends(require_admin),
    service: AdminService = Depends(get_admin_service),
):
    data = await file.read()
    if len(data) > get_settings().document_parser_max_source_bytes:
        raise HTTPException(
            status_code=422,
            detail="knowledge source must not exceed the configured size limit",
        )
    extracted = extract_law_metadata(file.filename or "", data)
    try:
        source = KnowledgeSource(
            title=title or extracted.get("title", ""),
            source_type=source_type,
            issuing_authority=(
                issuing_authority
                or extracted.get("issuing_authority", "")
            ),
            document_number=document_number,
            source_url=source_url,
            article_number=article_number,
            section_title=section_title,
            paragraph_index=paragraph_index,
            page_start=page_start,
            page_end=page_end,
            version=version or extracted.get("version", ""),
            effective_date=effective_date
            or (
                date.fromisoformat(extracted["effective_date"])
                if extracted.get("effective_date")
                else None
            ),
            expiry_date=expiry_date,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        return await service.ingest(
            actor, source, file.filename or "source", data
        )
    except (DomainError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/knowledge/{document_id}/deactivate")
async def deactivate_knowledge(
    document_id: str,
    actor: Actor = Depends(require_admin),
    service: AdminService = Depends(get_admin_service),
):
    return await service.deactivate(actor, document_id)


@router.post("/knowledge/{document_id}/activate")
async def activate_knowledge(
    document_id: str,
    actor: Actor = Depends(require_admin),
    service: AdminService = Depends(get_admin_service),
):
    return await service.activate(actor, document_id)


@router.post("/knowledge/{document_id}/archive")
async def archive_knowledge(
    document_id: str,
    actor: Actor = Depends(require_admin),
    service: AdminService = Depends(get_admin_service),
):
    return await service.archive(actor, document_id)


@router.post("/knowledge/{document_id}/restore")
async def restore_knowledge(
    document_id: str,
    actor: Actor = Depends(require_admin),
    service: AdminService = Depends(get_admin_service),
):
    return await service.restore(actor, document_id)
