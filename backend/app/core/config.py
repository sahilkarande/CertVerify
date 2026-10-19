"""Application settings, loaded from environment variables and an optional .env file."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
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

    # Root for encrypted document blobs.
    storage_dir: Path = BACKEND_DIR / "data" / "storage"
    # Base64 (url-safe) of 32 random bytes. Required in production; see app/core/security.py.
    storage_key: SecretStr | None = None

    # Seed-data importer: Google OAuth client (downloaded from Google Cloud) and the saved
    # sign-in token. Both live in the git-ignored data directory.
    google_client_secret_file: Path = BACKEND_DIR / "data" / "google_client_secret.json"
    google_token_file: Path = BACKEND_DIR / "data" / "google_token.json"
    # Respondent -> form-number mapping reviewed by a person before a folder import (has PII).
    folder_mapping_file: Path = BACKEND_DIR / "data" / "folder_mapping.csv"
    # Hand-labelled OCR ground truth (contains PII; git-ignored).
    ocr_ground_truth_file: Path = BACKEND_DIR / "data" / "ocr_eval" / "ground_truth.json"


@lru_cache
def get_settings() -> Settings:
    return Settings()
