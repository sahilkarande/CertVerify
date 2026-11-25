"""Student sign-in with a one-time code sent by email (no password to remember or leak)."""

import hashlib
import hmac
import re
import secrets
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.auth.service import _aware
from app.db.base import utcnow
from app.models import Applicant, LoginCode, User
from app.models.enums import UserRole
from app.notify.email import EmailSender
from app.services.audit import record_event

CODE_TTL = timedelta(minutes=10)
MAX_GUESSES = 5
MAX_CODES_PER_WINDOW = 3
CODE_WINDOW = timedelta(minutes=15)
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[A-Za-z]{2,}$")


def normalize_email(email: str) -> str | None:
    e = email.strip().lower()
    return e if len(e) <= 320 and EMAIL_RE.match(e) else None


def _hash(key: bytes, email: str, code: str) -> str:
    return hmac.new(key, f"{email}:{code}".encode(), hashlib.sha256).hexdigest()


def request_code(db: Session, sender: EmailSender, key: bytes, email: str, ip: str | None) -> None:
    """Email a fresh code. Silent about whether an account exists, and quietly does
    nothing when the address asked for too many codes recently (so it cannot be used to
    flood someone's inbox)."""
    now = utcnow()
    recent = db.scalar(
        select(func.count())
        .select_from(LoginCode)
        .where(LoginCode.email == email, LoginCode.created_at > now - CODE_WINDOW)
    )
    if recent >= MAX_CODES_PER_WINDOW:
        record_event(db, action="auth.code_throttled", entity_type="login_code")
        return
    staff = db.scalar(select(User).where(User.email == email, User.role != UserRole.STUDENT))
    if staff is not None:
        return  # staff sign in with password + authenticator, never by email code
    code = f"{secrets.randbelow(10**6):06d}"
    # Older unused codes for this address stop working: only the newest is valid.
    db.execute(
        update(LoginCode)
        .where(LoginCode.email == email, LoginCode.used_at.is_(None))
        .values(used_at=now)
    )
    db.add(
        LoginCode(
            email=email,
            code_hash=_hash(key, email, code),
            created_at=now,
            expires_at=now + CODE_TTL,
            ip=ip,
        )
    )
    db.flush()
    sender.send(
        email,
        f"Your CertVerify sign-in code: {code}",
        f"Your sign-in code is {code}\n\nIt works once, for 10 minutes.\n"
        "If you did not ask for it, you can ignore this email.\n",
    )
    record_event(db, action="auth.code_sent", entity_type="login_code")


def verify_code(db: Session, key: bytes, email: str, code: str) -> User | None:
    """The student's account (created on first sign-in) if the code is right."""
    now = utcnow()
    row = db.scalar(
        select(LoginCode)
        .where(LoginCode.email == email, LoginCode.used_at.is_(None))
        .order_by(LoginCode.created_at.desc())
        .limit(1)
    )
    code = code.strip().replace(" ", "")
    if row is None or _aware(row.expires_at) <= now or row.attempts >= MAX_GUESSES:
        return None
    if not hmac.compare_digest(row.code_hash, _hash(key, email, code)):
        row.attempts += 1
        if row.attempts >= MAX_GUESSES:
            row.used_at = now  # burnt: a new code must be requested
        record_event(
            db,
            action="auth.code_failed",
            entity_type="login_code",
            details={"attempts": row.attempts},
        )
        return None
    row.used_at = now
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email, full_name="", role=UserRole.STUDENT, institution_id=None)
        db.add(user)
        db.flush()
        record_event(
            db,
            action="user.created",
            entity_type="user",
            entity_id=user.id,
            actor_user_id=user.id,
            details={"role": "student"},
        )
    elif user.role != UserRole.STUDENT or not user.is_active:
        return None
    # Proving the email links applications made with it (e.g. from the imported form).
    linked = db.execute(
        update(Applicant)
        .where(func.lower(Applicant.email) == email, Applicant.user_id.is_(None))
        .values(user_id=user.id)
    ).rowcount
    if linked:
        record_event(
            db,
            action="applicant.linked",
            entity_type="user",
            entity_id=user.id,
            actor_user_id=user.id,
            details={"applications": linked},
        )
    db.flush()
    return user
