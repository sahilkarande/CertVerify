"""Institutions (tenants), platform users and their sign-in sessions."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, TimestampMixin, str_enum
from app.models.enums import UserRole


class Institution(IdMixin, TimestampMixin, Base):
    __tablename__ = "institutions"

    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(64), unique=True)

    users: Mapped[list["User"]] = relationship(back_populates="institution")


class User(IdMixin, TimestampMixin, Base):
    """A platform account. Staff (verifiers, admins) must use an authenticator app."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True)  # stored lower-cased
    full_name: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(str_enum(UserRole))
    # Null only for platform admins; everyone else belongs to exactly one tenant.
    institution_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("institutions.id", ondelete="RESTRICT"), index=True
    )
    password_hash: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(default=True)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Authenticator-app secret, encrypted with the storage key (never stored in clear).
    totp_secret_enc: Mapped[str | None] = mapped_column(String(255))
    totp_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Last accepted 30-second time step: a code cannot be used twice.
    totp_last_step: Mapped[int | None] = mapped_column()
    failed_logins: Mapped[int] = mapped_column(default=0, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    institution: Mapped[Institution | None] = relationship(back_populates="users")


class UserSession(IdMixin, Base):
    """A signed-in browser. The cookie holds a random token; only its SHA-256 is stored, so a
    database leak does not hand out live sessions. Revocable at any time."""

    __tablename__ = "user_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Password checked but the authenticator code not yet: the session can only finish 2FA.
    mfa_passed: Mapped[bool] = mapped_column(default=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(255))

    user: Mapped[User] = relationship()
