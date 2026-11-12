"""Initial basic ATS schema"""

import sqlalchemy as sa
from alembic import op

revision = "5e9be2fa29c7"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "candidates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "vacancies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("department", sa.String(length=200), nullable=True),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("status IN ('Active', 'Archived')"),
        sa.CheckConstraint("length(trim(description)) > 0"),
        sa.CheckConstraint("length(trim(title)) > 0"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("vacancies", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_vacancies_status"), ["status"], unique=False)

    op.create_table(
        "job_applications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.Integer(), nullable=False),
        sa.Column("vacancy_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("status IN ('Uploaded', 'Removed')"),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidates.id"],
        ),
        sa.ForeignKeyConstraint(
            ["vacancy_id"],
            ["vacancies.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "vacancy_id"),
    )
    with op.batch_alter_table("job_applications", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_job_applications_candidate_id"), ["candidate_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_job_applications_status"), ["status"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_job_applications_vacancy_id"), ["vacancy_id"], unique=False
        )

    op.create_table(
        "candidate_documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("application_id", sa.Integer(), nullable=False),
        sa.Column("vacancy_id", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("storage_key", sa.String(length=64), nullable=False),
        sa.Column("file_type", sa.String(length=8), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("upload_timestamp", sa.DateTime(), nullable=False),
        sa.Column("text_extraction_status", sa.String(length=20), nullable=False),
        sa.CheckConstraint("file_type IN ('pdf', 'docx')"),
        sa.CheckConstraint("text_extraction_status IN ('Available', 'Unavailable')"),
        sa.CheckConstraint("file_size > 0"),
        sa.ForeignKeyConstraint(
            ["application_id", "vacancy_id"],
            ["job_applications.id", "job_applications.vacancy_id"],
        ),
        sa.ForeignKeyConstraint(
            ["vacancy_id"],
            ["vacancies.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key"),
        sa.UniqueConstraint("vacancy_id", "checksum", name="uq_vacancy_checksum"),
    )
    with op.batch_alter_table("candidate_documents", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_candidate_documents_application_id"), ["application_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_candidate_documents_upload_timestamp"),
            ["upload_timestamp"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_candidate_documents_vacancy_id"), ["vacancy_id"], unique=False
        )


def downgrade():
    with op.batch_alter_table("candidate_documents", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_candidate_documents_vacancy_id"))
        batch_op.drop_index(batch_op.f("ix_candidate_documents_upload_timestamp"))
        batch_op.drop_index(batch_op.f("ix_candidate_documents_application_id"))

    op.drop_table("candidate_documents")
    with op.batch_alter_table("job_applications", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_job_applications_vacancy_id"))
        batch_op.drop_index(batch_op.f("ix_job_applications_status"))
        batch_op.drop_index(batch_op.f("ix_job_applications_candidate_id"))

    op.drop_table("job_applications")
    with op.batch_alter_table("vacancies", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_vacancies_status"))

    op.drop_table("vacancies")
    op.drop_table("candidates")
