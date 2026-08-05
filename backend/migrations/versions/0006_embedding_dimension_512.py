"""Resize knowledge embeddings to 512 dimensions for faster CPU ingestion."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector


revision: str = "0006_embedding_dimension_512"
down_revision: str | None = "0005_clause_locators"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


INDEX_NAME = "ix_knowledge_chunks_embedding_hnsw"


def _create_embedding_column(dimension: int) -> None:
    op.add_column(
        "knowledge_chunks",
        sa.Column("embedding", Vector(dimension), nullable=True),
    )
    op.execute(
        f"CREATE INDEX {INDEX_NAME} ON knowledge_chunks "
        "USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL"
    )


def upgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="knowledge_chunks")
    op.drop_column("knowledge_chunks", "embedding")
    _create_embedding_column(512)


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="knowledge_chunks")
    op.drop_column("knowledge_chunks", "embedding")
    _create_embedding_column(1024)
