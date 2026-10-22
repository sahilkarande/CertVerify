"""Background processing jobs, kept in the database (no separate queue service).

A worker claims a queued job with one atomic UPDATE, so several workers never take the
same job; a claim is a lease that another worker may take over if it is not renewed
(the worker crashed). Jobs commit their progress stage by stage, so the uploader sees it.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, str_enum, utcnow
from app.models.enums import JobStage, JobStatus


class Job(IdMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_claim", "status", "run_after"),)

    kind: Mapped[str] = mapped_column(String(32))  # "process_document"
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[JobStatus] = mapped_column(str_enum(JobStatus), default=JobStatus.QUEUED)
    stage: Mapped[JobStage] = mapped_column(str_enum(JobStage), default=JobStage.WAITING)
    priority: Mapped[int] = mapped_column(default=100)  # lower runs first; uploads beat backfills
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=3)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    locked_by: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Error type only (e.g. "WorkerError"), never document content.
    last_error: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
