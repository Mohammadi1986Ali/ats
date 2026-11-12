"""Add automatic durable pipeline stages for each uploaded CV."""

import sqlalchemy as sa
from alembic import op

revision = "b712e1f4a920"
down_revision = "f72e820b4b1c"
branch_labels = None
depends_on = None

STAGES = (
    "TEXT_EXTRACTION",
    "PROFILE_EXTRACTION",
    "EVIDENCE_VALIDATION",
    "MATCHING",
    "SCORING",
    "HR_REVIEW",
)


def upgrade():
    op.create_table(
        "document_pipeline_stages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_summary", sa.String(length=500), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["candidate_documents.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("document_id", "stage", name="uq_document_pipeline_stage"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'WAITING')",
            name="ck_document_pipeline_stage_status",
        ),
    )
    op.create_index(
        "ix_document_pipeline_stages_document_id", "document_pipeline_stages", ["document_id"]
    )
    op.create_index(
        "ix_document_pipeline_stages_status", "document_pipeline_stages", ["status"]
    )
    # Existing source documents become queued without modifying their content.
    # The worker checks the source text cache before doing any extraction.
    for stage in STAGES:
        op.execute(
            sa.text(
                "INSERT INTO document_pipeline_stages "
                "(document_id, stage, status, attempts, updated_at) "
                "SELECT d.id, :stage, :status, 0, CURRENT_TIMESTAMP "
                "FROM candidate_documents d JOIN job_applications a ON a.id = d.application_id "
                "WHERE a.status = 'Uploaded'"
            ).bindparams(
                stage=stage,
                status="PENDING" if stage == "TEXT_EXTRACTION" else "WAITING",
            )
        )


def downgrade():
    op.drop_index("ix_document_pipeline_stages_status", table_name="document_pipeline_stages")
    op.drop_index("ix_document_pipeline_stages_document_id", table_name="document_pipeline_stages")
    op.drop_table("document_pipeline_stages")
