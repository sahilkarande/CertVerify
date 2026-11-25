"""Extract fields from classified pages and store them as ExtractedField rows.

Fields are stored per page: one PDF may hold several semester marksheets, each with its own
SGPA. Which fields a page gets comes from its document type's field schema (the registry),
so a new type needs registry data, not code, as long as its fields are among the finders'.
Human corrections and values the student confirmed are never touched: re-running replaces
machine rows only.
"""

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.extract.fields import Candidate, extract_page
from app.models import Document, DocumentPage, DocumentType, ExtractedField, StoredFile, Submission
from app.models.enums import DocumentCategory, FieldSource
from app.ocr.pipeline import preferred_readings

# Bump when finders or confidence rules change so pages are re-extracted.
EXTRACTOR_VERSION = 3

# Values the classifier already determined.
FROM_CLASSIFICATION = {"semester"}


@dataclass
class PageExtraction:
    stored: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)


def _schema(doc_type: DocumentType) -> tuple[set[str], set[str]]:
    keys = {f["key"] for f in doc_type.field_schema}
    required = {f["key"] for f in doc_type.field_schema if f.get("required")}
    if "total_marks_obtained" in keys:
        keys.add("total_marks_max")
    return keys, required


def extract_page_fields(db: Session, page: DocumentPage) -> PageExtraction:
    """(Re-)extract one classified page. Caller commits."""
    result = PageExtraction()
    doc_type = db.get(DocumentType, page.detected_type_id) if page.detected_type_id else None
    db.execute(
        delete(ExtractedField).where(
            ExtractedField.page_id == page.id, ExtractedField.source == FieldSource.MACHINE
        )
    )
    page.extractor_version = EXTRACTOR_VERSION
    if doc_type is None or doc_type.category != DocumentCategory.ACADEMIC_RECORD:
        return result
    keys, required = _schema(doc_type)
    corrected = set(
        db.scalars(
            select(ExtractedField.field_key).where(
                ExtractedField.page_id == page.id, ExtractedField.source != FieldSource.MACHINE
            )
        )
    )
    reading = preferred_readings(db, [page.id]).get(page.id)
    doc = db.get(Document, page.document_id)
    applicant_name = doc.submission.applicant.full_name if doc.submission else None
    found: dict[str, Candidate] = extract_page(reading.words if reading else [], applicant_name)
    if "semester" in keys and len(page.semesters) == 1:
        found["semester"] = Candidate(
            "semester", str(page.semesters[0]), page.classification_confidence or 0.5
        )
    for key in sorted(keys - corrected):
        c = found.get(key)
        if c is None:
            if key in required:
                result.missing_required.append(key)
            continue
        bbox = None
        if c.box is not None and reading is not None:
            # Coordinates are in the OCR image: the page turned by `rotation` (and scaled).
            bbox = {**c.box.bbox(), "rotation": reading.rotation, "engine": reading.engine}
        db.add(
            ExtractedField(
                document_id=page.document_id,
                page_id=page.id,
                field_key=key,
                machine_value=c.value,
                value=c.value,
                confidence=c.confidence,
                source=FieldSource.MACHINE,
                bbox=bbox,
            )
        )
        result.stored.append(key)
    db.flush()
    return result


# ---------- identifiers confirmed by another page ----------

ID_KEYS = ("seat_number", "student_id")
# An ID read identically from two different page images (a PRN or enrolment number repeats
# on every semester sheet) is very unlikely to be a misread: on the labelled pages, 26 of 26
# such values were right, against 20 of 21 for values read beside their label.
CORROBORATED = 0.95


def corroborate_ids(db: Session, applicant_id: uuid.UUID) -> int:
    """Recompute, from scratch, which of the applicant's machine-read IDs another page
    confirms. The extractor's own confidence is kept in ``bbox["base_confidence"]`` so this can
    be undone (a file deleted, a page re-read). Returns how many fields are confirmed. Caller
    commits."""
    rows = db.execute(
        select(ExtractedField, StoredFile.sha256)
        .join(DocumentPage, DocumentPage.id == ExtractedField.page_id)
        .join(StoredFile, StoredFile.id == DocumentPage.image_file_id)
        .join(Document, Document.id == ExtractedField.document_id)
        .join(Submission, Submission.id == Document.submission_id)
        .where(
            Submission.applicant_id == applicant_id,
            ExtractedField.field_key.in_(ID_KEYS),
            ExtractedField.source == FieldSource.MACHINE,
        )
    ).all()
    images: dict[str, set[str]] = defaultdict(set)  # value -> page images it was read from
    for f, sha in rows:
        if f.machine_value:
            images[f.machine_value.upper()].add(sha)
    confirmed = 0
    for f, _ in rows:
        box = dict(f.bbox or {})
        base = box.get("base_confidence", f.confidence)
        if f.machine_value and len(images[f.machine_value.upper()]) >= 2:
            box["base_confidence"] = base
            f.confidence = max(base or 0, CORROBORATED)
            confirmed += 1
        else:
            box.pop("base_confidence", None)
            f.confidence = base
        f.bbox = box or None
    db.flush()
    return confirmed


def pages_needing_extraction(db: Session, *, force: bool = False) -> list:
    """Classified academic pages without current machine fields."""
    q = (
        select(DocumentPage.id)
        .join(DocumentType, DocumentType.id == DocumentPage.detected_type_id)
        .where(DocumentType.category == DocumentCategory.ACADEMIC_RECORD)
        .order_by(DocumentPage.document_id, DocumentPage.page_number)
    )
    if not force:
        q = q.where(
            DocumentPage.extractor_version.is_(None)
            | (DocumentPage.extractor_version != EXTRACTOR_VERSION)
        )
    return list(db.scalars(q))


def extraction_report(db: Session, high: float = 0.9) -> dict[str, Counter]:
    """Per document type: pages, and for each field how many pages have it / at high
    confidence. Counts only."""
    rows = db.execute(
        select(DocumentType.code, DocumentPage.id)
        .join(DocumentType, DocumentType.id == DocumentPage.detected_type_id)
        .where(DocumentPage.extractor_version.is_not(None))
    ).all()
    type_of = {pid: code for code, pid in rows}
    out: dict[str, Counter] = {}
    for code in type_of.values():
        out.setdefault(code, Counter())["pages"] += 1
    for pid, key, conf in db.execute(
        select(ExtractedField.page_id, ExtractedField.field_key, ExtractedField.confidence)
    ):
        if pid in type_of:
            out[type_of[pid]][key] += 1
            out[type_of[pid]][key + " high"] += (conf or 0) >= high
    return out
