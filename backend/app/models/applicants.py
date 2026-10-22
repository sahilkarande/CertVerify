"""Applicants and their submissions against a requirement set."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, TimestampMixin, str_enum, utcnow
from app.models.enums import ApplicantSource, SubmissionStatus


class Applicant(IdMixin, TimestampMixin, Base):
    """A student as known to one institution (tenant-scoped)."""

    __tablename__ = "applicants"
    __table_args__ = (UniqueConstraint("institution_id", "external_ref"),)

    institution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("institutions.id", ondelete="CASCADE"), index=True
    )
    # Linked once the student has a portal account (Phase 11).
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    # Institution's own identifier, e.g. the C-DAC form number.
    external_ref: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    phone: Mapped[str | None] = mapped_column(String(32))
    source: Mapped[ApplicantSource] = mapped_column(str_enum(ApplicantSource))

    submissions: Mapped[list["Submission"]] = relationship(
        back_populates="applicant", cascade="all, delete-orphan"
    )


class Submission(IdMixin, TimestampMixin, Base):
    """One applicant's set of documents for one requirement set."""

    __tablename__ = "submissions"
    __table_args__ = (UniqueConstraint("applicant_id", "requirement_set_id"),)

    applicant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applicants.id", ondelete="CASCADE"), index=True
    )
    requirement_set_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("requirement_sets.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[SubmissionStatus] = mapped_column(
        str_enum(SubmissionStatus), default=SubmissionStatus.DRAFT
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Answers given with the submission that rules may depend on,
    # e.g. {"degree_status": "...", "claimed_aggregate_percent": 75.0}.
    attributes: Mapped[dict[str, Any]] = mapped_column(default=dict)
    # Last verifier decision (approve / reject / request changes), who and why.
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)

    applicant: Mapped[Applicant] = relationship(back_populates="submissions")
    documents: Mapped[list["Document"]] = relationship(  # noqa: F821
        back_populates="submission", cascade="all, delete-orphan"
    )


class SubmissionMessage(IdMixin, Base):
    """A message between verifiers and the student about one application."""

    __tablename__ = "submission_messages"

    submission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), index=True
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    from_staff: Mapped[bool] = mapped_column(default=True)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
