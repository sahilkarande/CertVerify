"""Loading of the storage encryption key."""

import base64
import binascii
import logging
import os
import secrets
from pathlib import Path

from app.core.config import Settings

logger = logging.getLogger(__name__)


class ConfigurationError(RuntimeError):
    pass


def decode_key(value: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(value.encode())
    except (binascii.Error, ValueError):
        raise ConfigurationError("CERTVERIFY_STORAGE_KEY is not valid base64") from None
    if len(key) != 32:
        raise ConfigurationError("CERTVERIFY_STORAGE_KEY must decode to exactly 32 bytes")
    return key


def generate_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def load_storage_key(settings: Settings) -> bytes:
    """Return the AES-256 key for file storage.

    Production must set CERTVERIFY_STORAGE_KEY. In development a key file is created once under
    the data directory so encrypted files stay readable across restarts.
    """
    if settings.storage_key is not None:
        return decode_key(settings.storage_key.get_secret_value())

    if settings.environment == "production":
        raise ConfigurationError("CERTVERIFY_STORAGE_KEY must be set in production")

    key_file: Path = settings.storage_dir.parent / ".storage.key"
    if key_file.exists():
        return decode_key(key_file.read_text().strip())

    key_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(generate_key())
    logger.warning(
        "Generated a development storage key at %s. Back it up: losing it makes stored files "
        "unreadable. Set CERTVERIFY_STORAGE_KEY in production.",
        key_file,
    )
    return decode_key(key_file.read_text().strip())
