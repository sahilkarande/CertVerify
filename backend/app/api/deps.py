"""Shared FastAPI dependencies."""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.auth.service import STAFF_ROLES, find_session
from app.core.config import Settings
from app.db.session import Database
from app.models import User, UserSession
from app.models.enums import UserRole
from app.storage import ObjectStorage


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_database(request: Request) -> Database:
    return request.app.state.db


def get_db_session(request: Request) -> Iterator[Session]:
    yield from request.app.state.db.session()


def get_storage(request: Request) -> ObjectStorage:
    return request.app.state.storage


# --- authentication ------------------------------------------------------------------


def get_totp_key(request: Request) -> bytes:
    return request.app.state.totp_key


def get_current_session(
    request: Request,
    db: Annotated[Session, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> UserSession:
    """The signed-in session (password checked; 2FA possibly still pending) or 401."""
    session = find_session(db, request.cookies.get(settings.session_cookie_name))
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Please sign in.")
    db.commit()  # last_seen_at
    return session


def require_staff(*roles: UserRole):
    """Dependency: a fully signed-in staff member (2FA passed), optionally limited to roles."""
    allowed = set(roles) or STAFF_ROLES

    def dep(session: Annotated[UserSession, Depends(get_current_session)]) -> User:
        user = session.user
        if not session.mfa_passed:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Enter your authenticator code.")
        if user.role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Your role cannot do this.")
        return user

    return dep
