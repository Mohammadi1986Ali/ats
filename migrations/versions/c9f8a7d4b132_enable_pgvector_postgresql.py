"""Enable pgvector when using PostgreSQL; no schema change to SQLite."""

from alembic import op

revision = "c9f8a7d4b132"
down_revision = "b712e1f4a920"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade():
    # Do not drop extension: future vector tables or other users may rely on it.
    pass
