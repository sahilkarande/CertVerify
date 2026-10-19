"""Database engine and session management."""

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker


def build_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    connect_args: dict = {}

    if url.get_backend_name() == "sqlite":
        # FastAPI may use a session from a different thread than the one that created it.
        connect_args["check_same_thread"] = False
        if url.database and url.database != ":memory:":
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)

    if url.get_backend_name() == "sqlite":
        # SQLite ignores foreign keys (and ON DELETE rules) unless enabled per connection.
        @event.listens_for(engine, "connect")
        def _enable_sqlite_fks(dbapi_conn, _record) -> None:
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


class Database:
    """Owns the engine and session factory for one application instance."""

    def __init__(self, database_url: str) -> None:
        self.engine = build_engine(database_url)
        self.session_factory = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )

    def session(self) -> Iterator[Session]:
        db = self.session_factory()
        try:
            yield db
        finally:
            db.close()

    def ping(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    def dispose(self) -> None:
        self.engine.dispose()
