"""Sign-in logic: password check with lockout, 2FA, sessions. API-independent."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.auth import totp
from app.auth.passwords import hash_password, verify_password
from app.db.base import utcnow
from app.models import User, UserSession
from app.models.enums import UserRole
from app.services.audit import record_event

STAFF_ROLES = {UserRole.VERIFIER, UserRole.INSTITUTION_ADMIN, UserRole.PLATFORM_ADMIN}

MAX_FAILURES = 5
LOCKOUT = timedelta(minutes=15)
SESSION_IDLE = timedelta(hours=2)
SESSION_MAX = timedelta(hours=12)
# Between password and 2FA code: short, the session can do nothing else.
MFA_PENDING_MAX = timedelta(minutes=10)


def requires_mfa(user: User) -> bool:
    return user.role in STAFF_ROLES


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; all stored times are UTC."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def is_locked(user: User) -> bool:
    locked = _aware(user.locked_until)
    return locked is not None and locked > utcnow()


@dataclass
class LoginResult:
    user: User | None
    reason: str | None = None  # "invalid" | "locked" | "inactive"


def authenticate(db: Session, email: str, password: str) -> LoginResult:
    """Check email + password. Failures never reveal whether the account exists."""
    now = utcnow()
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is not None and is_locked(user):
        verify_password(None, password)  # same work as a real check
        record_event(db, action="auth.login_locked", entity_type="user", entity_id=user.id)
        return LoginResult(None, "locked")
    ok, needs_rehash = verify_password(user.password_hash if user else None, password)
    if user is None:
        record_event(db, action="auth.login_failed", entity_type="user")
        return LoginResult(None, "invalid")
    if not ok:
        user.failed_logins += 1
        if user.failed_logins >= MAX_FAILURES:
            # Doubles with every further failure: 15 min, 30 min, 1 h ... capped at a day.
            factor = 2 ** min(user.failed_logins - MAX_FAILURES, 6)
            user.locked_until = now + min(LOCKOUT * factor, timedelta(days=1))
        record_event(db, action="auth.login_failed", entity_type="user", entity_id=user.id,
                     details={"failures": user.failed_logins})  # fmt: skip
        return LoginResult(None, "invalid")
    if not user.is_active:
        record_event(db, action="auth.login_inactive", entity_type="user", entity_id=user.id)
        return LoginResult(None, "inactive")
    user.failed_logins = 0
    user.locked_until = None
    if needs_rehash:
        user.password_hash = hash_password(password)
    return LoginResult(user)


def start_session(
    db: Session, user: User, *, mfa_passed: bool, ip: str | None, user_agent: str | None
) -> str:
    """Create a session and return its token (goes into the cookie, never stored)."""
    token = secrets.token_urlsafe(32)
    now = utcnow()
    db.add(UserSession(
        user_id=user.id, token_hash=_hash_token(token), created_at=now, last_seen_at=now,
        expires_at=now + (SESSION_MAX if mfa_passed else MFA_PENDING_MAX),
        mfa_passed=mfa_passed, ip=ip, user_agent=(user_agent or "")[:255] or None,
    ))  # fmt: skip
    if mfa_passed:
        user.last_login_at = now
    record_event(db, action="auth.session_started", entity_type="user", entity_id=user.id,
                 actor_user_id=user.id, institution_id=user.institution_id,
                 details={"mfa_passed": mfa_passed})  # fmt: skip
    db.flush()
    return token


def find_session(db: Session, token: str | None) -> UserSession | None:
    """The live session for a cookie token, or None (expired, idle, revoked, user disabled)."""
    if not token:
        return None
    s = db.scalar(select(UserSession).where(UserSession.token_hash == _hash_token(token)))
    now = utcnow()
    if s is None or s.revoked_at is not None or _aware(s.expires_at) <= now:
        return None
    if s.mfa_passed and now - _aware(s.last_seen_at) > SESSION_IDLE:
        return None
    if not s.user.is_active:
        return None
    if s.user.password_changed_at and _aware(s.user.password_changed_at) > _aware(s.created_at):
        return None  # password changed elsewhere after this sign-in
    s.last_seen_at = now
    return s


def complete_mfa(db: Session, session: UserSession, key: bytes, code: str) -> bool:
    """Check an authenticator code for an enrolled user; on success the session is upgraded."""
    user = session.user
    if not user.totp_secret_enc or not user.totp_enabled_at:
        return False
    secret = totp.decrypt_secret(key, str(user.id), user.totp_secret_enc)
    step = totp.verify_code(secret, code, user.totp_last_step)
    if step is None:
        _count_failure(db, user)
        return False
    user.totp_last_step = step
    _upgrade(db, session)
    return True


def begin_enrolment(db: Session, session: UserSession, key: bytes) -> dict:
    """New (unconfirmed) authenticator secret for a user who has none yet."""
    user = session.user
    secret = totp.new_secret()
    user.totp_secret_enc = totp.encrypt_secret(key, str(user.id), secret)
    user.totp_enabled_at = None
    uri = totp.provisioning_uri(secret, user.email)
    db.flush()
    return {"secret": secret, "otpauth_uri": uri, "qr_svg": totp.qr_svg_data_uri(uri)}


def confirm_enrolment(db: Session, session: UserSession, key: bytes, code: str) -> bool:
    user = session.user
    if not user.totp_secret_enc or user.totp_enabled_at:
        return False
    secret = totp.decrypt_secret(key, str(user.id), user.totp_secret_enc)
    step = totp.verify_code(secret, code, None)
    if step is None:
        _count_failure(db, user)
        return False
    user.totp_enabled_at = utcnow()
    user.totp_last_step = step
    record_event(db, action="auth.totp_enrolled", entity_type="user", entity_id=user.id,
                 actor_user_id=user.id, institution_id=user.institution_id)  # fmt: skip
    _upgrade(db, session)
    return True


def _upgrade(db: Session, session: UserSession) -> None:
    now = utcnow()
    session.mfa_passed = True
    session.expires_at = now + SESSION_MAX
    session.last_seen_at = now
    session.user.last_login_at = now
    session.user.failed_logins = 0
    record_event(
        db,
        action="auth.mfa_passed",
        entity_type="user",
        entity_id=session.user_id,
        actor_user_id=session.user_id,
        institution_id=session.user.institution_id,
    )
    db.flush()


def _count_failure(db: Session, user: User) -> None:
    user.failed_logins += 1
    if user.failed_logins >= MAX_FAILURES:
        user.locked_until = utcnow() + LOCKOUT
    record_event(db, action="auth.mfa_failed", entity_type="user", entity_id=user.id,
                 details={"failures": user.failed_logins})  # fmt: skip
    db.flush()


def revoke(db: Session, session: UserSession) -> None:
    session.revoked_at = utcnow()
    record_event(db, action="auth.signed_out", entity_type="user", entity_id=session.user_id,
                 actor_user_id=session.user_id)  # fmt: skip


def revoke_all(db: Session, user: User, *, keep: UserSession | None = None) -> int:
    q = update(UserSession).where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
    if keep is not None:
        q = q.where(UserSession.id != keep.id)
    return db.execute(q.values(revoked_at=utcnow())).rowcount


def set_password(
    db: Session, user: User, password: str, *, keep: UserSession | None = None
) -> None:
    """Set a new password and sign the user out everywhere else."""
    user.password_hash = hash_password(password)
    user.password_changed_at = utcnow()
    user.failed_logins = 0
    user.locked_until = None
    if keep is not None:
        # The current session stays valid: it is re-dated past the change.
        keep.created_at = user.password_changed_at + timedelta(microseconds=1)
    revoke_all(db, user, keep=keep)
    record_event(db, action="auth.password_changed", entity_type="user", entity_id=user.id,
                 actor_user_id=keep.user_id if keep else None)  # fmt: skip
    db.flush()


def reset_mfa(db: Session, user: User) -> None:
    """Admin action when a phone is lost: next sign-in enrols a new authenticator."""
    user.totp_secret_enc = None
    user.totp_enabled_at = None
    user.totp_last_step = None
    revoke_all(db, user)
    record_event(db, action="auth.totp_reset", entity_type="user", entity_id=user.id)
    db.flush()
