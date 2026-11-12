from alembic import context

from job_hire_ai.database.models import Base
from job_hire_ai.database.session import build_engine
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
    engine = build_engine(url)

    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()
