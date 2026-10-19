import stat
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.security import ConfigurationError, generate_key, load_storage_key


def make_settings(tmp_path: Path, **kw) -> Settings:
    return Settings(_env_file=None, storage_dir=tmp_path / "storage", **kw)


def test_explicit_key_is_used(tmp_path: Path) -> None:
    key = generate_key()
    s = make_settings(tmp_path, storage_key=key)
    assert len(load_storage_key(s)) == 32
    assert not (tmp_path / ".storage.key").exists()


def test_production_requires_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="production"):
        load_storage_key(make_settings(tmp_path, environment="production"))


def test_development_key_file_created_once_and_reused(tmp_path: Path) -> None:
    s = make_settings(tmp_path, environment="development")
    first = load_storage_key(s)
    key_file = tmp_path / ".storage.key"
    assert key_file.exists()
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert load_storage_key(s) == first


@pytest.mark.parametrize("bad", ["not-base64!!", "c2hvcnQ="])
def test_invalid_key_rejected(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ConfigurationError):
        load_storage_key(make_settings(tmp_path, storage_key=bad))
