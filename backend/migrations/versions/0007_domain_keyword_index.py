"""Index legal metadata in knowledge chunk keyword search.

Revision ID: 0007_domain_keyword_index
Revises: 0006_embedding_dimension_512
"""

from collections.abc import Sequence

from alembic import op


revision: str = "0007_domain_keyword_index"
down_revision: str | None = "0006_embedding_dimension_512"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS knowledge_chunks_keyword_search_immutable "
        "ON knowledge_chunks"
    )
    op.execute(
        """
        UPDATE knowledge_chunks chunk
        SET keyword_search_text =
            chunk.keyword_search_text
            || ' '
            || regexp_replace(
                lower(
                    concat_ws(
                        ' ',
                        document.title,
                        document.issuing_authority,
                        document.document_number,
                        chunk.section_title
                    )
                ),
                '([㐀-鿿])', E'\\\\1 ', 'g'
            )
            || ' '
            || regexp_replace(
                coalesce(chunk.article_number, ''),
                '[[:space:]]',
                '',
                'g'
            )
        FROM knowledge_documents document
        WHERE document.id = chunk.document_id
          AND document.scope = chunk.scope
          AND document.tenant_id IS NOT DISTINCT FROM chunk.tenant_id
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
