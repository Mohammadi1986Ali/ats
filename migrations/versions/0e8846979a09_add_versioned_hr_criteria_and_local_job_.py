"""Add versioned HR criteria and local job analysis audit"""

import sqlalchemy as sa
from alembic import op

revision = "0e8846979a09"
down_revision = "5e9be2fa29c7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "job_analysis_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("vacancy_id", sa.Integer(), nullable=False),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("model_digest", sa.String(length=200), nullable=True),
        sa.Column("prompt_version", sa.String(length=100), nullable=False),
        sa.Column("input_description_hash", sa.String(length=64), nullable=False),
        sa.Column("inference_options", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error_summary", sa.String(length=500), nullable=True),
        sa.CheckConstraint("status IN ('RUNNING', 'SUCCEEDED', 'FAILED')"),
        sa.ForeignKeyConstraint(
            ["vacancy_id"],
            ["vacancies.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("job_analysis_runs", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_job_analysis_runs_vacancy_id"), ["vacancy_id"], unique=False
        )

    op.create_table(
        "criteria_configurations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("vacancy_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("job_description_hash", sa.String(length=64), nullable=False),
        sa.Column("job_description_snapshot", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("approved_by", sa.String(length=200), nullable=True),
        sa.Column("analysis_run_id", sa.Integer(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("status = 'DRAFT' OR approved_at IS NOT NULL"),
        sa.CheckConstraint("status IN ('DRAFT', 'APPROVED', 'SUPERSEDED')"),
        sa.CheckConstraint("version > 0"),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"],
            ["job_analysis_runs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["vacancy_id"],
            ["vacancies.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("vacancy_id", "version"),
    )
    with op.batch_alter_table("criteria_configurations", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_criteria_configurations_vacancy_id"), ["vacancy_id"], unique=False
        )
        batch_op.create_index(
            "uq_current_approved",
            ["vacancy_id"],
            unique=True,
            sqlite_where=sa.text("status = 'APPROVED'"),
            postgresql_where=sa.text("status = 'APPROVED'"),
        )
        batch_op.create_index(
            "uq_current_draft",
            ["vacancy_id"],
            unique=True,
            sqlite_where=sa.text("status = 'DRAFT'"),
            postgresql_where=sa.text("status = 'DRAFT'"),
        )

    op.create_table(
        "job_criteria",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("configuration_id", sa.Integer(), nullable=False),
        sa.Column("criterion_key", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("requirement_type", sa.String(length=20), nullable=False),
        sa.Column("source_excerpt", sa.String(), nullable=False),
        sa.Column("extraction_notes", sa.String(), nullable=False),
        sa.Column("uncertainty", sa.String(), nullable=False),
        sa.Column("evidence_status", sa.String(length=20), nullable=False),
        sa.Column("evidence_acknowledged", sa.Boolean(), nullable=False),
        sa.Column("acknowledgement_note", sa.String(), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=20), nullable=False),
        sa.Column("original_extraction", sa.JSON(), nullable=True),
        sa.CheckConstraint("created_by IN ('AI', 'AI_MODIFIED', 'MANUAL')"),
        sa.CheckConstraint("evidence_status IN ('VERIFIED', 'UNVERIFIED')"),
        sa.CheckConstraint("requirement_type IN ('REQUIRED', 'PREFERRED', 'UNCLEAR')"),
        sa.CheckConstraint(
            "weight IS NULL OR "
            + ("typeof(weight) = 'integer' AND " if op.get_bind().dialect.name == "sqlite" else "")
            + "weight BETWEEN 0 AND 100"
        ),
        sa.CheckConstraint("display_order BETWEEN 1 AND 50"),
        sa.CheckConstraint("length(trim(name)) > 0 AND length(trim(description)) > 0"),
        sa.ForeignKeyConstraint(
            ["configuration_id"],
            ["criteria_configurations.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("configuration_id", "criterion_key"),
    )
    with op.batch_alter_table("job_criteria", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_job_criteria_configuration_id"), ["configuration_id"], unique=False
        )

    # Keep the original SQLite triggers; PostgreSQL uses native PL/pgSQL triggers.
    if op.get_bind().dialect.name == "postgresql":
        _install_postgresql_triggers()
        return

    # Enforce historical immutability even for direct SQL/ORM writes.
    for operation in ("INSERT", "UPDATE", "DELETE"):
        ref = "NEW" if operation == "INSERT" else "OLD"
        extra = (
            ""
            if operation != "UPDATE"
            else """ OR EXISTS (
            SELECT 1 FROM criteria_configurations
            WHERE id = NEW.configuration_id AND status != 'DRAFT')"""
        )
        op.execute(f"""CREATE TRIGGER immutable_criterion_{operation.lower()}
            BEFORE {operation} ON job_criteria
            WHEN EXISTS (SELECT 1 FROM criteria_configurations
                         WHERE id = {ref}.configuration_id AND status != 'DRAFT') {extra}
            BEGIN SELECT RAISE(ABORT, 'Approved criteria are immutable'); END""")
    op.execute("""CREATE TRIGGER immutable_configuration_delete
        BEFORE DELETE ON criteria_configurations WHEN OLD.status != 'DRAFT'
        BEGIN SELECT RAISE(ABORT, 'Approved configurations are immutable'); END""")
    op.execute("""CREATE TRIGGER immutable_configuration_update
        BEFORE UPDATE ON criteria_configurations WHEN OLD.status != 'DRAFT' AND NOT (
            OLD.status = 'APPROVED' AND NEW.status = 'SUPERSEDED'
            AND NEW.id = OLD.id AND NEW.vacancy_id = OLD.vacancy_id
            AND NEW.version = OLD.version AND NEW.created_at = OLD.created_at
            AND NEW.job_description_hash = OLD.job_description_hash
            AND NEW.job_description_snapshot = OLD.job_description_snapshot
            AND NEW.approved_at IS OLD.approved_at AND NEW.approved_by IS OLD.approved_by
            AND NEW.analysis_run_id IS OLD.analysis_run_id AND NEW.revision = OLD.revision + 1
        ) BEGIN SELECT RAISE(ABORT, 'Approved configurations are immutable'); END""")
    op.execute("""CREATE TRIGGER validate_configuration_approval
        BEFORE UPDATE OF status ON criteria_configurations
        WHEN NEW.status = 'APPROVED' AND (
            NOT EXISTS (SELECT 1 FROM job_criteria WHERE configuration_id = NEW.id)
            OR (SELECT coalesce(sum(weight), 0) FROM job_criteria
                WHERE configuration_id = NEW.id) != 100
            OR EXISTS (SELECT 1 FROM job_criteria WHERE configuration_id = NEW.id AND (
                weight IS NULL OR (evidence_status != 'VERIFIED' AND
                (evidence_acknowledged = 0 OR length(trim(acknowledgement_note)) = 0))))
            OR NEW.job_description_snapshot != (
                SELECT description FROM vacancies WHERE id = NEW.vacancy_id)
        ) BEGIN SELECT RAISE(ABORT, 'Invalid approval configuration'); END""")


def _install_postgresql_triggers():
    op.execute("""
        CREATE FUNCTION ats_protect_criterion() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            configuration_key integer;
        BEGIN
            configuration_key := CASE WHEN TG_OP = 'DELETE' THEN OLD.configuration_id
                                       ELSE NEW.configuration_id END;
            IF EXISTS (
                SELECT 1 FROM criteria_configurations
                WHERE id = configuration_key AND status != 'DRAFT'
            ) THEN
                RAISE EXCEPTION 'Approved criteria are immutable';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    for operation in ("INSERT", "UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER immutable_criterion_{operation.lower()} "
            f"BEFORE {operation} ON job_criteria "
            "FOR EACH ROW EXECUTE FUNCTION ats_protect_criterion()"
        )
    op.execute("""
        CREATE FUNCTION ats_protect_configuration() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.status != 'DRAFT' THEN
                    RAISE EXCEPTION 'Approved configurations are immutable';
                END IF;
                RETURN OLD;
            END IF;
            IF OLD.status != 'DRAFT' AND NOT (
                OLD.status = 'APPROVED' AND NEW.status = 'SUPERSEDED'
                AND NEW.id = OLD.id AND NEW.vacancy_id = OLD.vacancy_id
                AND NEW.version = OLD.version AND NEW.created_at = OLD.created_at
                AND NEW.job_description_hash = OLD.job_description_hash
                AND NEW.job_description_snapshot = OLD.job_description_snapshot
                AND NEW.approved_at IS NOT DISTINCT FROM OLD.approved_at
                AND NEW.approved_by IS NOT DISTINCT FROM OLD.approved_by
                AND NEW.analysis_run_id IS NOT DISTINCT FROM OLD.analysis_run_id
                AND NEW.revision = OLD.revision + 1
            ) THEN
                RAISE EXCEPTION 'Approved configurations are immutable';
            END IF;
            IF NEW.status = 'APPROVED' AND OLD.status != 'APPROVED' AND (
                NOT EXISTS (SELECT 1 FROM job_criteria WHERE configuration_id = NEW.id)
                OR (SELECT COALESCE(SUM(weight), 0) FROM job_criteria
                    WHERE configuration_id = NEW.id) != 100
                OR EXISTS (SELECT 1 FROM job_criteria WHERE configuration_id = NEW.id
                    AND (weight IS NULL OR (evidence_status != 'VERIFIED' AND
                    (evidence_acknowledged IS FALSE OR
                     LENGTH(TRIM(acknowledgement_note)) = 0))))
                OR NEW.job_description_snapshot != (
                    SELECT description FROM vacancies WHERE id = NEW.vacancy_id)
            ) THEN
                RAISE EXCEPTION 'Invalid approval configuration';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    for operation in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER immutable_configuration_{operation.lower()} "
            f"BEFORE {operation} ON criteria_configurations "
            "FOR EACH ROW EXECUTE FUNCTION ats_protect_configuration()"
        )


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        for operation in ("INSERT", "UPDATE", "DELETE"):
            op.execute(f"DROP TRIGGER immutable_criterion_{operation.lower()} ON job_criteria")
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"DROP TRIGGER immutable_configuration_{operation.lower()} "
                "ON criteria_configurations"
            )
        op.execute("DROP FUNCTION ats_protect_criterion()")
        op.execute("DROP FUNCTION ats_protect_configuration()")
    else:
        for name in (
            "immutable_criterion_insert",
            "immutable_criterion_update",
            "immutable_criterion_delete",
            "immutable_configuration_delete",
            "immutable_configuration_update",
            "validate_configuration_approval",
        ):
            op.execute(f"DROP TRIGGER {name}")
    with op.batch_alter_table("job_criteria", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_job_criteria_configuration_id"))

    op.drop_table("job_criteria")
    with op.batch_alter_table("criteria_configurations", schema=None) as batch_op:
        batch_op.drop_index("uq_current_draft", sqlite_where=sa.text("status = 'DRAFT'"))
        batch_op.drop_index("uq_current_approved", sqlite_where=sa.text("status = 'APPROVED'"))
        batch_op.drop_index(batch_op.f("ix_criteria_configurations_vacancy_id"))

    op.drop_table("criteria_configurations")
    with op.batch_alter_table("job_analysis_runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_job_analysis_runs_vacancy_id"))

    op.drop_table("job_analysis_runs")
