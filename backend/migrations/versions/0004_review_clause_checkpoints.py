"""Add per-clause review checkpoints.

Revision ID: 0004_review_clause_checkpoints
Revises: 0003_indexed_keyword_search
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy.dialects import postgresql
import sqlalchemy as sa


revision: str = "0004_review_clause_checkpoints"
down_revision: str | None = "0003_indexed_keyword_search"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UUID = postgresql.UUID(as_uuid=False)

clause_checkpoint_status = postgresql.ENUM(
    "queued",
    "complete",
    "insufficient",
    "needs_retrieval",
    "failed",
    name="clause_checkpoint_status",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    clause_checkpoint_status.create(bind, checkfirst=True)

    op.create_table(
        "review_clause_checkpoints",
        sa.Column(
            "id",
            UUID,
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            UUID,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("job_id", UUID, nullable=False),
        sa.Column("contract_id", UUID, nullable=False),
        sa.Column("clause_id", sa.String(255), nullable=False),
        sa.Column("clause_text", sa.Text(), nullable=False),
        sa.Column(
            "status",
            clause_checkpoint_status,
            nullable=False,
            server_default=sa.text("'queued'::clause_checkpoint_status"),
        ),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "id", "tenant_id", name="uq_review_clause_checkpoints_id_tenant"
        ),
        sa.UniqueConstraint(
            "job_id", "clause_id", name="uq_review_clause_checkpoints_job_clause"
        ),
        sa.ForeignKeyConstraint(
            ["job_id", "contract_id", "tenant_id"],
            ["review_jobs.id", "review_jobs.contract_id", "review_jobs.tenant_id"],
            name="fk_review_clause_checkpoints_job_contract_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["contract_id", "tenant_id"],
            ["contracts.id", "contracts.tenant_id"],
            name="fk_review_clause_checkpoints_contract_tenant",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_review_clause_checkpoints_job_status",
        "review_clause_checkpoints",
        ["job_id", "status"],
    )
    op.execute(
        'ALTER TABLE "review_clause_checkpoints" ENABLE ROW LEVEL SECURITY'
    )
    op.execute(
        'ALTER TABLE "review_clause_checkpoints" FORCE ROW LEVEL SECURITY'
    )
    op.execute(
        'CREATE POLICY "review_clause_checkpoints_tenant_access" '
        'ON "review_clause_checkpoints" FOR ALL USING '
        "(tenant_id = (nullif(current_setting('app.tenant_id', true), ''))::uuid) "
        "WITH CHECK "
        "(tenant_id = (nullif(current_setting('app.tenant_id', true), ''))::uuid)"
    )


def downgrade() -> None:
    op.execute(
        'DROP POLICY IF EXISTS "review_clause_checkpoints_tenant_access" '
        'ON "review_clause_checkpoints"'
    )
    op.drop_table("review_clause_checkpoints")
    bind = op.get_bind()
    clause_checkpoint_status.drop(bind, checkfirst=True)
