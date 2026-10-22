"""Trained models: versions, where their file is, how they scored, which one is in use."""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, utcnow


class MlModel(IdMixin, Base):
    __tablename__ = "ml_models"
    __table_args__ = (UniqueConstraint("kind", "version"),)

    kind: Mapped[str] = mapped_column(String(32))  # "page_type"
    version: Mapped[int] = mapped_column()
    # File name inside the models directory (JSON numbers, never pickles).
    file_name: Mapped[str] = mapped_column(String(128))
    sha256: Mapped[str] = mapped_column(String(64))  # checked when loading
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Training data counts and held-out results, e.g. {"train": {...}, "heldout_correct": 79}.
    metrics: Mapped[dict[str, Any]] = mapped_column(default=dict)
    active: Mapped[bool] = mapped_column(default=False)
