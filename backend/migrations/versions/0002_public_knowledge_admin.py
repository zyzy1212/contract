"""Authorize transaction-bound administrators to maintain public law knowledge.

Revision ID: 0002_public_knowledge_admin
Revises: 0001_core_schema
"""

from collections.abc import Sequence

from alembic import op


revision: str = "0002_public_knowledge_admin"
down_revision: str | None = "0001_core_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TENANT = (
    "tenant_id = "
    "(nullif(current_setting('app.tenant_id', true), ''))::uuid"
)
ADMIN = "current_setting('app.knowledge_admin', true) = 'true'"


def _drop_task_1_write_policy(table: str) -> None:
    op.execute(f'DROP POLICY IF EXISTS "{table}_private_write" ON "{table}"')


def _create_private_policies(table: str) -> None:
    predicate = f"scope <> 'public' AND {TENANT}"
    op.execute(
        f'CREATE POLICY "{table}_private_insert" ON "{table}" '
        f"FOR INSERT WITH CHECK ({predicate})"
    )
    op.execute(
        f'CREATE POLICY "{table}_private_update" ON "{table}" '
        f"FOR UPDATE USING ({predicate}) WITH CHECK ({predicate})"
    )
    op.execute(
        f'CREATE POLICY "{table}_private_delete" ON "{table}" '
        f"FOR DELETE USING ({predicate})"
    )


def _create_public_policies() -> None:
    document = f"scope = 'public' AND source_type = 'law' AND {ADMIN}"
    op.execute(
        'CREATE POLICY "knowledge_documents_public_insert" '
        'ON "knowledge_documents" FOR INSERT '
        f"WITH CHECK ({document})"
    )
    chunk = (
        f"scope = 'public' AND {ADMIN} AND "
        "EXISTS (SELECT 1 FROM knowledge_documents document "
        "WHERE document.id = document_id "
        "AND document.scope = 'public' "
        "AND document.source_type = 'law')"
    )
    op.execute(
        'CREATE POLICY "knowledge_chunks_public_insert" '
        'ON "knowledge_chunks" FOR INSERT '
        f"WITH CHECK ({chunk})"
    )


def upgrade() -> None:
    for table in ("knowledge_documents", "knowledge_chunks"):
        _drop_task_1_write_policy(table)
        _create_private_policies(table)
    _create_public_policies()


def downgrade() -> None:
    for table in ("knowledge_documents", "knowledge_chunks"):
        for suffix in (
            "private_insert",
            "private_update",
            "private_delete",
            "public_insert",
        ):
            op.execute(f'DROP POLICY IF EXISTS "{table}_{suffix}" ON "{table}"')
        predicate = f"scope <> 'public' AND {TENANT}"
        op.execute(
            f'CREATE POLICY "{table}_private_write" ON "{table}" FOR ALL '
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
