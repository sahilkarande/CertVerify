"""Institutions (tenants) and platform users."""

import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, TimestampMixin, str_enum
from app.models.enums import UserRole


class Institution(IdMixin, TimestampMixin, Base):
    __tablename__ = "institutions"

    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(64), unique=True)

    users: Mapped[list["User"]] = relationship(back_populates="institution")


class User(IdMixin, TimestampMixin, Base):
    """A platform account. Authentication (passwords, OTP, sessions) arrives in Phase 9."""

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

    institution: Mapped[Institution | None] = relationship(back_populates="users")
