"""Testing Lab (admin): batches of real student documents processed like live uploads to
measure the system, kept apart from real applicants (app/testing/)."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, utcnow


class TestRun(IdMixin, Base):
    __tablename__ = "test_runs"
    __test__ = False  # not a pytest test class

    name: Mapped[str] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)
    # Test students are checked against this checklist, like real ones.
    requirement_set_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("requirement_sets.id", ondelete="RESTRICT"), index=True
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
