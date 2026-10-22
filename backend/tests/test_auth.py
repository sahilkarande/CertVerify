import time
from datetime import timedelta

import pyotp
import pytest
from cryptography.exceptions import InvalidTag
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import totp
from app.auth.passwords import hash_password, password_problems, verify_password
from app.auth.ratelimit import RateLimiter
from app.core.config import Settings
from app.db.base import utcnow
from app.main import create_app
from app.models import AuditEvent, User, UserSession
from app.models.enums import UserRole
from tests.auth_helpers import CSRF, PASSWORD, code, make_user, sign_in
from tests.factories import make_institution

AUTH = "/api/v1/auth"


def login(client: TestClient, email: str, password: str = PASSWORD):
    return client.post(f"{AUTH}/login", json={"email": email, "password": password}, headers=CSRF)


@pytest.fixture
def verifier(session: Session) -> User:
    inst = make_institution(session)
    return make_user(session, "asha@cdac.test", institution_id=inst.id)


# ---------- building blocks ----------


def test_password_hashing_and_policy() -> None:
    h = hash_password("a long passphrase here")
    assert h.startswith("$argon2id$")
    assert verify_password(h, "a long passphrase here") == (True, False)
    assert verify_password(h, "wrong")[0] is False
    assert verify_password(None, "anything") == (False, False)  # no account: same work
    assert password_problems("short") and password_problems("aaaaaaaaaaaaaa")
    assert password_problems("asha.patil-2026!", "asha.patil@cdac.test")
    assert password_problems("password1234")
    assert password_problems("correct horse battery", "asha@cdac.test") == []


def test_totp_codes_window_and_replay() -> None:
    secret = totp.new_secret()
    now = time.time()
    t = pyotp.TOTP(secret)
    step = totp.verify_code(secret, t.at(now), None, now)
    assert step == int(now // 30)
    assert totp.verify_code(secret, t.at(now), step, now) is None  # replay refused
    assert totp.verify_code(secret, t.at(now + 30), step, now) == step + 1  # clock drift
    assert totp.verify_code(secret, t.at(now + 120), None, now) is None  # too far off
    assert totp.verify_code(secret, "12345", None, now) is None


def test_totp_secret_encrypted_and_bound_to_user() -> None:
    key = totp.derive_key(b"k" * 32)
    blob = totp.encrypt_secret(key, "user-1", "SECRET")
    assert "SECRET" not in blob
    assert totp.decrypt_secret(key, "user-1", blob) == "SECRET"
    with pytest.raises(InvalidTag):
        totp.decrypt_secret(key, "user-2", blob)


def test_rate_limiter_window() -> None:
    rl = RateLimiter(limit=2, window_s=10)
    assert rl.hit("ip", 0) and rl.hit("ip", 1)
    assert not rl.hit("ip", 2)
    assert rl.hit("ip", 12)  # old hits expired


# ---------- sign-in flow ----------


def test_full_sign_in_with_authenticator(client: TestClient, verifier: User) -> None:
    r = login(client, "ASHA@cdac.test ")  # case and spaces ignored
    assert r.status_code == 200
    assert r.json()["mfa_passed"] is False and r.json()["mfa_enrolled"] is False
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "secure" not in cookie
    assert client.get("/api/v1/review/submissions").status_code == 401

    setup = client.post(f"{AUTH}/totp/setup", headers=CSRF).json()
    assert setup["qr_svg"].startswith("data:image/svg+xml")
    assert "otpauth://totp/CertVerify" in setup["otpauth_uri"]
    bad = client.post(f"{AUTH}/totp/confirm", json={"code": "000000"}, headers=CSRF)
    assert bad.status_code == 400
    ok = client.post(f"{AUTH}/totp/confirm", json={"code": code(setup["secret"])}, headers=CSRF)
    assert ok.json()["mfa_passed"] is True
    assert client.get("/api/v1/review/submissions").status_code == 200
    assert client.post(f"{AUTH}/totp/setup", headers=CSRF).status_code == 409


def test_second_sign_in_uses_the_enrolled_authenticator(client: TestClient, verifier) -> None:
    secret = sign_in(client, "asha@cdac.test")
    client.post(f"{AUTH}/logout", headers=CSRF)
    login(client, "asha@cdac.test")
    used = code(secret)  # already used during enrolment in this 30 s step
    assert client.post(f"{AUTH}/totp/verify", json={"code": used}, headers=CSRF).status_code == 400
    r = client.post(f"{AUTH}/totp/verify", json={"code": code(secret, 1)}, headers=CSRF)
    assert r.status_code == 200 and r.json()["mfa_passed"] is True


def test_wrong_password_and_unknown_email_look_the_same(client: TestClient, verifier) -> None:
    a = login(client, "asha@cdac.test", "wrong password!!")
    b = login(client, "nobody@cdac.test", "wrong password!!")
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json() == {"detail": "Email or password is incorrect."}


def test_lockout_after_repeated_failures(client: TestClient, verifier, session: Session) -> None:
    for _ in range(5):
        login(client, "asha@cdac.test", "wrong password!!")
    assert login(client, "asha@cdac.test").status_code == 423  # even the right password
    session.refresh(verifier)
    verifier.locked_until = utcnow() - timedelta(seconds=1)  # lock has expired
    session.commit()
    assert login(client, "asha@cdac.test").status_code == 200
    session.refresh(verifier)
    assert verifier.failed_logins == 0


def test_too_many_attempts_from_one_address(client: TestClient, verifier) -> None:
    for _ in range(20):
        login(client, "nobody@cdac.test", "wrong password!!")
    assert login(client, "asha@cdac.test").status_code == 429


def test_students_and_disabled_staff_cannot_sign_in(
    client: TestClient, session: Session, verifier: User
) -> None:
    make_user(session, "student@cdac.test", role=UserRole.STUDENT)
    assert login(client, "student@cdac.test").status_code == 401
    verifier.is_active = False
    session.commit()
    assert login(client, "asha@cdac.test").status_code == 401


def test_csrf_header_required_for_changes(client: TestClient, verifier) -> None:
    r = client.post(f"{AUTH}/login", json={"email": "asha@cdac.test", "password": PASSWORD})
    assert r.status_code == 403


def test_security_headers(client: TestClient) -> None:
    h = client.get("/api/v1/health").headers
    assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in h["content-security-policy"]


def test_cookie_secure_in_production(test_settings: Settings, database, session: Session) -> None:
    inst = make_institution(session)
    make_user(session, "asha@cdac.test", institution_id=inst.id)
    prod = test_settings.model_copy(update={"environment": "production"})
    with TestClient(create_app(prod), base_url="https://testserver") as c:
        r = login(c, "asha@cdac.test")
    assert "secure" in r.headers["set-cookie"].lower()


# ---------- sessions ----------


def test_session_token_stored_only_as_hash(client: TestClient, verifier, session: Session) -> None:
    login(client, "asha@cdac.test")
    token = client.cookies.get("cv_session")
    [s] = session.scalars(select(UserSession))
    assert token and token not in s.token_hash and len(s.token_hash) == 64


def test_logout_revokes_the_session(client: TestClient, verifier) -> None:
    sign_in(client, "asha@cdac.test")
    token = client.cookies.get("cv_session")
    assert client.post(f"{AUTH}/logout", headers=CSRF).status_code == 204
    client.cookies.set("cv_session", token)  # replaying the old cookie
    assert client.get(f"{AUTH}/me").status_code == 401


def test_idle_session_expires(client: TestClient, verifier, session: Session) -> None:
    sign_in(client, "asha@cdac.test")
    s = session.scalars(select(UserSession)).one()
    s.last_seen_at = utcnow() - timedelta(hours=3)
    session.commit()
    assert client.get(f"{AUTH}/me").status_code == 401


def test_disabling_an_account_ends_its_sessions(client: TestClient, verifier, session: Session):
    sign_in(client, "asha@cdac.test")
    verifier.is_active = False
    session.commit()
    assert client.get(f"{AUTH}/me").status_code == 401


def test_password_change_signs_out_other_sessions(
    test_settings: Settings, verifier, session: Session
) -> None:
    app = create_app(test_settings)
    with TestClient(app) as a, TestClient(app) as b:
        sign_in(a, "asha@cdac.test")
        secret_user = session.get(User, verifier.id)
        session.refresh(secret_user)
        # second browser: password + code for the next time step
        login(b, "asha@cdac.test")
        key = app.state.totp_key
        secret = totp.decrypt_secret(key, str(verifier.id), secret_user.totp_secret_enc)
        b.post(f"{AUTH}/totp/verify", json={"code": code(secret, 1)}, headers=CSRF)
        assert b.get(f"{AUTH}/me").json()["mfa_passed"] is True

        weak = a.post(
            f"{AUTH}/password",
            headers=CSRF,
            json={"current_password": PASSWORD, "new_password": "short"},
        )
        assert weak.status_code == 400
        r = a.post(
            f"{AUTH}/password",
            headers=CSRF,
            json={"current_password": PASSWORD, "new_password": "another long phrase"},
        )
        assert r.status_code == 200
        assert a.get(f"{AUTH}/me").status_code == 200  # this browser stays signed in
        assert b.get(f"{AUTH}/me").status_code == 401  # the other one is signed out
    assert login(TestClient(app), "asha@cdac.test", "another long phrase").status_code == 200


def test_sign_in_events_are_audited_without_secrets(client: TestClient, verifier, session):
    login(client, "asha@cdac.test", "wrong password!!")
    sign_in(client, "asha@cdac.test")
    actions = [
        e.action for e in session.scalars(select(AuditEvent).order_by(AuditEvent.occurred_at))
    ]
    assert actions[:1] == ["auth.login_failed"]
    assert {"auth.session_started", "auth.totp_enrolled", "auth.mfa_passed"} <= set(actions)
    text = str([e.details for e in session.scalars(select(AuditEvent))])
    assert PASSWORD not in text and "asha@" not in text


def test_session_older_than_a_password_change_is_refused(
    client: TestClient, verifier, session: Session
) -> None:
    """Second line of defence: even a session that was not revoked (e.g. a password reset
    written by another process) stops working once the password changed after it began."""
    sign_in(client, "asha@cdac.test")
    session.refresh(verifier)
    verifier.password_changed_at = utcnow()
    session.commit()
    assert client.get(f"{AUTH}/me").status_code == 401
