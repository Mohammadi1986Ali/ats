from contextlib import contextmanager

from sqlalchemy import create_engine, event, make_url
from sqlalchemy.orm import sessionmaker


def build_engine(url: str):
    """Keep SQLite tuning local to SQLite; PostgreSQL uses its own connection pool."""
    dialect = make_url(url).get_backend_name()
    if dialect not in {"sqlite", "postgresql"}:
        raise ValueError("Only SQLite and PostgreSQL database URLs are supported.")
    options = {"pool_pre_ping": True}
    if dialect == "sqlite":
        options["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(url, **options)
    if dialect == "sqlite":
        @event.listens_for(engine, "connect")
        def configure(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
    return engine


class Database:
    def __init__(self, url: str):
        self.engine = build_engine(url)
        self.factory = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def transaction(self):
        with self.factory.begin() as session:
            yield session
