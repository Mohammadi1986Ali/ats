from alembic import context
from sqlalchemy import create_engine, event, pool

from job_hire_ai.database.models import Base
from job_hire_ai.settings import Settings

config = context.config
settings = Settings()
settings.directory.mkdir(parents=True, exist_ok=True)
url = config.attributes.get("database_url", settings.database_url)

if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, poolclass=pool.NullPool)

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=Base.metadata, render_as_batch=True
        )
        with context.begin_transaction():
            context.run_migrations()
