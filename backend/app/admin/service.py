from __future__ import annotations

import re
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text

from app.auth import Actor
from app.config import REPOSITORY_ROOT, get_settings
from app.db import async_session_factory, bind_tenant, tenant_transaction
from app.embeddings import load_sentence_transformer
from app.knowledge.ingestion import KnowledgeIngestionService
from app.knowledge.models import KnowledgeSource
from app.knowledge.repository import KnowledgeRepository
from app.storage.objects import LocalObjectStore


LAW_TITLE_SUFFIXES = (
    "法",
    "条例",
    "规定",
    "办法",
    "规则",
    "细则",
    "决定",
    "解释",
    "法典",
    "宪法",
    "通则",
    "纲要",
    "公约",
    "章程",
)
AUTHORITY_PATTERN = re.compile(
    r"全国人民代表大会常务委员会|全国人民代表大会|国务院|最高人民法院|最高人民检察院"
)
AUTHORITY_LABEL_PATTERN = re.compile(
    r"(?:发布|公布|制定|主管)机关(?:名称)?[:：]\s*([^\s，。；、（）()]{2,60})"
)
ENACTMENT_AUTHORITY_PATTERN = re.compile(
    r"(\d{4}年\d{1,2}月\d{1,2}日\s*)?("
    r"(?:第[一二三四五六七八九十百\d]+届)?"
    r"(?:全国人民代表大会常务委员会|全国人民代表大会|国务院|最高人民法院|最高人民检察院|"
    r"[\u4e00-\u9fa5]{2,20}(?:人民法院|人民检察院|人民政府|委员会|大会|政府|办公室|部|厅|局|署|办))"
    r")[^（）]*?(?:会议通过|通过|批准|公布)"
)
AUTHORITY_DECREE_PATTERN = re.compile(
    r"([\u4e00-\u9fa5]{2,20}(?:人民法院|人民检察院|人民政府|委员会|大会|政府|办公室|部|厅|局|署|办)"
    r"|全国人民代表大会常务委员会|全国人民代表大会|国务院|最高人民法院|最高人民检察院)"
    r"(?:令|公告|批复|通知)"
)
EFFECTIVE_DATE_PATTERNS = (
    re.compile(r"自\s*(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日\s*起\s*(?:施行|实施|生效)"),
    re.compile(r"(?:施行|实施|生效)(?:日期|时间)?\s*[:：]\s*(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日"),
    re.compile(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日\s*起\s*(?:施行|实施|生效)"),
    re.compile(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日\s*(?:施行|实施)"),
)
_STRUCTURAL_HEADING_PATTERN = re.compile(
    r"^(?:第[\d一二三四五六七八九十百千]+[编篇章条款节]|附则|目录)"
)
_PAGE_MARKER_PATTERN = re.compile(
    r"^(?:第\s*\d+\s*页|[-—]?\s*\d+\s*[-—]?|\d+)$"
)


def _normalized_line(candidate: str) -> str:
    return re.sub(r"[\s\u3000]+", "", candidate)


def _is_structural_heading(candidate: str) -> bool:
    return bool(_STRUCTURAL_HEADING_PATTERN.match(_normalized_line(candidate)))


def _is_plausible_title(candidate: str) -> bool:
    normalized = _normalized_line(candidate)
    if not 2 <= len(normalized) <= 60:
        return False
    if _is_structural_heading(candidate):
        return False
    if _PAGE_MARKER_PATTERN.match(normalized):
        return False
    if not re.search(r"[\u4e00-\u9fa5]", normalized):
        return False
    if candidate.startswith(("（", "(", "【", "《")):
        return False
    if re.search(
        r"(?:\d{1,4}年\s*\d{1,2}月\s*\d{1,2}日|通过|发布|公布)",
        candidate,
    ):
        return False
    return True


def _document_text(
    filename: str,
    data: bytes,
    *,
    head_pages: int = 3,
    tail_pages: int = 0,
) -> str:
    suffix = Path(filename).suffix.lower()
    try:
        if suffix == ".docx":
            from docx import Document

            document = Document(BytesIO(data))
            return "\n".join(paragraph.text for paragraph in document.paragraphs)
        if suffix == ".pdf":
            import fitz

            document = fitz.open(stream=data, filetype="pdf")
            try:
                page_indices = set(range(min(document.page_count, head_pages)))
                if tail_pages and document.page_count > head_pages:
                    start = max(head_pages, document.page_count - tail_pages)
                    page_indices.update(range(start, document.page_count))
                return "\n".join(
                    document.load_page(index).get_text()
                    for index in sorted(page_indices)
                )
            finally:
                document.close()
    except Exception:
        return ""
    return ""


def _extract_issuing_authority(text: str) -> str:
    match = AUTHORITY_LABEL_PATTERN.search(text)
    if match:
        return match.group(1).strip()
    match = ENACTMENT_AUTHORITY_PATTERN.search(text)
    if match:
        authority = match.group(2).strip()
        return re.sub(r"^第[一二三四五六七八九十百\d]+届", "", authority)
    match = AUTHORITY_DECREE_PATTERN.search(text)
    if match:
        return match.group(1).strip()
    match = AUTHORITY_PATTERN.search(text)
    if match:
        return match.group(0)
    return ""


def _extract_effective_date(text: str) -> str:
    for pattern in EFFECTIVE_DATE_PATTERNS:
        match = pattern.search(text)
        if match:
            return (
                f"{int(match.group(1)):04d}-"
                f"{int(match.group(2)):02d}-"
                f"{int(match.group(3)):02d}"
            )
    return ""


def _filename_title(filename: str) -> str:
    stem = Path(filename).stem
    match = re.match(r"^(.*?)(?:_\d{8})?$", stem)
    return (match.group(1) if match else stem).strip()


def extract_law_metadata(
    filename: str,
    data: bytes,
    *,
    head_pages: int = 3,
    tail_pages: int = 0,
) -> dict[str, str]:
    text = _document_text(
        filename,
        data,
        head_pages=head_pages,
        tail_pages=tail_pages,
    )
    metadata: dict[str, str] = {}

    title = ""
    for line in text.splitlines():
        candidate = line.strip()
        normalized = _normalized_line(candidate)
        if not 2 <= len(normalized) <= 60:
            continue
        if _is_structural_heading(candidate):
            continue
        if normalized.endswith(LAW_TITLE_SUFFIXES):
            title = candidate
            break
    if not title:
        for line in text.splitlines():
            candidate = line.strip()
            if _is_plausible_title(candidate):
                title = candidate
                break
    if not title:
        title = _filename_title(filename)
    if title:
        metadata["title"] = title

    issuing_authority = _extract_issuing_authority(text)
    if issuing_authority:
        metadata["issuing_authority"] = issuing_authority

    effective_date = _extract_effective_date(text)
    if effective_date:
        metadata["effective_date"] = effective_date

    filename_version = re.search(r"_(\d{8})", filename)
    if filename_version:
        metadata["version"] = filename_version.group(1)
    else:
        date_match = re.search(r"\d{4}年\d{1,2}月\d{1,2}日", text)
        if date_match:
            metadata["version"] = date_match.group(0)

    if title and any(title.endswith(suffix) for suffix in LAW_TITLE_SUFFIXES):
        metadata["source_type"] = "law"
    return metadata


class SentenceTransformerEmbeddingProvider:
    model_name = "BAAI/bge-small-zh-v1.5"

    def __init__(
        self,
        *,
        precision: str | None = None,
        batch_size: int | None = None,
    ) -> None:
        settings = get_settings()
        self.precision = (
            precision if precision is not None else settings.embedding_precision
        )
        self.batch_size = (
            batch_size if batch_size is not None else settings.embedding_batch_size
        )
        self._model = None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self._model is None:
            self._model = load_sentence_transformer(self.model_name, self.precision)
        return [
            [float(value) for value in vector]
            for vector in self._model.encode(
                texts,
                normalize_embeddings=True,
                batch_size=self.batch_size,
            )
        ]


def default_ingestion_service() -> KnowledgeIngestionService:
    return KnowledgeIngestionService(
        embedding_provider=SentenceTransformerEmbeddingProvider(),
        repository=KnowledgeRepository(transaction_factory=tenant_transaction),
        object_store=LocalObjectStore(REPOSITORY_ROOT / "storage" / "objects"),
    )


class AdminService:
    def __init__(
        self,
        *,
        ingestion_factory=default_ingestion_service,
        transaction_factory=tenant_transaction,
        enqueue: Callable[..., None] | None = None,
    ) -> None:
        self._ingestion_factory = ingestion_factory
        self._transaction_factory = transaction_factory
        self._enqueue = enqueue

    async def ingest(
        self,
        actor: Actor,
        source: KnowledgeSource,
        filename: str,
        data: bytes,
    ) -> dict[str, str]:
        suffix = Path(filename).suffix.lower()
        if suffix not in {".pdf", ".docx"}:
            raise ValueError("only PDF and DOCX are supported")
        staging_root = REPOSITORY_ROOT / "storage" / "staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        path = staging_root / f"{uuid4().hex}{suffix}"
        path.write_bytes(data)
        try:
            if self._enqueue is not None:
                self._enqueue(source, actor, path)
            else:
                from app.tasks.ingest_knowledge import (
                    enqueue_knowledge_ingestion,
                )

                enqueue_knowledge_ingestion(source, actor, path)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return {"id": "", "status": "queued"}

    async def list_knowledge(
        self,
        actor: Actor,
        scope: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, object]]:
        filters = ""
        parameters: dict[str, object] = {}
        if scope:
            filters += " AND scope = :scope"
            parameters["scope"] = scope
        if status:
            filters += " AND status = :status"
            parameters["status"] = status
        else:
            filters += " AND status <> 'deleted'"
        async with self._transaction_factory(actor) as session:
            result = await session.execute(
                text(
                    f"""
                    SELECT id::text AS id,
                           title,
                           scope::text AS scope,
                           version,
                           effective_date::text AS effective_date,
                           status::text AS status
                    FROM knowledge_documents
                    WHERE 1 = 1{filters}
                    ORDER BY title, created_at
                    """
                ),
                parameters,
            )
            return [dict(row) for row in result.mappings().all()]

    async def archive(
        self,
        actor: Actor,
        document_id: str,
    ) -> dict[str, str]:
        async with self._transaction_factory(actor) as session:
            await session.execute(
                text(
                    """
                    UPDATE knowledge_documents
                    SET status = 'deleted', updated_at = CURRENT_TIMESTAMP
                    WHERE id = :document_id
                    """
                ),
                {"document_id": document_id},
            )
        return {"id": document_id, "status": "deleted"}

    async def restore(
        self,
        actor: Actor,
        document_id: str,
    ) -> dict[str, str]:
        async with self._transaction_factory(actor) as session:
            await session.execute(
                text(
                    """
                    UPDATE knowledge_documents
                    SET status = 'active', updated_at = CURRENT_TIMESTAMP
                    WHERE id = :document_id
                    """
                ),
                {"document_id": document_id},
            )
        return {"id": document_id, "status": "active"}

    async def activate(
        self,
        actor: Actor,
        document_id: str,
    ) -> dict[str, str]:
        async with self._transaction_factory(actor) as session:
            await session.execute(
                text(
                    """
                    UPDATE knowledge_documents
                    SET status = 'active', updated_at = CURRENT_TIMESTAMP
                    WHERE id = :document_id
                    """
                ),
                {"document_id": document_id},
            )
        return {"id": document_id, "status": "active"}

    async def deactivate(
        self,
        actor: Actor,
        document_id: str,
    ) -> dict[str, str]:
        async with self._transaction_factory(actor) as session:
            await session.execute(
                text(
                    """
                    UPDATE knowledge_documents
                    SET status = 'inactive', updated_at = CURRENT_TIMESTAMP
                    WHERE id = :document_id
                    """
                ),
                {"document_id": document_id},
            )
        return {"id": document_id, "status": "inactive"}

    async def list_tenants(self, actor: Actor) -> list[dict[str, object]]:
        async with self._transaction_factory(actor) as session:
            result = await session.execute(
                text(
                    """
                    SELECT id::text AS id, slug, name, status::text AS status
                    FROM tenants
                    ORDER BY created_at
                    """
                )
            )
            return [dict(row) for row in result.mappings().all()]

    async def create_tenant(
        self,
        actor: Actor,
        slug: str,
        name: str,
    ) -> dict[str, str]:
        tenant_id = str(uuid4())
        factory = async_session_factory()
        async with factory() as session, session.begin():
            await bind_tenant(session, tenant_id)
            await session.execute(
                text(
                    """
                    INSERT INTO tenants (id, slug, name, status)
                    VALUES (:id, :slug, :name, 'active')
                    """
                ),
                {"id": tenant_id, "slug": slug, "name": name},
            )
        return {"id": tenant_id, "slug": slug, "name": name, "status": "active"}

    async def assign_user(
        self,
        actor: Actor,
        tenant_id: str,
        external_subject: str,
        email: str,
        role: str = "customer",
    ) -> dict[str, str]:
        async with self._transaction_factory(
            actor, requested_tenant_id=tenant_id
        ) as session:
            result = await session.execute(
                text(
                    """
                    INSERT INTO users (
                        tenant_id, external_subject, email, display_name, role
                    ) VALUES (
                        :tenant_id, :external_subject, :email, :external_subject, :role
                    )
                    RETURNING id::text AS id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "external_subject": external_subject,
                    "email": email,
                    "role": role,
                },
            )
            row = result.mappings().first()
            if row is None:
                raise RuntimeError("user insert returned no id")
            return {
                "id": str(row["id"]),
                "tenant_id": tenant_id,
                "email": email,
            }
