"""Application settings, loaded from environment variables and an optional .env file."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        env_prefix="CERTVERIFY_",
        extra="ignore",
    )

    app_name: str = "CertVerify"
    environment: Literal["development", "test", "production"] = "development"
    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    api_prefix: str = "/api/v1"

    # SQLite by default so the app runs with zero external services.
    # Switch to PostgreSQL later, e.g. postgresql+psycopg://user:pass@localhost:5432/certverify
    database_url: str = Field(default=f"sqlite:///{BACKEND_DIR / 'data' / 'certverify.db'}")

    # Local storage root for downloaded / processed documents (used from Phase 3 on).
    storage_dir: Path = BACKEND_DIR / "data" / "storage"


@lru_cache
def get_settings() -> Settings:
    return Settings()
