"""OCR output per page. Derived data: re-running OCR replaces it."""

import uuid
from typing import Any

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, TimestampMixin, str_enum
from app.models.enums import OcrStatus


class PageOcr(IdMixin, TimestampMixin, Base):
    __tablename__ = "page_ocr"
    __table_args__ = (UniqueConstraint("page_id", "engine"),)

    page_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document_pages.id", ondelete="CASCADE"), index=True
    )
    engine: Mapped[str] = mapped_column(String(32))  # e.g. "tesseract"
    engine_version: Mapped[str] = mapped_column(String(32))
    pipeline_version: Mapped[int] = mapped_column()
    # Clockwise degrees applied to the page image before reading (0/90/180/270).
    rotation: Mapped[int] = mapped_column(default=0)
    status: Mapped[OcrStatus] = mapped_column(str_enum(OcrStatus))
    mean_confidence: Mapped[float | None] = mapped_column()  # 0-100
    word_count: Mapped[int] = mapped_column(default=0)
    text: Mapped[str] = mapped_column(Text, default="")
    # [[text, conf, x, y, w, h, line_key], ...] in the coordinates of the rotated page image,
    # so extraction (Phase 7) can look for values next to their labels.
    words: Mapped[list[Any]] = mapped_column(default=list)
    timing_ms: Mapped[int | None] = mapped_column()

    page: Mapped["DocumentPage"] = relationship()  # noqa: F821
