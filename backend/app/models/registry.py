"""Document-type registry and per-institution requirement sets.

Document types are data, not code: adding a new type means adding a row (plus, in later
phases, its recognition/extraction config and training examples).
"""

import uuid
from typing import Any

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, TimestampMixin, str_enum
from app.models.enums import DocumentCategory


class DocumentType(IdMixin, TimestampMixin, Base):
    __tablename__ = "document_types"

    code: Mapped[str] = mapped_column(String(64), unique=True)  # e.g. "semester_marksheet"
    name: Mapped[str] = mapped_column(String(255))
    category: Mapped[DocumentCategory] = mapped_column(str_enum(DocumentCategory))
    description: Mapped[str | None] = mapped_column(Text)
    # Fields expected on this type: [{"key", "label", "type", "required"}, ...]
    field_schema: Mapped[list[Any]] = mapped_column(default=list)
    is_active: Mapped[bool] = mapped_column(default=True)


class RequirementSet(IdMixin, TimestampMixin, Base):
    """What one institution requires for one intake, e.g. "C-DAC PG admission Feb 2026"."""

    __tablename__ = "requirement_sets"
    __table_args__ = (UniqueConstraint("institution_id", "code"),)

    institution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("institutions.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(default=True)

    items: Mapped[list["RequirementItem"]] = relationship(
        back_populates="requirement_set",
        cascade="all, delete-orphan",
        order_by="RequirementItem.position",
    )


class RequirementItem(IdMixin, Base):
    """One slot in a requirement set.

    `accepted_type_codes` lists the document types that satisfy it (e.g. 12th OR Diploma).
    `condition` optionally limits when the slot applies, based on submission attributes;
    it is evaluated by the validation engine (Phase 8).
    """

    __tablename__ = "requirement_items"
    __table_args__ = (UniqueConstraint("requirement_set_id", "key"),)

    requirement_set_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("requirement_sets.id", ondelete="CASCADE"), index=True
    )
    key: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(255))
    position: Mapped[int] = mapped_column(default=0)
    accepted_type_codes: Mapped[list[Any]] = mapped_column(default=list)
    is_required: Mapped[bool] = mapped_column(default=True)
    min_count: Mapped[int] = mapped_column(default=1)
    condition: Mapped[dict[str, Any] | None] = mapped_column()

    requirement_set: Mapped[RequirementSet] = relationship(back_populates="items")
