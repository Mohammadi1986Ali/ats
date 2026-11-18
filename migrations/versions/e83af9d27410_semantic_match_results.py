"""Add vector-based, source-quoted candidate comparison snapshots.

Revision ID: e83af9d27410
Revises: b72f36c519e0
"""

import sqlalchemy as sa
from alembic import op

revision = "e83af9d27410"
down_revision = "b72f36c519e0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "semantic_match_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("vacancy_id", sa.Integer(), nullable=False),
        sa.Column("jd_source_hash", sa.String(64), nullable=False),
        sa.Column("cv_source_hash", sa.String(64), nullable=False),
        sa.Column("embedding_model", sa.String(200), nullable=False),
        sa.Column("method_version", sa.String(60), nullable=False),
        sa.Column("comparisons", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"], ["candidate_documents.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["vacancy_id"], ["vacancies.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "document_id", "jd_source_hash", "cv_source_hash",
            "embedding_model", "method_version", name="uq_semantic_match_snapshot",
        ),
    )
    op.create_index(
        "ix_semantic_match_results_document_id", "semantic_match_results", ["document_id"]
    )
    op.create_index(
        "ix_semantic_match_results_vacancy_id", "semantic_match_results", ["vacancy_id"]
    )


def downgrade():
    op.drop_index(
        "ix_semantic_match_results_vacancy_id", table_name="semantic_match_results"
    )
    op.drop_index(
        "ix_semantic_match_results_document_id", table_name="semantic_match_results"
    )
    op.drop_table("semantic_match_results")
