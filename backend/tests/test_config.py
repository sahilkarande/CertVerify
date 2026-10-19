import pytest

from app.core.config import Settings


def test_defaults_use_sqlite() -> None:
    s = Settings(_env_file=None)
    assert s.database_url.startswith("sqlite:///")
    assert s.api_prefix == "/api/v1"


def test_env_vars_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CERTVERIFY_ENVIRONMENT", "production")
    monkeypatch.setenv("CERTVERIFY_LOG_LEVEL", "WARNING")
    s = Settings(_env_file=None)
    assert s.environment == "production"
    assert s.log_level == "WARNING"


def test_invalid_environment_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CERTVERIFY_ENVIRONMENT", "staging-typo")
    with pytest.raises(ValueError):
        Settings(_env_file=None)
