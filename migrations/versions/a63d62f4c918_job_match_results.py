"""Persist source-backed, versioned criterion matches without hiring scores.

Revision ID: a63d62f4c918
Revises: d28f8a3c5210
"""

import sqlalchemy as sa
from alembic import op

revision = "a63d62f4c918"
down_revision = "d28f8a3c5210"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "job_match_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("vacancy_id", sa.Integer(), nullable=False),
        sa.Column("configuration_id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("criteria_results", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"], ["candidate_documents.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["vacancy_id"], ["vacancies.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["configuration_id"], ["criteria_configurations.id"]
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["candidate_profiles.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "document_id", "configuration_id", "profile_id",
            name="uq_job_match_snapshot",
        ),
    )
    for field in ("document_id", "vacancy_id", "configuration_id", "profile_id"):
        op.create_index(f"ix_job_match_results_{field}", "job_match_results", [field])


def downgrade():
    for field in ("profile_id", "configuration_id", "vacancy_id", "document_id"):
        op.drop_index(f"ix_job_match_results_{field}", table_name="job_match_results")
    op.drop_table("job_match_results")
