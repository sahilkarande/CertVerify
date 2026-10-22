import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.models  # noqa: F401  (register tables)
from app.core.config import Settings
from app.core.security import generate_key
from app.db.base import Base
from app.db.session import Database
from app.main import create_app
from app.storage import EncryptedLocalStorage


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        storage_dir=tmp_path / "storage",
        storage_key=generate_key(),
    )


@pytest.fixture
def client(test_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(test_settings)) as c:
        yield c


@pytest.fixture
def database(test_settings: Settings) -> Iterator[Database]:
    db = Database(test_settings.database_url)
    Base.metadata.create_all(db.engine)
    yield db
    db.dispose()


@pytest.fixture
def session(database: Database) -> Iterator[Session]:
    with database.session_factory() as s:
        yield s


@pytest.fixture
def storage(tmp_path: Path) -> EncryptedLocalStorage:
    return EncryptedLocalStorage(tmp_path / "blobs", os.urandom(32))


@pytest.fixture(autouse=True)
def _reset_rate_limits() -> Iterator[None]:
    """Rate limiters are per process; keep tests independent."""
    from app.api.routes.auth import CODE_LIMIT, LOGIN_LIMIT
    from app.api.routes.documents import UPLOAD_LIMIT

    for limiter in (LOGIN_LIMIT, CODE_LIMIT, UPLOAD_LIMIT):
        limiter.reset()
    yield
