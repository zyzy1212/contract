from __future__ import annotations

from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from sqlalchemy.exc import IntegrityError

from app.auth import Actor, current_actor
from app.common.errors import InputValidationError
from app.contracts.service import (
    ActiveJobConflict,
    ContractService,
    JobNotFound,
    get_contract_service,
)
from app.reports.service import build_report


router = APIRouter(prefix="/api")

SUPPORTED_CONTRACT_CONTENT_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


@router.post("/contracts", status_code=202)
async def upload_contract(
    file: UploadFile = File(...),
    actor: Actor = Depends(current_actor),
    service: ContractService = Depends(get_contract_service),
):
    if file.content_type not in SUPPORTED_CONTRACT_CONTENT_TYPES:
        raise HTTPException(status_code=415, detail="only PDF and DOCX are supported")
    try:
        job = await service.create_review(
            actor,
            file.filename or "contract",
            file.content_type,
            file,
        )
    except InputValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        if "tenant_id_fkey" not in str(exc):
            raise
        raise HTTPException(
            status_code=409,
            detail="tenant identity is not provisioned; seed or create the tenant first",
        ) from exc
    return job.model_dump()


@router.get("/reviews")
async def list_reviews(
    actor: Actor = Depends(current_actor),
    service: ContractService = Depends(get_contract_service),
):
    history = await service.list_review_history(actor)
    return [item.model_dump() for item in history]


@router.get("/reviews/{job_id}")
async def get_review(
    job_id: str,
    actor: Actor = Depends(current_actor),
    service: ContractService = Depends(get_contract_service),
):
    try:
        return (await service.get_review(actor, job_id)).model_dump()
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail="review job does not exist") from exc


@router.post("/reviews/{job_id}/retry")
async def retry_review(
    job_id: str,
    actor: Actor = Depends(current_actor),
    service: ContractService = Depends(get_contract_service),
):
    try:
        job = await service.retry_review(actor, job_id)
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail="review job does not exist") from exc
    except InputValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return job.model_dump()


@router.post("/reviews/{job_id}/rerun", status_code=202)
async def rerun_review(
    job_id: str,
    actor: Actor = Depends(current_actor),
    service: ContractService = Depends(get_contract_service),
):
    try:
        job = await service.rerun_review(actor, job_id)
    except JobNotFound as exc:
        raise HTTPException(
            status_code=404, detail="review job does not exist"
        ) from exc
    except ActiveJobConflict as exc:
        raise HTTPException(
            status_code=409,
            detail="contract already has an active review job",
        ) from exc
    return job.model_dump()


@router.get("/reviews/{job_id}/file")
async def get_contract_file(
    job_id: str,
    actor: Actor = Depends(current_actor),
    service: ContractService = Depends(get_contract_service),
):
    try:
        filename, content_type, data = await service.get_contract_file(
            actor, job_id
        )
    except JobNotFound as exc:
        raise HTTPException(
            status_code=404, detail="review job does not exist"
        ) from exc
    ascii_filename = filename.encode("ascii", "ignore").decode() or "contract"
    return Response(
        content=data,
        media_type=content_type,
        headers={
            "Content-Disposition": (
                f'inline; filename="{ascii_filename}"; '
                f"filename*=UTF-8''{quote(filename)}"
            )
        },
    )


@router.get("/reviews/{job_id}/progress")
async def get_review_progress(
    job_id: str,
    actor: Actor = Depends(current_actor),
    service: ContractService = Depends(get_contract_service),
):
    try:
        detail = await service.get_review(actor, job_id)
    except JobNotFound as exc:
        raise HTTPException(status_code=404, detail="review job does not exist") from exc
    return {
        "id": detail.id,
        "status": detail.status,
        "completed_clauses": detail.completed_clauses,
        "total_clauses": detail.total_clauses,
        "unreviewed_clause_ids": detail.unreviewed_clause_ids,
    }


@router.get("/reviews/{job_id}/reports/{report_format}")
async def get_report(
    job_id: str,
    report_format: Literal["docx", "pdf"],
    actor: Actor = Depends(current_actor),
    service: ContractService = Depends(get_contract_service),
):
    try:
        detail = await service.get_review(actor, job_id)
    except JobNotFound as exc:
        raise HTTPException(
            status_code=404, detail="review job does not exist"
        ) from exc
    report = build_report(detail)
    if report_format == "docx":
        media_type = (
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        )
        content = report.docx_bytes
    else:
        media_type = "application/pdf"
        content = report.pdf_bytes
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="review-{job_id}.{report_format}"'
            )
        },
    )
