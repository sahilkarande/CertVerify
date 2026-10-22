"""Create staff accounts and sign them in through the real API (password + authenticator)."""

import time

import pyotp
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth.passwords import hash_password
from app.models import User
from app.models.enums import UserRole

CSRF = {"X-Requested-With": "CertVerify"}
PASSWORD = "correct horse battery"


def make_user(
    db: Session,
    email: str,
    role: UserRole = UserRole.VERIFIER,
    institution_id=None,
    password: str = PASSWORD,
) -> User:
    user = User(
        email=email,
        full_name="Test Person",
        role=role,
        institution_id=institution_id,
        password_hash=hash_password(password),
    )
    db.add(user)
    db.commit()
    return user


def code(secret: str, offset_steps: int = 0) -> str:
    return pyotp.TOTP(secret).at(time.time() + 30 * offset_steps)


def sign_in(client: TestClient, email: str, password: str = PASSWORD) -> str:
    """Password, then enrol an authenticator and confirm it. Returns the TOTP secret."""
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password}, headers=CSRF)
    assert r.status_code == 200, r.text
    secret = client.post("/api/v1/auth/totp/setup", headers=CSRF).json()["secret"]
    r = client.post("/api/v1/auth/totp/confirm", json={"code": code(secret)}, headers=CSRF)
    assert r.status_code == 200, r.text
    return secret
