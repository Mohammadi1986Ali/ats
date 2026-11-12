"""Store persistent CV/JD embedding vectors and queue existing CV stages."""

import sqlalchemy as sa
from alembic import op

revision = "d28f8a3c5210"
down_revision = "c9f8a7d4b132"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        op.execute("""
            CREATE TABLE embedding_chunks (
                id BIGSERIAL PRIMARY KEY,
                source_type VARCHAR(8) NOT NULL,
                document_id INTEGER REFERENCES candidate_documents(id) ON DELETE CASCADE,
                vacancy_id INTEGER NOT NULL REFERENCES vacancies(id) ON DELETE CASCADE,
                source_hash VARCHAR(64) NOT NULL,
                model VARCHAR(200) NOT NULL,
                chunker_version VARCHAR(40) NOT NULL,
                chunk_index INTEGER NOT NULL,
                reference VARCHAR(200) NOT NULL,
                chunk_text TEXT NOT NULL,
                embedding vector(768) NOT NULL,
                CONSTRAINT uq_embedding_chunk UNIQUE
                    (source_type, vacancy_id, document_id, source_hash,
                     model, chunker_version, chunk_index),
                CONSTRAINT ck_embedding_source CHECK
                    ((source_type = 'CV' AND document_id IS NOT NULL) OR
                     (source_type = 'JD' AND document_id IS NULL))
            )
        """)
    else:
        op.create_table(
            "embedding_chunks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("source_type", sa.String(8), nullable=False),
            sa.Column("document_id", sa.Integer(),
                      sa.ForeignKey("candidate_documents.id", ondelete="CASCADE"),
                      nullable=True),
            sa.Column("vacancy_id", sa.Integer(),
                      sa.ForeignKey("vacancies.id", ondelete="CASCADE"), nullable=False),
            sa.Column("source_hash", sa.String(64), nullable=False),
            sa.Column("model", sa.String(200), nullable=False),
            sa.Column("chunker_version", sa.String(40), nullable=False),
            sa.Column("chunk_index", sa.Integer, nullable=False),
            sa.Column("reference", sa.String(200), nullable=False),
            sa.Column("chunk_text", sa.Text(), nullable=False),
            sa.Column("embedding", sa.Text(), nullable=False),
            sa.UniqueConstraint(
                "source_type", "vacancy_id", "document_id", "source_hash",
                "model", "chunker_version", "chunk_index", name="uq_embedding_chunk",
            ),
            sa.CheckConstraint(
                "(source_type = 'CV' AND document_id IS NOT NULL) OR "
                "(source_type = 'JD' AND document_id IS NULL)",
                name="ck_embedding_source",
            ),
        )
    op.create_index("ix_embedding_chunks_vacancy_id", "embedding_chunks", ["vacancy_id"])
    op.create_index("ix_embedding_chunks_document_id", "embedding_chunks", ["document_id"])
    op.execute("""
        INSERT INTO document_pipeline_stages
          (document_id, stage, status, attempts, updated_at)
        SELECT d.document_id, 'EMBEDDING', 'WAITING', 0, CURRENT_TIMESTAMP
        FROM document_pipeline_stages d
        WHERE d.stage = 'TEXT_EXTRACTION'
          AND NOT EXISTS (
            SELECT 1 FROM document_pipeline_stages e
            WHERE e.document_id = d.document_id AND e.stage = 'EMBEDDING')
    """)


def downgrade():
    op.execute("DELETE FROM document_pipeline_stages WHERE stage = 'EMBEDDING'")
    op.drop_index("ix_embedding_chunks_document_id", table_name="embedding_chunks")
    op.drop_index("ix_embedding_chunks_vacancy_id", table_name="embedding_chunks")
    op.drop_table("embedding_chunks")
