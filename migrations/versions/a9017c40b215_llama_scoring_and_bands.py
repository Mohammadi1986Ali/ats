"""Configurable evidence score bands and separate, auditable Llama assessments."""

import sqlalchemy as sa
from alembic import op

revision = "a9017c40b215"
down_revision = "f58a1b0d2319"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("vacancies", sa.Column(
        "good_threshold", sa.Integer(), nullable=False, server_default="75"
    ))
    op.add_column("vacancies", sa.Column(
        "medium_threshold", sa.Integer(), nullable=False, server_default="45"
    ))
    op.create_check_constraint(
        "ck_vacancy_score_bands", "vacancies",
        "medium_threshold BETWEEN 1 AND 99 AND "
        "good_threshold BETWEEN 1 AND 100 AND medium_threshold < good_threshold",
    ) if op.get_bind().dialect.name != "sqlite" else None
    op.create_table(
        "llama_score_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey(
            "candidate_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("configuration_id", sa.Integer(), sa.ForeignKey(
            "criteria_configurations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("profile_id", sa.Integer(), sa.ForeignKey(
            "candidate_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("model_name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("score", sa.Integer()),
        sa.Column("review_weight", sa.Integer()),
        sa.Column("breakdown", sa.JSON()),
        sa.Column("error_summary", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "document_id", "configuration_id", "profile_id", "model_name",
            name="uq_llama_scoring_snapshot",
        ),
        sa.CheckConstraint("status IN ('RUNNING', 'COMPLETED', 'FAILED')"),
        sa.CheckConstraint("score IS NULL OR score BETWEEN 0 AND 100"),
    )
    for field in ("document_id", "configuration_id", "profile_id"):
        op.create_index(f"ix_llama_score_results_{field}", "llama_score_results", [field])


def downgrade():
    op.drop_table("llama_score_results")
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint("ck_vacancy_score_bands", "vacancies")
    op.drop_column("vacancies", "medium_threshold")
    op.drop_column("vacancies", "good_threshold")
