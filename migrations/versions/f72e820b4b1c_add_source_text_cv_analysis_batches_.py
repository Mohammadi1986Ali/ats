"""Add source text CV analysis batches profiles evidence and reviews"""

import sqlalchemy as sa
from alembic import op

revision = "f72e820b4b1c"
down_revision = "0e8846979a09"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "cv_analysis_batches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("vacancy_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["vacancy_id"],
            ["vacancies.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("cv_analysis_batches", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_cv_analysis_batches_vacancy_id"), ["vacancy_id"], unique=False
        )

    op.create_table(
        "cv_analysis_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("force_reanalysis", sa.Boolean(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("worker_token", sa.String(length=32), nullable=True),
        sa.Column("lease_until", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_summary", sa.String(length=500), nullable=True),
        sa.Column("reused_profile", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING', 'EXTRACTING', 'ANALYZING', 'VALIDATING', "
            "'COMPLETED', 'FAILED', 'NEEDS_REVIEW')"
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["cv_analysis_batches.id"],
        ),
        sa.ForeignKeyConstraint(["document_id"], ["candidate_documents.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "document_id"),
    )
    with op.batch_alter_table("cv_analysis_jobs", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_cv_analysis_jobs_batch_id"), ["batch_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_cv_analysis_jobs_document_id"), ["document_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_cv_analysis_jobs_status"), ["status"], unique=False)

    op.create_table(
        "document_text_extractions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("extractor_version", sa.String(length=100), nullable=False),
        sa.Column("source", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["candidate_documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "checksum", "extractor_version"),
    )
    with op.batch_alter_table("document_text_extractions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_document_text_extractions_document_id"), ["document_id"], unique=False
        )

    op.create_table(
        "cv_analysis_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=True),
        sa.Column("document_checksum", sa.String(length=64), nullable=False),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("model_digest", sa.String(length=200), nullable=True),
        sa.Column("prompt_version", sa.String(length=100), nullable=False),
        sa.Column("schema_version", sa.String(length=100), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("inference_options", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("chunks_completed", sa.Integer(), nullable=False),
        sa.Column("error_summary", sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(["document_id"], ["candidate_documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["cv_analysis_jobs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("cv_analysis_runs", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_cv_analysis_runs_document_id"), ["document_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_cv_analysis_runs_job_id"), ["job_id"], unique=False)

    op.create_table(
        "candidate_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("text_extraction_id", sa.Integer(), nullable=False),
        sa.Column("analysis_run_id", sa.Integer(), nullable=False),
        sa.Column("extraction_version", sa.Integer(), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("analysis_status", sa.String(length=20), nullable=False),
        sa.Column("original_extraction", sa.JSON(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("analysis_status IN ('COMPLETED', 'NEEDS_REVIEW')"),
        sa.CheckConstraint("extraction_version > 0"),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"],
            ["cv_analysis_runs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidates.id"],
        ),
        sa.ForeignKeyConstraint(["document_id"], ["candidate_documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["text_extraction_id"], ["document_text_extractions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("analysis_run_id"),
        sa.UniqueConstraint("document_id", "extraction_version"),
    )
    with op.batch_alter_table("candidate_profiles", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_candidate_profiles_candidate_id"), ["candidate_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_candidate_profiles_config_hash"), ["config_hash"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_candidate_profiles_document_id"), ["document_id"], unique=False
        )

    op.create_table(
        "evidence_references",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("field_path", sa.String(length=200), nullable=False),
        sa.Column("source_excerpt", sa.String(), nullable=False),
        sa.Column("source_reference", sa.String(length=200), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("character_start", sa.Integer(), nullable=True),
        sa.Column("character_end", sa.Integer(), nullable=True),
        sa.Column("verification_status", sa.String(length=20), nullable=False),
        sa.CheckConstraint("verification_status IN ('VERIFIED', 'UNVERIFIED', 'AMBIGUOUS')"),
        sa.ForeignKeyConstraint(["document_id"], ["candidate_documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["profile_id"], ["candidate_profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("evidence_references", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_evidence_references_document_id"), ["document_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_evidence_references_profile_id"), ["profile_id"], unique=False
        )

    op.create_table(
        "profile_review_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("field_path", sa.String(length=200), nullable=False),
        sa.Column("original_value", sa.JSON(), nullable=True),
        sa.Column("corrected_value", sa.JSON(), nullable=True),
        sa.Column("reviewer", sa.String(length=200), nullable=True),
        sa.Column("note", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["candidate_profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("profile_review_events", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_profile_review_events_profile_id"), ["profile_id"], unique=False
        )

    if op.get_bind().dialect.name == "postgresql":
        op.execute("""
            CREATE FUNCTION ats_protect_extraction_history() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'Extraction history is immutable';
            END;
            $$
        """)
    for table in (
        "candidate_profiles",
        "document_text_extractions",
        "evidence_references",
        "profile_review_events",
    ):
        if op.get_bind().dialect.name == "postgresql":
            op.execute(
                f"CREATE TRIGGER immutable_{table}_update BEFORE UPDATE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION ats_protect_extraction_history()"
            )
        else:
            op.execute(f"""CREATE TRIGGER immutable_{table}_update BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT, 'Extraction history is immutable'); END""")


def downgrade():
    for table in (
        "candidate_profiles",
        "document_text_extractions",
        "evidence_references",
        "profile_review_events",
    ):
        if op.get_bind().dialect.name == "postgresql":
            op.execute(f"DROP TRIGGER immutable_{table}_update ON {table}")
        else:
            op.execute(f"DROP TRIGGER immutable_{table}_update")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION ats_protect_extraction_history()")
    with op.batch_alter_table("profile_review_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_profile_review_events_profile_id"))

    op.drop_table("profile_review_events")
    with op.batch_alter_table("evidence_references", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_evidence_references_profile_id"))
        batch_op.drop_index(batch_op.f("ix_evidence_references_document_id"))

    op.drop_table("evidence_references")
    with op.batch_alter_table("candidate_profiles", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_candidate_profiles_document_id"))
        batch_op.drop_index(batch_op.f("ix_candidate_profiles_config_hash"))
        batch_op.drop_index(batch_op.f("ix_candidate_profiles_candidate_id"))

    op.drop_table("candidate_profiles")
    with op.batch_alter_table("cv_analysis_runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_cv_analysis_runs_job_id"))
        batch_op.drop_index(batch_op.f("ix_cv_analysis_runs_document_id"))

    op.drop_table("cv_analysis_runs")
    with op.batch_alter_table("document_text_extractions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_document_text_extractions_document_id"))

    op.drop_table("document_text_extractions")
    with op.batch_alter_table("cv_analysis_jobs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_cv_analysis_jobs_status"))
        batch_op.drop_index(batch_op.f("ix_cv_analysis_jobs_document_id"))
        batch_op.drop_index(batch_op.f("ix_cv_analysis_jobs_batch_id"))

    op.drop_table("cv_analysis_jobs")
    with op.batch_alter_table("cv_analysis_batches", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_cv_analysis_batches_vacancy_id"))

    op.drop_table("cv_analysis_batches")
