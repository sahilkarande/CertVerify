"""Alembic environment. The database URL comes from app settings unless the caller
(e.g. a test) already set `sqlalchemy.url` on the Alembic config."""

from alembic import context

import app.models  # noqa: F401  (registers all tables on Base.metadata)
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import build_engine

config = context.config
target_metadata = Base.metadata

if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", get_settings().database_url)


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


class ForeignKeyViolation(RuntimeError):
    pass


def run_migrations_online() -> None:
    # build_engine also creates the SQLite parent directory if needed.
    engine = build_engine(config.get_main_option("sqlalchemy.url"))
    is_sqlite = engine.dialect.name == "sqlite"
    with engine.connect() as connection:
        if is_sqlite:
            # SQLite batch migrations rebuild a table by copying it, dropping the original and
            # renaming the copy. With foreign keys enforced, that DROP fires ON DELETE CASCADE /
            # SET NULL on every child row (this once emptied page_ocr). Disable enforcement for
            # the migration (must happen outside a transaction) and verify integrity afterwards.
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            # SQLAlchemy 2.0 auto-begins a transaction on the first statement; end it, or
            # Alembic would join it and the whole migration would be rolled back on close.
            connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite cannot ALTER most constraints; batch mode recreates tables instead.
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        if is_sqlite:
            problems = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()
            if problems:
                raise ForeignKeyViolation(f"Foreign key violations after migration: {problems[:5]}")
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
