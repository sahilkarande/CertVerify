"""Stored files, uploaded documents, their pages and extracted fields."""

import uuid
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, TimestampMixin, str_enum
from app.models.enums import DocumentStatus, FieldSource


class StoredFile(IdMixin, TimestampMixin, Base):
    """Metadata for one encrypted blob in the storage backend."""

    __tablename__ = "stored_files"

    object_key: Mapped[str] = mapped_column(String(128), unique=True)
    # SHA-256 of the *plaintext*; used for exact-duplicate detection. Not unique on purpose:
    # the same file uploaded twice is a finding to report, not an error to reject.
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    mime_type: Mapped[str] = mapped_column(String(127))
    original_filename: Mapped[str | None] = mapped_column(String(512))


class Document(IdMixin, TimestampMixin, Base):
    __tablename__ = "documents"

    submission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), index=True
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stored_files.id", ondelete="RESTRICT"), index=True
    )
    # Requirement-item key the student uploaded into. A hint only: content decides the type.
    slot_key: Mapped[str | None] = mapped_column(String(64))
    # Where the file came from, e.g. "gdrive:<file id>" for seed imports.
    source_ref: Mapped[str | None] = mapped_column(String(255), index=True)
    # Type detected from content (Phase 6); null until classified.
    detected_type_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_types.id", ondelete="SET NULL")
    )
    status: Mapped[DocumentStatus] = mapped_column(
        str_enum(DocumentStatus), default=DocumentStatus.UPLOADED
    )
    page_count: Mapped[int | None] = mapped_column()

    submission: Mapped["Submission"] = relationship(back_populates="documents")  # noqa: F821
    file: Mapped[StoredFile] = relationship()
    pages: Mapped[list["DocumentPage"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", order_by="DocumentPage.page_number"
    )
    fields: Mapped[list["ExtractedField"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class DocumentPage(IdMixin, TimestampMixin, Base):
    """One page of a document. A multi-page PDF may hold several logical documents
    (e.g. all semester marksheets in one file), so classification happens per page."""

    __tablename__ = "document_pages"
    __table_args__ = (
        UniqueConstraint("document_id", "page_number"),
        CheckConstraint("page_number >= 1", name="page_number_positive"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    page_number: Mapped[int] = mapped_column()
    detected_type_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_types.id", ondelete="SET NULL")
    )
    classification_confidence: Mapped[float | None] = mapped_column()

    document: Mapped[Document] = relationship(back_populates="pages")


class ExtractedField(IdMixin, TimestampMixin, Base):
    """A value read from a document. `machine_value` is never overwritten, so every human
    correction becomes a labelled example for the learning loop (Phase 13)."""

    __tablename__ = "extracted_fields"
    __table_args__ = (
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="confidence_range",
        ),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    page_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("document_pages.id", ondelete="CASCADE"), index=True
    )
    field_key: Mapped[str] = mapped_column(String(64))
    # Repeating fields (e.g. one row per subject) are distinguished by index.
    field_index: Mapped[int] = mapped_column(default=0)
    machine_value: Mapped[str | None] = mapped_column(Text)
    value: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column()
    source: Mapped[FieldSource] = mapped_column(str_enum(FieldSource), default=FieldSource.MACHINE)
    corrected_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # Location on the page for the review UI, e.g. {"x":..,"y":..,"w":..,"h":..}.
    bbox: Mapped[dict[str, Any] | None] = mapped_column()

    document: Mapped[Document] = relationship(back_populates="fields")
