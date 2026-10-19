"""Validation issues raised against a submission and the audit trail."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, TimestampMixin, str_enum, utcnow
from app.models.enums import IssueSeverity, IssueStatus


class ValidationIssue(IdMixin, TimestampMixin, Base):
    __tablename__ = "validation_issues"

    submission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), index=True
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(64))  # e.g. "missing_document", "name_mismatch"
    severity: Mapped[IssueSeverity] = mapped_column(str_enum(IssueSeverity))
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[IssueStatus] = mapped_column(str_enum(IssueStatus), default=IssueStatus.OPEN)
    resolved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(IdMixin, Base):
    """Append-only record of who did what.

    `details` must never contain document content or PII."""

    __tablename__ = "audit_events"

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    institution_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("institutions.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(64))  # e.g. "file.stored", "field.corrected"
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(64), index=True)
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
