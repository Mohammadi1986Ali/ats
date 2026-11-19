"""Record the JD-time weighting choice without changing existing applications."""

import sqlalchemy as sa
from alembic import op

revision = "f58a1b0d2319"
down_revision = "e83af9d27410"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "vacancies",
        sa.Column("weighting_mode", sa.String(20), nullable=False,
                  server_default="MANUAL"),
    )
    op.add_column(
        "vacancies",
        sa.Column("weighting_rules", sa.Text(), nullable=False, server_default=""),
    )


def downgrade():
    # Direct DROP COLUMN preserves SQLite foreign keys from existing applications.
    op.drop_column("vacancies", "weighting_rules")
    op.drop_column("vacancies", "weighting_mode")
