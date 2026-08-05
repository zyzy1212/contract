"""Create the tenant-isolated core schema.

Revision ID: 0001_core_schema
Revises: None
"""

from collections.abc import Sequence

from alembic import op
from pgvector.sqlalchemy import Vector
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0001_core_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UUID = postgresql.UUID(as_uuid=False)
JSONB = postgresql.JSONB(astext_type=sa.Text())

tenant_status = postgresql.ENUM(
    "active", "suspended", name="tenant_status", create_type=False
)
actor_role = postgresql.ENUM(
    "customer", "admin", name="actor_role", create_type=False
)
record_status = postgresql.ENUM(
    "active", "inactive", "deleted", name="record_status", create_type=False
)
knowledge_scope = postgresql.ENUM(
    "public", "firm", "tenant_private", name="knowledge_scope", create_type=False
)
knowledge_source_type = postgresql.ENUM(
    "law",
    "firm_rule",
    "tenant_private",
    name="knowledge_source_type",
    create_type=False,
)
contract_status = postgresql.ENUM(
    "uploaded",
    "parsed",
    "reviewing",
    "complete",
    "failed",
    name="contract_status",
    create_type=False,
)
job_status = postgresql.ENUM(
    "queued",
    "running",
    "complete",
    "partial",
    "failed",
    name="job_status",
    create_type=False,
)
finding_status = postgresql.ENUM(
    "draft", "passed", "rejected", name="finding_status", create_type=False
)
risk_level = postgresql.ENUM(
    "high", "medium", "low", name="risk_level", create_type=False
)

ENUMS = (
    tenant_status,
    actor_role,
    record_status,
    knowledge_scope,
    knowledge_source_type,
    contract_status,
    job_status,
    finding_status,
    risk_level,
)


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
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
    )


def _tenant_expression(column: str = "tenant_id") -> str:
    return (
        f"{column} = "
        "(nullif(current_setting('app.tenant_id', true), ''))::uuid"
    )


def _enable_tenant_rls(table: str, column: str = "tenant_id") -> None:
    expression = _tenant_expression(column)
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY "{table}_tenant_access" ON "{table}" '
        f"FOR ALL USING ({expression}) WITH CHECK ({expression})"
    )


def _enable_knowledge_rls(table: str) -> None:
    tenant_expression = _tenant_expression()
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY "{table}_read" ON "{table}" FOR SELECT '
        f"USING (scope = 'public' OR {tenant_expression})"
    )
    op.execute(
        f'CREATE POLICY "{table}_private_write" ON "{table}" FOR ALL '
        f"USING (scope <> 'public' AND {tenant_expression}) "
        f"WITH CHECK (scope <> 'public' AND {tenant_expression})"
    )


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS migration_extension_ownership (
            revision_id text NOT NULL,
            extension_name text NOT NULL,
            created_by_revision boolean NOT NULL,
            PRIMARY KEY (revision_id, extension_name)
        )
        """
    )
    op.execute("REVOKE ALL ON TABLE migration_extension_ownership FROM PUBLIC")
    op.execute(
        """
        DO $migration$
        DECLARE
            extension_preexisting boolean;
            extension_created boolean := false;
        BEGIN
            SELECT EXISTS (
                SELECT 1 FROM pg_extension WHERE extname = 'vector'
            ) INTO extension_preexisting;
            IF NOT extension_preexisting THEN
                BEGIN
                    EXECUTE 'CREATE EXTENSION vector';
                    extension_created := true;
                EXCEPTION WHEN duplicate_object THEN
                    extension_created := false;
                END;
            END IF;
            INSERT INTO migration_extension_ownership
                (revision_id, extension_name, created_by_revision)
            VALUES ('0001_core_schema', 'vector', extension_created)
            ON CONFLICT (revision_id, extension_name) DO UPDATE
            SET created_by_revision =
                migration_extension_ownership.created_by_revision
                OR EXCLUDED.created_by_revision;
        END
        $migration$
        """
    )
    bind = op.get_bind()
    for enum in ENUMS:
        enum.create(bind, checkfirst=True)

    op.create_table(
        "tenants",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "status",
            tenant_status,
            nullable=False,
            server_default=sa.text("'active'::tenant_status"),
        ),
        *_timestamps(),
    )
    op.create_index("ix_tenants_status", "tenants", ["status"])

    op.create_table(
        "users",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "tenant_id",
            UUID,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_subject", sa.String(255), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False, server_default=""),
        sa.Column("role", actor_role, nullable=False),
        sa.Column(
            "status",
            record_status,
            nullable=False,
            server_default=sa.text("'active'::record_status"),
        ),
        *_timestamps(),
        sa.UniqueConstraint("id", "tenant_id", name="uq_users_id_tenant"),
        sa.UniqueConstraint("tenant_id", "external_subject", name="uq_users_tenant_subject"),
        sa.UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
    )
    op.create_index("ix_users_tenant_status", "users", ["tenant_id", "status"])

    op.create_table(
        "knowledge_documents",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "tenant_id",
            UUID,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("scope", knowledge_scope, nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("source_type", knowledge_source_type, nullable=False),
        sa.Column("issuing_authority", sa.String(500), nullable=False, server_default=""),
        sa.Column("document_number", sa.String(255), nullable=False, server_default=""),
        sa.Column("source_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("version", sa.String(255), nullable=False, server_default=""),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("expiry_date", sa.Date(), nullable=True),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("source_metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column(
            "status",
            record_status,
            nullable=False,
            server_default=sa.text("'active'::record_status"),
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "(scope = 'public' AND tenant_id IS NULL) OR "
            "(scope <> 'public' AND tenant_id IS NOT NULL)",
            name="ck_knowledge_documents_scope_tenant",
        ),
        sa.CheckConstraint(
            "expiry_date IS NULL OR effective_date IS NULL OR expiry_date >= effective_date",
            name="ck_knowledge_documents_effective_range",
        ),
        sa.CheckConstraint(
            "scope <> 'public' OR created_by IS NULL",
            name="ck_knowledge_documents_public_creator",
        ),
        sa.UniqueConstraint(
            "id", "scope", "tenant_id", name="uq_knowledge_documents_id_scope_tenant"
        ),
        sa.ForeignKeyConstraint(
            ["created_by", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_knowledge_documents_creator_tenant",
        ),
    )
    op.create_index(
        "ix_knowledge_documents_tenant_scope_status",
        "knowledge_documents",
        ["tenant_id", "scope", "status"],
    )
    op.create_index(
        "uq_knowledge_documents_public_content",
        "knowledge_documents",
        ["content_sha256"],
        unique=True,
        postgresql_where=sa.text("scope = 'public'"),
    )
    op.create_index(
        "uq_knowledge_documents_private_content",
        "knowledge_documents",
        ["tenant_id", "content_sha256"],
        unique=True,
        postgresql_where=sa.text("scope IN ('firm', 'tenant_private')"),
    )
    op.create_index(
        "ix_knowledge_documents_public_status",
        "knowledge_documents",
        ["status"],
        postgresql_where=sa.text("scope = 'public'"),
    )

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "tenant_id",
            UUID,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("scope", knowledge_scope, nullable=False),
        sa.Column(
            "document_id",
            UUID,
            sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_chunk_id",
            UUID,
            sa.ForeignKey("knowledge_chunks.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("article_number", sa.String(100), nullable=True),
        sa.Column("section_title", sa.String(500), nullable=True),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("paragraph_index", sa.Integer(), nullable=True),
        sa.Column("bboxes", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("source_snapshot", JSONB, nullable=False),
        sa.Column("embedding_model", sa.String(255), nullable=True),
        sa.Column("embedding", Vector(1024), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "(scope = 'public' AND tenant_id IS NULL) OR "
            "(scope <> 'public' AND tenant_id IS NOT NULL)",
            name="ck_knowledge_chunks_scope_tenant",
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_knowledge_chunks_ordinal"),
        sa.CheckConstraint(
            "page_end IS NULL OR page_start IS NULL OR page_end >= page_start",
            name="ck_knowledge_chunks_page_range",
        ),
        sa.UniqueConstraint(
            "id",
            "document_id",
            "scope",
            "tenant_id",
            name="uq_knowledge_chunks_id_document_scope_tenant",
        ),
        sa.UniqueConstraint("document_id", "ordinal", name="uq_knowledge_chunks_document_ordinal"),
        sa.ForeignKeyConstraint(
            ["document_id", "scope", "tenant_id"],
            [
                "knowledge_documents.id",
                "knowledge_documents.scope",
                "knowledge_documents.tenant_id",
            ],
            name="fk_knowledge_chunks_document_scope_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["parent_chunk_id", "document_id", "scope", "tenant_id"],
            [
                "knowledge_chunks.id",
                "knowledge_chunks.document_id",
                "knowledge_chunks.scope",
                "knowledge_chunks.tenant_id",
            ],
            name="fk_knowledge_chunks_parent_document_scope_tenant",
        ),
    )
    op.create_index(
        "ix_knowledge_chunks_tenant_document",
        "knowledge_chunks",
        ["tenant_id", "document_id"],
    )
    op.create_index(
        "ix_knowledge_chunks_parent",
        "knowledge_chunks",
        ["parent_chunk_id"],
    )
    op.create_index(
        "ix_knowledge_chunks_public_document",
        "knowledge_chunks",
        ["document_id"],
        postgresql_where=sa.text("scope = 'public'"),
    )
    op.execute(
        "CREATE INDEX ix_knowledge_chunks_embedding_hnsw ON knowledge_chunks "
        "USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL"
    )

    op.create_table(
        "contracts",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "tenant_id",
            UUID,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("uploaded_by", UUID, nullable=True),
        sa.Column("filename", sa.String(500), nullable=False),
        sa.Column("content_type", sa.String(255), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column(
            "status",
            contract_status,
            nullable=False,
            server_default=sa.text("'uploaded'::contract_status"),
        ),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("id", "tenant_id", name="uq_contracts_id_tenant"),
        sa.UniqueConstraint("tenant_id", "content_sha256", name="uq_contracts_tenant_content"),
        sa.ForeignKeyConstraint(
            ["uploaded_by", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_contracts_uploader_tenant",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_contracts_tenant_status", "contracts", ["tenant_id", "status"])

    op.create_table(
        "review_jobs",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "tenant_id",
            UUID,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "contract_id",
            UUID,
            nullable=False,
        ),
        sa.Column("requested_by", UUID, nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("review_config", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "status",
            job_status,
            nullable=False,
            server_default=sa.text("'queued'::job_status"),
        ),
        sa.Column("completed_clauses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_clauses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint(
            "id", "contract_id", "tenant_id", name="uq_review_jobs_id_contract_tenant"
        ),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_review_jobs_tenant_idempotency"),
        sa.ForeignKeyConstraint(
            ["contract_id", "tenant_id"],
            ["contracts.id", "contracts.tenant_id"],
            name="fk_review_jobs_contract_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_review_jobs_requester_tenant",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "completed_clauses >= 0 AND total_clauses >= completed_clauses",
            name="ck_review_jobs_progress",
        ),
    )
    op.create_index(
        "ix_review_jobs_tenant_contract_status",
        "review_jobs",
        ["tenant_id", "contract_id", "status"],
    )

    op.create_table(
        "review_findings",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "tenant_id",
            UUID,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            UUID,
            nullable=False,
        ),
        sa.Column(
            "contract_id",
            UUID,
            nullable=False,
        ),
        sa.Column("clause_id", sa.String(255), nullable=False),
        sa.Column("risk_level", risk_level, nullable=False),
        sa.Column("problem", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("suggestion", sa.Text(), nullable=False),
        sa.Column("proposed_clause", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "status",
            finding_status,
            nullable=False,
            server_default=sa.text("'draft'::finding_status"),
        ),
        sa.Column("model_name", sa.String(255), nullable=False),
        sa.Column("model_version", sa.String(255), nullable=False, server_default=""),
        *_timestamps(),
        sa.UniqueConstraint("id", "tenant_id", name="uq_review_findings_id_tenant"),
        sa.ForeignKeyConstraint(
            ["job_id", "contract_id", "tenant_id"],
            ["review_jobs.id", "review_jobs.contract_id", "review_jobs.tenant_id"],
            name="fk_review_findings_job_contract_tenant",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_review_findings_tenant_job_status",
        "review_findings",
        ["tenant_id", "job_id", "status"],
    )
    op.create_index(
        "ix_review_findings_contract_clause",
        "review_findings",
        ["contract_id", "clause_id"],
    )

    op.create_table(
        "evidence_snapshots",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "tenant_id",
            UUID,
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "finding_id",
            UUID,
            nullable=False,
        ),
        sa.Column(
            "knowledge_chunk_id",
            UUID,
            nullable=False,
        ),
        sa.Column("source_content_sha256", sa.String(64), nullable=False),
        sa.Column("source_document_sha256", sa.String(64), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("exact_excerpt", sa.Text(), nullable=False),
        sa.Column("source_snapshot", JSONB, nullable=False),
        sa.Column("retrieval_trace", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("finding_id", "rank", name="uq_evidence_snapshots_finding_rank"),
        sa.ForeignKeyConstraint(
            ["finding_id", "tenant_id"],
            ["review_findings.id", "review_findings.tenant_id"],
            name="fk_evidence_snapshots_finding_tenant",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("rank > 0", name="ck_evidence_snapshots_rank"),
        sa.CheckConstraint(
            "length(source_content_sha256) = 64 AND length(source_document_sha256) = 64",
            name="ck_evidence_snapshots_provenance_hashes",
        ),
    )
    op.create_index(
        "ix_evidence_snapshots_tenant_finding",
        "evidence_snapshots",
        ["tenant_id", "finding_id"],
    )
    op.create_index(
        "ix_evidence_snapshots_chunk",
        "evidence_snapshots",
        ["knowledge_chunk_id"],
    )

    op.create_table(
        "audit_events",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "tenant_id",
            UUID,
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("actor_id", UUID, nullable=True),
        sa.Column("event_type", sa.String(255), nullable=False),
        sa.Column("entity_type", sa.String(100), nullable=False),
        sa.Column("entity_id", UUID, nullable=True),
        sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_audit_events_actor_tenant",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_audit_events_tenant_created",
        "audit_events",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_audit_events_entity",
        "audit_events",
        ["entity_type", "entity_id"],
    )

    _enable_tenant_rls("tenants", "id")
    _enable_tenant_rls("users")
    _enable_knowledge_rls("knowledge_documents")
    _enable_knowledge_rls("knowledge_chunks")
    _enable_tenant_rls("contracts")
    _enable_tenant_rls("review_jobs")
    _enable_tenant_rls("review_findings")
    _enable_tenant_rls("evidence_snapshots")
    _enable_tenant_rls("audit_events")

    op.execute(
        """
        CREATE FUNCTION reject_knowledge_document_identity_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.id IS DISTINCT FROM NEW.id
               OR OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
               OR OLD.scope IS DISTINCT FROM NEW.scope
               OR OLD.title IS DISTINCT FROM NEW.title
               OR OLD.source_type IS DISTINCT FROM NEW.source_type
               OR OLD.issuing_authority IS DISTINCT FROM NEW.issuing_authority
               OR OLD.document_number IS DISTINCT FROM NEW.document_number
               OR OLD.source_url IS DISTINCT FROM NEW.source_url
               OR OLD.version IS DISTINCT FROM NEW.version
               OR OLD.effective_date IS DISTINCT FROM NEW.effective_date
               OR OLD.expiry_date IS DISTINCT FROM NEW.expiry_date
               OR OLD.content_sha256 IS DISTINCT FROM NEW.content_sha256
               OR OLD.object_key IS DISTINCT FROM NEW.object_key
               OR OLD.source_metadata IS DISTINCT FROM NEW.source_metadata
               OR OLD.created_by IS DISTINCT FROM NEW.created_by
               OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
                RAISE EXCEPTION 'knowledge document identity and provenance are immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_documents_identity_immutable
        BEFORE UPDATE ON knowledge_documents
        FOR EACH ROW EXECUTE FUNCTION reject_knowledge_document_identity_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_knowledge_chunk_identity_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.id IS DISTINCT FROM NEW.id
               OR OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
               OR OLD.scope IS DISTINCT FROM NEW.scope
               OR OLD.document_id IS DISTINCT FROM NEW.document_id
               OR OLD.parent_chunk_id IS DISTINCT FROM NEW.parent_chunk_id
               OR OLD.ordinal IS DISTINCT FROM NEW.ordinal
               OR OLD.content IS DISTINCT FROM NEW.content
               OR OLD.content_sha256 IS DISTINCT FROM NEW.content_sha256
               OR OLD.article_number IS DISTINCT FROM NEW.article_number
               OR OLD.section_title IS DISTINCT FROM NEW.section_title
               OR OLD.page_start IS DISTINCT FROM NEW.page_start
               OR OLD.page_end IS DISTINCT FROM NEW.page_end
               OR OLD.paragraph_index IS DISTINCT FROM NEW.paragraph_index
               OR OLD.bboxes IS DISTINCT FROM NEW.bboxes
               OR OLD.source_snapshot IS DISTINCT FROM NEW.source_snapshot
               OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
                RAISE EXCEPTION 'knowledge chunk identity and provenance are immutable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_chunks_identity_immutable
        BEFORE UPDATE ON knowledge_chunks
        FOR EACH ROW EXECUTE FUNCTION reject_knowledge_chunk_identity_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION validate_knowledge_document_tenant() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.created_by IS NOT NULL
               AND NEW.scope <> 'public'
               AND NOT EXISTS (
                   SELECT 1 FROM users
                   WHERE id = NEW.created_by AND tenant_id = NEW.tenant_id
               ) THEN
                RAISE EXCEPTION 'private knowledge creator must belong to the same tenant';
            END IF;
            IF TG_OP = 'UPDATE'
               AND (OLD.scope IS DISTINCT FROM NEW.scope
                    OR OLD.tenant_id IS DISTINCT FROM NEW.tenant_id)
               AND EXISTS (
                   SELECT 1 FROM knowledge_chunks chunk
                   WHERE chunk.document_id = NEW.id
                     AND (chunk.scope IS DISTINCT FROM NEW.scope
                          OR chunk.tenant_id IS DISTINCT FROM NEW.tenant_id)
               ) THEN
                RAISE EXCEPTION 'knowledge document scope or tenant conflicts with its chunks';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER knowledge_documents_tenant_guard
        AFTER INSERT OR UPDATE ON knowledge_documents
        DEFERRABLE INITIALLY IMMEDIATE
        FOR EACH ROW EXECUTE FUNCTION validate_knowledge_document_tenant()
        """
    )
    op.execute(
        """
        CREATE FUNCTION validate_knowledge_chunk_tenant() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            document_scope knowledge_scope;
            document_tenant uuid;
            parent_document_id uuid;
            parent_scope knowledge_scope;
            parent_tenant uuid;
        BEGIN
            SELECT scope, tenant_id
              INTO document_scope, document_tenant
              FROM knowledge_documents
             WHERE id = NEW.document_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'knowledge document does not exist';
            END IF;
            IF document_scope IS DISTINCT FROM NEW.scope
               OR document_tenant IS DISTINCT FROM NEW.tenant_id THEN
                RAISE EXCEPTION 'knowledge chunk scope and tenant must match its document';
            END IF;

            IF NEW.parent_chunk_id IS NOT NULL THEN
                SELECT document_id, scope, tenant_id
                  INTO parent_document_id, parent_scope, parent_tenant
                  FROM knowledge_chunks
                 WHERE id = NEW.parent_chunk_id;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'parent knowledge chunk does not exist';
                END IF;
                IF parent_document_id IS DISTINCT FROM NEW.document_id
                   OR parent_scope IS DISTINCT FROM NEW.scope
                   OR parent_tenant IS DISTINCT FROM NEW.tenant_id THEN
                    RAISE EXCEPTION 'parent chunk must belong to the same document, scope, and tenant';
                END IF;
            END IF;
            IF TG_OP = 'UPDATE'
               AND (OLD.document_id IS DISTINCT FROM NEW.document_id
                    OR OLD.scope IS DISTINCT FROM NEW.scope
                    OR OLD.tenant_id IS DISTINCT FROM NEW.tenant_id)
               AND EXISTS (
                   SELECT 1 FROM knowledge_chunks child
                   WHERE child.parent_chunk_id = NEW.id
                     AND (child.document_id IS DISTINCT FROM NEW.document_id
                          OR child.scope IS DISTINCT FROM NEW.scope
                          OR child.tenant_id IS DISTINCT FROM NEW.tenant_id)
               ) THEN
                RAISE EXCEPTION 'knowledge chunk change conflicts with its children';
            END IF;
            IF TG_OP = 'UPDATE'
               AND (OLD.scope IS DISTINCT FROM NEW.scope
                    OR OLD.tenant_id IS DISTINCT FROM NEW.tenant_id)
               AND EXISTS (
                   SELECT 1 FROM evidence_snapshots evidence
                   WHERE evidence.knowledge_chunk_id = NEW.id
                     AND NOT (
                         (NEW.scope = 'public' AND NEW.tenant_id IS NULL)
                         OR evidence.tenant_id IS NOT DISTINCT FROM NEW.tenant_id
                     )
               ) THEN
                RAISE EXCEPTION 'knowledge chunk change conflicts with evidence tenant';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER knowledge_chunks_tenant_guard
        AFTER INSERT OR UPDATE ON knowledge_chunks
        DEFERRABLE INITIALLY IMMEDIATE
        FOR EACH ROW EXECUTE FUNCTION validate_knowledge_chunk_tenant()
        """
    )
    op.execute(
        """
        CREATE FUNCTION validate_evidence_snapshot_knowledge() RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            chunk_scope knowledge_scope;
            chunk_tenant uuid;
            chunk_hash varchar(64);
            document_hash varchar(64);
        BEGIN
            IF NEW.knowledge_chunk_id IS NULL THEN
                RETURN NEW;
            END IF;
            SELECT chunk.scope, chunk.tenant_id,
                   chunk.content_sha256, document.content_sha256
              INTO chunk_scope, chunk_tenant, chunk_hash, document_hash
              FROM public.knowledge_chunks chunk
              JOIN public.knowledge_documents document
                ON document.id = chunk.document_id
             WHERE chunk.id = NEW.knowledge_chunk_id
             FOR KEY SHARE OF chunk, document;
            IF NOT FOUND OR NOT (
                (chunk_scope = 'public' AND chunk_tenant IS NULL)
                OR chunk_tenant IS NOT DISTINCT FROM NEW.tenant_id
            )
               OR chunk_hash IS DISTINCT FROM NEW.source_content_sha256
               OR document_hash IS DISTINCT FROM NEW.source_document_sha256 THEN
                RAISE EXCEPTION 'evidence source is unavailable';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION validate_evidence_snapshot_knowledge() FROM PUBLIC"
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER evidence_snapshots_knowledge_guard
        AFTER INSERT OR UPDATE ON evidence_snapshots
        DEFERRABLE INITIALLY IMMEDIATE
        FOR EACH ROW EXECUTE FUNCTION validate_evidence_snapshot_knowledge()
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_evidence_snapshot_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'evidence snapshots are immutable';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER evidence_snapshots_immutable
        BEFORE UPDATE OR DELETE ON evidence_snapshots
        FOR EACH ROW EXECUTE FUNCTION reject_evidence_snapshot_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS evidence_snapshots_immutable ON evidence_snapshots")
    op.execute("DROP FUNCTION IF EXISTS reject_evidence_snapshot_mutation()")
    op.execute(
        "DROP TRIGGER IF EXISTS evidence_snapshots_knowledge_guard ON evidence_snapshots"
    )
    op.execute("DROP TRIGGER IF EXISTS knowledge_chunks_tenant_guard ON knowledge_chunks")
    op.execute(
        "DROP TRIGGER IF EXISTS knowledge_documents_tenant_guard ON knowledge_documents"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS knowledge_chunks_identity_immutable ON knowledge_chunks"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS knowledge_documents_identity_immutable ON knowledge_documents"
    )
    op.execute("DROP FUNCTION IF EXISTS validate_evidence_snapshot_knowledge()")
    op.execute("DROP FUNCTION IF EXISTS validate_knowledge_chunk_tenant()")
    op.execute("DROP FUNCTION IF EXISTS validate_knowledge_document_tenant()")
    op.execute("DROP FUNCTION IF EXISTS reject_knowledge_chunk_identity_mutation()")
    op.execute("DROP FUNCTION IF EXISTS reject_knowledge_document_identity_mutation()")

    for table, policies in (
        ("audit_events", ("audit_events_tenant_access",)),
        ("evidence_snapshots", ("evidence_snapshots_tenant_access",)),
        ("review_findings", ("review_findings_tenant_access",)),
        ("review_jobs", ("review_jobs_tenant_access",)),
        ("contracts", ("contracts_tenant_access",)),
        ("knowledge_chunks", ("knowledge_chunks_read", "knowledge_chunks_private_write")),
        ("knowledge_documents", ("knowledge_documents_read", "knowledge_documents_private_write")),
        ("users", ("users_tenant_access",)),
        ("tenants", ("tenants_tenant_access",)),
    ):
        for policy in policies:
            op.execute(f'DROP POLICY IF EXISTS "{policy}" ON "{table}"')

    op.drop_table("audit_events")
    op.drop_table("evidence_snapshots")
    op.drop_table("review_findings")
    op.drop_table("review_jobs")
    op.drop_table("contracts")
    op.drop_table("knowledge_chunks")
    op.drop_table("knowledge_documents")
    op.drop_table("users")
    op.drop_table("tenants")

    bind = op.get_bind()
    for enum in reversed(ENUMS):
        enum.drop(bind, checkfirst=True)
    op.execute(
        """
        DO $migration$
        DECLARE
            extension_was_created boolean := false;
        BEGIN
            SELECT created_by_revision
              INTO extension_was_created
              FROM migration_extension_ownership
             WHERE revision_id = '0001_core_schema'
               AND extension_name = 'vector';
            IF extension_was_created THEN
                EXECUTE 'DROP EXTENSION IF EXISTS vector';
            END IF;
            DELETE FROM migration_extension_ownership
             WHERE revision_id = '0001_core_schema'
               AND extension_name = 'vector';
        END
        $migration$
        """
    )
    op.execute("DROP TABLE IF EXISTS migration_extension_ownership")
