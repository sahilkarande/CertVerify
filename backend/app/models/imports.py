"""Remote files referenced by an import (e.g. Google Drive links in a form export) that still
have to be fetched. Once downloaded, a ref points at the Document created from it."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, TimestampMixin, str_enum
from app.models.enums import ImportFileStatus


class ImportFileRef(IdMixin, TimestampMixin, Base):
    __tablename__ = "import_file_refs"
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    submission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), index=True
    )
    slot_key: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(32))  # e.g. "gdrive"
    external_id: Mapped[str] = mapped_column(String(255))  # e.g. Drive file id
    status: Mapped[ImportFileStatus] = mapped_column(
        str_enum(ImportFileStatus), default=ImportFileStatus.PENDING, index=True
    )
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL")
    )

    submission: Mapped["Submission"] = relationship()  # noqa: F821
