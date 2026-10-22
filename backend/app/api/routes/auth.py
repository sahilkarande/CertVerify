"""Sign-in API for staff: email + password, then an authenticator-app code.

Flow: POST /auth/login -> session cookie in "2FA pending" state ->
  first time:  POST /auth/totp/setup (QR) -> POST /auth/totp/confirm {code}
  afterwards:  POST /auth/totp/verify {code}
Only then do other endpoints accept the session.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import (
    get_app_settings,
    get_current_session,
    get_db_session,
    get_totp_key,
)
from app.auth import service
from app.auth.passwords import password_problems, verify_password
from app.auth.ratelimit import RateLimiter
from app.core.config import Settings
from app.models import UserSession

router = APIRouter(prefix="/auth")
DbSession = Annotated[Session, Depends(get_db_session)]
CurrentSession = Annotated[UserSession, Depends(get_current_session)]
TotpKey = Annotated[bytes, Depends(get_totp_key)]

# Per client address, on top of the per-account lockout stored with the user.
LOGIN_LIMIT = RateLimiter(limit=20, window_s=300)
CODE_LIMIT = RateLimiter(limit=10, window_s=300)
INVALID = "Email or password is incorrect."


class LoginIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=256)


class CodeIn(BaseModel):
    code: str = Field(max_length=12)


class PasswordIn(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _me(session: UserSession) -> dict:
    u = session.user
    return {
        "id": str(u.id),
        "email": u.email,
        "name": u.full_name,
        "role": u.role.value,
        "institution": u.institution.name if u.institution else None,
        "mfa_passed": session.mfa_passed,
        "mfa_enrolled": u.totp_enabled_at is not None,
        "mfa_required": service.requires_mfa(u),
    }


def _set_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        settings.session_cookie_name, token, httponly=True, samesite="strict",
        secure=settings.cookie_secure, path="/", max_age=int(service.SESSION_MAX.total_seconds()),
    )  # fmt: skip


def _limit(limiter: RateLimiter, request: Request) -> None:
    if not limiter.hit(_client(request)):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Wait a few minutes."
        )


@router.post("/login")
def login(
    body: LoginIn,
    request: Request,
    response: Response,
    db: DbSession,
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> dict:
    _limit(LOGIN_LIMIT, request)
    result = service.authenticate(db, body.email, body.password)
    if result.user is None:
        db.commit()
        if result.reason == "locked":
            raise HTTPException(
                status.HTTP_423_LOCKED,
                "Too many failed attempts: this account is locked for a while.",
            )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, INVALID)
    user = result.user
    if user.role not in service.STAFF_ROLES:
        db.commit()  # student sign-in arrives with the student portal (Phase 11)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, INVALID)
    token = service.start_session(
        db, user, mfa_passed=not service.requires_mfa(user), ip=_client(request),
        user_agent=request.headers.get("user-agent"),
    )  # fmt: skip
    db.commit()
    _set_cookie(response, settings, token)
    session = service.find_session(db, token)
    return _me(session)


@router.get("/me")
def me(session: CurrentSession) -> dict:
    return _me(session)


@router.post("/totp/setup")
def totp_setup(session: CurrentSession, db: DbSession, key: TotpKey) -> dict:
    if session.user.totp_enabled_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "An authenticator is already set up.")
    data = service.begin_enrolment(db, session, key)
    db.commit()
    return data


@router.post("/totp/confirm")
def totp_confirm(body: CodeIn, request: Request, session: CurrentSession, db: DbSession,
                 key: TotpKey) -> dict:  # fmt: skip
    _limit(CODE_LIMIT, request)
    ok = service.confirm_enrolment(db, session, key, body.code)
    db.commit()
    if not ok:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "That code is not right. Try the current one."
        )
    return _me(session)


@router.post("/totp/verify")
def totp_verify(body: CodeIn, request: Request, session: CurrentSession, db: DbSession,
                key: TotpKey) -> dict:  # fmt: skip
    _limit(CODE_LIMIT, request)
    if service.is_locked(session.user):
        raise HTTPException(status.HTTP_423_LOCKED, "Too many failed attempts. Try again later.")
    ok = service.complete_mfa(db, session, key, body.code)
    db.commit()
    if not ok:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "That code is not right. Try the current one."
        )
    return _me(session)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    db: DbSession,
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> Response:
    session = service.find_session(db, request.cookies.get(settings.session_cookie_name))
    if session is not None:
        service.revoke(db, session)
        db.commit()
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(settings.session_cookie_name, path="/")
    return response


@router.post("/password")
def change_password(body: PasswordIn, request: Request, session: CurrentSession,
                    db: DbSession) -> dict:  # fmt: skip
    _limit(LOGIN_LIMIT, request)
    if not session.mfa_passed:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Enter your authenticator code.")
    user = session.user
    if not verify_password(user.password_hash, body.current_password)[0]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Your current password is not right.")
    problems = password_problems(body.new_password, user.email)
    if problems:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, " ".join(problems))
    service.set_password(db, user, body.new_password, keep=session)
    db.commit()
    return {"ok": True}
