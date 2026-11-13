"""Evidence coverage snapshots; no ranking or automated hiring decisions.

Revision ID: b72f36c519e0
Revises: a63d62f4c918
"""

import sqlalchemy as sa
from alembic import op

revision = "b72f36c519e0"
down_revision = "a63d62f4c918"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "candidate_score_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("match_result_id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("vacancy_id", sa.Integer(), nullable=False),
        sa.Column("configuration_id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("method_version", sa.String(60), nullable=False),
        sa.Column("documented_weight", sa.Integer(), nullable=False),
        sa.Column("unverified_weight", sa.Integer(), nullable=False),
        sa.Column("review_weight", sa.Integer(), nullable=False),
        sa.Column("criterion_breakdown", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["match_result_id"], ["job_match_results.id"], ondelete="CASCADE"
        ),
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
            "match_result_id", "method_version", name="uq_scoring_snapshot"
        ),
        sa.CheckConstraint("documented_weight BETWEEN 0 AND 100"),
        sa.CheckConstraint("unverified_weight BETWEEN 0 AND 100"),
        sa.CheckConstraint("review_weight BETWEEN 0 AND 100"),
        sa.CheckConstraint(
            "documented_weight + unverified_weight + review_weight = 100"
        ),
    )
    for field in (
        "match_result_id", "document_id", "vacancy_id", "configuration_id", "profile_id"
    ):
        op.create_index(
            f"ix_candidate_score_snapshots_{field}",
            "candidate_score_snapshots", [field],
        )


def downgrade():
    for field in (
        "profile_id", "configuration_id", "vacancy_id", "document_id", "match_result_id"
    ):
        op.drop_index(
            f"ix_candidate_score_snapshots_{field}",
            table_name="candidate_score_snapshots",
        )
    op.drop_table("candidate_score_snapshots")
