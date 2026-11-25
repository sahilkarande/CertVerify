"""Authenticator-app codes (TOTP, RFC 6238): 6 digits, 30-second steps."""

import base64
import os
import time

import pyotp
import segno
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ISSUER = "CertVerify"
# Accept the previous and next step too: phone clocks drift and people type slowly.
VALID_WINDOW = 1


def derive_key(master_key: bytes, purpose: str = "totp") -> bytes:
    """A separate key per purpose, derived from the storage master key (a flaw in one use
    cannot expose the others)."""
    info = f"certverify-{purpose}".encode()
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(master_key)


def new_secret() -> str:
    return pyotp.random_base32()


def encrypt_secret(key: bytes, user_id: str, secret: str) -> str:
    """AES-256-GCM with the user id bound in, so a secret cannot be moved to another user."""
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, secret.encode(), f"totp:{user_id}".encode())
    return base64.urlsafe_b64encode(nonce + ct).decode()


def decrypt_secret(key: bytes, user_id: str, blob: str) -> str:
    raw = base64.urlsafe_b64decode(blob)
    return AESGCM(key).decrypt(raw[:12], raw[12:], f"totp:{user_id}".encode()).decode()


def provisioning_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER)


def qr_svg_data_uri(uri: str) -> str:
    return segno.make(uri, error="m").svg_data_uri(scale=5, border=2)


def verify_code(
    secret: str, code: str, last_step: int | None, now: float | None = None
) -> int | None:
    """The accepted time step, or None. A step at or before ``last_step`` is refused, so an
    observed code cannot be replayed."""
    code = code.strip().replace(" ", "")
    if not (code.isdigit() and len(code) == 6):
        return None
    totp = pyotp.TOTP(secret)
    now = time.time() if now is None else now
    current = int(now // totp.interval)
    for step in range(current - VALID_WINDOW, current + VALID_WINDOW + 1):
        if last_step is not None and step <= last_step:
            continue
        if pyotp.utils.strings_equal(totp.generate_otp(step), code):
            return step
    return None
