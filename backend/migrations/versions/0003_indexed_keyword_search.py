"""Add indexed Chinese-aware keyword search for knowledge chunks.

Revision ID: 0003_indexed_keyword_search
Revises: 0002_public_knowledge_admin
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0003_indexed_keyword_search"
down_revision: str | None = "0002_public_knowledge_admin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "knowledge_chunks",
        sa.Column(
            "keyword_search_text",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    # Existing rows receive character-separated CJK fallback lexemes. New rows
    # additionally receive deterministic jieba terms from ingestion.
    op.execute(
        "UPDATE knowledge_chunks SET keyword_search_text = "
        "regexp_replace(lower(content), '([㐀-鿿])', E'\\\\1 ', 'g')"
    )
    op.add_column(
        "knowledge_chunks",
        sa.Column(
            "keyword_tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple', keyword_search_text)", persisted=True),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_knowledge_chunks_keyword_tsv_gin",
        "knowledge_chunks",
        ["keyword_tsv"],
        postgresql_using="gin",
    )
    op.execute(
        """
        CREATE FUNCTION reject_knowledge_chunk_keyword_search_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'knowledge chunk keyword search text is immutable';
        END;
        $$
        """
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
    op.execute("DROP FUNCTION IF EXISTS reject_knowledge_chunk_keyword_search_mutation()")
    op.drop_index("ix_knowledge_chunks_keyword_tsv_gin", table_name="knowledge_chunks")
    op.drop_column("knowledge_chunks", "keyword_tsv")
    op.drop_column("knowledge_chunks", "keyword_search_text")
