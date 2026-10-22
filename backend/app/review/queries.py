"""Queries behind the review dashboard. Read-only: nothing here changes data except the
audit events recording who looked at what."""

import io
import logging
import uuid
from collections import Counter

import numpy as np
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.base import iso_utc
from app.models import (
    Applicant,
    Document,
    DocumentPage,
    DocumentType,
    ExtractedField,
    Submission,
    User,
    ValidationIssue,
)
from app.models.enums import IssueSeverity, IssueStatus
from app.ocr.paddle import MAX_LONG_SIDE_PX
from app.ocr.pipeline import preferred_readings
from app.ocr.preprocess import OCR_SHORT_PX, rotate
from app.review.actions import messages_json
from app.services.files import read_file
from app.storage import ObjectStorage
from app.storage.base import StorageError
from app.validate.engine import verdict

logger = logging.getLogger(__name__)

SEVERITY_ORDER = {IssueSeverity.ERROR: 0, IssueSeverity.WARNING: 1, IssueSeverity.INFO: 2}
WEB_IMAGE_LONG_SIDE = 1600


def submission_list(db: Session, institution_id: uuid.UUID | None) -> list[dict]:
    """``institution_id`` None = all institutions (platform admins only)."""
    open_issues: dict = {}
    for sub_id, sev, n in db.execute(
        select(ValidationIssue.submission_id, ValidationIssue.severity, func.count())
        .where(ValidationIssue.status == IssueStatus.OPEN)
        .group_by(ValidationIssue.submission_id, ValidationIssue.severity)
    ):
        open_issues.setdefault(sub_id, Counter())[sev.value] = n
    docs = dict(
        db.execute(
            select(Document.submission_id, func.count()).group_by(Document.submission_id)
        ).all()
    )
    out = []
    q = (
        select(Submission, Applicant)
        .join(Applicant, Applicant.id == Submission.applicant_id)
        .order_by(Applicant.full_name)
    )
    if institution_id is not None:
        q = q.where(Applicant.institution_id == institution_id)
    for sub, app in db.execute(q):
        counts = open_issues.get(sub.id, Counter())
        out.append(
            {
                "id": str(sub.id),
                "name": app.full_name,
                "form_number": app.external_ref,
                "status": sub.status.value,
                "degree_status": sub.attributes.get("degree_status"),
                "claimed_percent": sub.attributes.get("claimed_aggregate_percent"),
                "submitted_at": iso_utc(sub.submitted_at),
                "documents": docs.get(sub.id, 0),
                "issues": {s: counts.get(s, 0) for s in ("error", "warning", "info")},
                "verdict": verdict([IssueSeverity(s) for s, n in counts.items() for _ in range(n)]),
            }
        )
    return out


def _ocr_scale(page: DocumentPage, engine: str, rotation: int) -> tuple[float, float]:
    """Size of the image the OCR engine read (its word boxes are in that space)."""
    w, h = page.width_px or 1, page.height_px or 1
    if rotation in (90, 270):
        w, h = h, w
    paddle = engine == "paddle"
    s = min(1.0, MAX_LONG_SIDE_PX / max(w, h)) if paddle else OCR_SHORT_PX / min(w, h)
    return w * s, h * s


def _visible(institution_id: uuid.UUID | None, applicant: Applicant) -> bool:
    return institution_id is None or applicant.institution_id == institution_id


def submission_detail(
    db: Session, sub_id: uuid.UUID, institution_id: uuid.UUID | None
) -> dict | None:
    """None if missing or belonging to another institution (indistinguishable on purpose)."""
    sub = db.get(Submission, sub_id)
    if sub is None or not _visible(institution_id, sub.applicant):
        return None
    codes = dict(db.execute(select(DocumentType.id, DocumentType.code)).all())
    names = dict(db.execute(select(DocumentType.code, DocumentType.name)).all())
    schemas = {
        c: [{"key": f["key"], "label": f["label"]} for f in schema]
        for c, schema in db.execute(select(DocumentType.code, DocumentType.field_schema))
    }
    docs = db.scalars(
        select(Document).where(Document.submission_id == sub.id).order_by(Document.slot_key)
    ).all()
    page_ids = [p.id for d in docs for p in d.pages]
    readings = preferred_readings(db, page_ids)
    fields: dict = {}
    for f in db.scalars(select(ExtractedField).where(ExtractedField.page_id.in_(page_ids))):
        fields.setdefault(f.page_id, []).append(f)

    def page_json(p: DocumentPage) -> dict:
        r = readings.get(p.id)
        rotation = r.rotation if r else 0
        size = _ocr_scale(p, r.engine, rotation) if r else (1, 1)
        page_fields = []
        for f in sorted(fields.get(p.id, []), key=lambda f: f.field_key):
            box = None
            if f.bbox and r:
                box = {
                    k: round(f.bbox[k] / size[0 if k in ("x", "w") else 1], 4)
                    for k in ("x", "y", "w", "h")
                }
            page_fields.append(
                {
                    "id": str(f.id),
                    "key": f.field_key,
                    "value": f.value,
                    "machine_value": f.machine_value,
                    "confidence": f.confidence,
                    "source": f.source.value,
                    "box": box,
                }
            )
        code = codes.get(p.detected_type_id)
        return {
            "id": str(p.id),
            "number": p.page_number,
            "type": code,
            "type_name": names.get(code),
            "classification_confidence": p.classification_confidence,
            "classification_method": p.classification_method.value
            if p.classification_method
            else None,
            "semesters": p.semesters,
            "level": p.academic_level.value if p.academic_level else None,
            "quality": p.quality_verdict.value if p.quality_verdict else None,
            "quality_reasons": p.quality_reasons,
            "ocr": {
                "engine": r.engine,
                "status": r.status.value,
                "confidence": r.mean_confidence,
                "rotation": rotation,
            }
            if r
            else None,
            "has_image": p.image_file_id is not None,
            "fields": page_fields,
            # Values a verifier may add when the machine missed them.
            "addable_fields": [
                f for f in schemas.get(code, []) if f["key"] not in {x["key"] for x in page_fields}
            ],
        }

    issues = db.scalars(
        select(ValidationIssue).where(ValidationIssue.submission_id == sub.id)
    ).all()
    issues = sorted(
        issues, key=lambda i: (i.status != IssueStatus.OPEN, SEVERITY_ORDER[i.severity], i.code)
    )
    open_sev = [i.severity for i in issues if i.status == IssueStatus.OPEN]
    app = sub.applicant
    return {
        "id": str(sub.id),
        "applicant": {
            "name": app.full_name,
            "email": app.email,
            "phone": app.phone,
            "form_number": app.external_ref,
        },
        "status": sub.status.value,
        "submitted_at": iso_utc(sub.submitted_at),
        "attributes": {k: v for k, v in sub.attributes.items() if k != "guardian_phone"},
        "verdict": verdict(open_sev),
        "decision": {
            "by": db.get(User, sub.decided_by_id).full_name if sub.decided_by_id else None,
            "at": iso_utc(sub.decided_at),
            "note": sub.decision_note,
        },
        "messages": messages_json(db, sub.id),
        "document_types": [
            {"code": c, "name": n}
            for c, n in db.execute(
                select(DocumentType.code, DocumentType.name)
                .where(DocumentType.is_active.is_(True))
                .order_by(DocumentType.name)
            )
        ],
        "issues": [
            {
                "id": str(i.id),
                "code": i.code,
                "severity": i.severity.value,
                "status": i.status.value,
                "message": i.message,
                "document_id": str(i.document_id) if i.document_id else None,
                "resolution_note": i.resolution_note,
                "resolved_at": iso_utc(i.resolved_at),
                "details": {
                    k: v for k, v in i.details.items() if k not in ("key", "rules_version")
                },
            }
            for i in issues
        ],
        "documents": [
            {
                "id": str(d.id),
                "slot": d.slot_key,
                "status": d.status.value,
                "type": codes.get(d.detected_type_id),
                "page_count": len(d.pages),
                "processing_error": d.processing_error,
                "pages": [page_json(p) for p in sorted(d.pages, key=lambda p: p.page_number)],
            }
            for d in docs
        ],
    }


def page_image(
    db: Session, storage: ObjectStorage, page_id: uuid.UUID, institution_id: uuid.UUID | None
) -> bytes | None:
    """The page as a web-sized JPEG, turned upright the way OCR read it."""
    page = db.get(DocumentPage, page_id)
    if page is None or page.image_file is None:
        return None
    if not _visible(institution_id, page.document.submission.applicant):
        return None
    reading = preferred_readings(db, [page.id]).get(page.id)
    rotation = reading.rotation if reading else 0
    try:
        raw = read_file(storage, page.image_file)
    except StorageError as exc:
        # Missing or failing its integrity check: never serve it, and make it visible.
        logger.error("page image %s unavailable: %s", page.id, exc)
        return None
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    if rotation:
        img = Image.fromarray(rotate(np.asarray(img), rotation))
    img.thumbnail((WEB_IMAGE_LONG_SIDE, WEB_IMAGE_LONG_SIDE))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()
