"""Repair keyword search text damaged by 0007 backfill.

0007 re-tokenized already-tokenized text and split every CJK character,
destroying multi-character lexemes such as 合同/联合体/生效. This revision
regenerates keyword_search_text from source content and legal metadata.

Revision ID: 0008_repair_keyword_search
Revises: 0007_domain_keyword_index
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text


revision: str = "0008_repair_keyword_search"
down_revision: str | None = "0007_domain_keyword_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tokenize_keyword_text(value: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        import jieba

    tokens: list[str] = []
    for segmented in jieba.cut(value, cut_all=False, HMM=False):
        for token in re.findall(r"[A-Za-z0-9_]+|[\u3400-\u9fff]+", segmented):
            normalized = token.lower()
            tokens.append(normalized)
            if re.fullmatch(r"[\u3400-\u9fff]+", normalized):
                tokens.extend(normalized)
    return " ".join(tokens)


def _domain_keyword_text(row) -> str:
    metadata = " ".join(
        part
        for part in (
            row["title"] or "",
            row["issuing_authority"] or "",
            row["document_number"] or "",
            row["section_title"] or "",
        )
        if part
    )
    raw = f"{metadata} {row['content']}" if metadata else row["content"]
    tokens = _tokenize_keyword_text(raw)
    article = re.sub(r"\s+", "", row["article_number"] or "").lower()
    if article:
        tokens = f"{tokens} {article}"
    return tokens


def upgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS knowledge_chunks_keyword_search_immutable "
        "ON knowledge_chunks"
    )
    conn = op.get_bind()
    rows = conn.execute(
        text(
            """
            SELECT chunk.id::text AS id,
                   chunk.content,
                   chunk.article_number,
                   chunk.section_title,
                   document.title,
                   document.issuing_authority,
                   document.document_number
            FROM knowledge_chunks chunk
            JOIN knowledge_documents document
              ON document.id = chunk.document_id
             AND document.scope = chunk.scope
             AND document.tenant_id IS NOT DISTINCT FROM chunk.tenant_id
            """
        )
    ).mappings().all()
    for row in rows:
        conn.execute(
            text(
                "UPDATE knowledge_chunks "
                "SET keyword_search_text = :value "
                "WHERE id = :id"
            ),
            {"value": _domain_keyword_text(row), "id": row["id"]},
        )
    op.execute(
        """
        CREATE TRIGGER knowledge_chunks_keyword_search_immutable
        BEFORE UPDATE OF keyword_search_text ON knowledge_chunks
        FOR EACH ROW EXECUTE FUNCTION reject_knowledge_chunk_keyword_search_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS knowledge_chunks_keyword_search_immutable "
        "ON knowledge_chunks"
    )
    op.execute(
        """
        UPDATE knowledge_chunks
        SET keyword_search_text =
            regexp_replace(lower(content), '([㐀-鿿])', E'\\\\1 ', 'g')
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_chunks_keyword_search_immutable
        BEFORE UPDATE OF keyword_search_text ON knowledge_chunks
        FOR EACH ROW EXECUTE FUNCTION reject_knowledge_chunk_keyword_search_mutation()
        """
    )
