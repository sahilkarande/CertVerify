"""Everything the rules need about one submission, loaded once (rules do not query)."""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Document,
    DocumentType,
    ExtractedField,
    RequirementItem,
    StoredFile,
    Submission,
)
from app.models.enums import DocumentStatus, QualityVerdict


@dataclass
class FieldValue:
    value: str
    confidence: float


@dataclass
class PageInfo:
    id: object
    number: int
    type_code: str | None
    semesters: list[int]
    quality: QualityVerdict | None
    fields: dict[str, FieldValue] = field(default_factory=dict)

    def get(self, key: str, min_conf: float = 0.0) -> str | None:
        f = self.fields.get(key)
        return f.value if f and f.confidence >= min_conf else None


@dataclass
class DocInfo:
    id: object
    slot: str | None
    sha256: str
    status: DocumentStatus
    pages: list[PageInfo]

    @property
    def types(self) -> set[str]:
        return {p.type_code for p in self.pages if p.type_code}

    @property
    def unrecognised_pages(self) -> list[PageInfo]:
        return [p for p in self.pages if p.type_code is None]


@dataclass
class SubmissionContext:
    submission_id: object
    applicant_name: str
    attributes: dict
    documents: list[DocInfo]
    requirements: list[RequirementItem]
    # sha256 -> number of *other* submissions holding a file with that content
    shared_hashes: dict[str, int] = field(default_factory=dict)
    # identifier -> number of other submissions where it was read (high confidence)
    shared_ids: dict[str, int] = field(default_factory=dict)

    def pages(self, *type_codes: str) -> list[PageInfo]:
        return [p for d in self.documents for p in d.pages if p.type_code in type_codes]


ID_FIELDS = ("seat_number", "student_id")
ID_MIN_CONFIDENCE = 0.85
ID_MIN_LENGTH = 6


def load_context(db: Session, submission: Submission) -> SubmissionContext:
    codes = dict(db.execute(select(DocumentType.id, DocumentType.code)).all())
    docs = db.scalars(
        select(Document).where(Document.submission_id == submission.id).order_by(Document.slot_key)
    ).all()
    doc_ids = [d.id for d in docs]
    fields: dict = {}
    for f in db.scalars(select(ExtractedField).where(ExtractedField.document_id.in_(doc_ids))):
        if f.value is not None:
            fields.setdefault(f.page_id, {})[f.field_key] = FieldValue(f.value, f.confidence or 0)
    infos = []
    for d in docs:
        pages = [
            PageInfo(
                p.id,
                p.page_number,
                codes.get(p.detected_type_id),
                list(p.semesters or []),
                p.quality_verdict,
                fields.get(p.id, {}),
            )  # fmt: skip
            for p in sorted(d.pages, key=lambda p: p.page_number)
        ]
        infos.append(DocInfo(d.id, d.slot_key, d.file.sha256, d.status, pages))

    hashes = {i.sha256 for i in infos}
    shared_hashes: dict[str, int] = {}
    for sha, _ in db.execute(
        select(StoredFile.sha256, Document.submission_id)
        .join(Document, Document.file_id == StoredFile.id)
        .where(StoredFile.sha256.in_(hashes), Document.submission_id != submission.id)
        .distinct()
    ):
        shared_hashes[sha] = shared_hashes.get(sha, 0) + 1

    own_ids = {
        f.value
        for p in (p for i in infos for p in i.pages)
        for k, f in p.fields.items()
        if k in ID_FIELDS and f.confidence >= ID_MIN_CONFIDENCE and len(f.value) >= ID_MIN_LENGTH
    }
    shared_ids: dict[str, int] = {}
    if own_ids:
        for value, _ in db.execute(
            select(ExtractedField.value, Document.submission_id)
            .join(Document, Document.id == ExtractedField.document_id)
            .where(
                ExtractedField.field_key.in_(ID_FIELDS),
                ExtractedField.value.in_(own_ids),
                ExtractedField.confidence >= ID_MIN_CONFIDENCE,
                Document.submission_id != submission.id,
            )
            .distinct()
        ):
            shared_ids[value] = shared_ids.get(value, 0) + 1

    return SubmissionContext(
        submission_id=submission.id,
        applicant_name=submission.applicant.full_name,
        attributes=dict(submission.attributes or {}),
        documents=infos,
        requirements=list(
            db.scalars(
                select(RequirementItem)
                .where(RequirementItem.requirement_set_id == submission.requirement_set_id)
                .order_by(RequirementItem.position)
            )
        ),
        shared_hashes=shared_hashes,
        shared_ids=shared_ids,
    )
