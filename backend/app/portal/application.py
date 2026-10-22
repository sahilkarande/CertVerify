"""A student's application: answers, the document checklist and "what to fix".

The checklist is built from the institution's requirement set (data, not code) and the
student's own answers (e.g. degree status decides which degree document is expected).
Students see their own documents and plain-language problems only; checks that hint at
fraud (files or IDs shared with another applicant) and judgement calls (claimed percentage)
are for verifiers.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import iso_utc
from app.extract.readability import ID_KEYS, MAX_ATTEMPTS, NAME
from app.intake.quality import MESSAGES as QUALITY_MESSAGES
from app.jobs.queue import latest_job
from app.models import (
    Applicant,
    Document,
    DocumentType,
    Institution,
    RequirementItem,
    RequirementSet,
    Submission,
    User,
    ValidationIssue,
)
from app.models.enums import (
    ApplicantSource,
    DocumentStatus,
    IssueStatus,
    JobStatus,
    QualityVerdict,
    Readability,
    SubmissionStatus,
)
from app.review.actions import messages_json
from app.services.audit import record_event
from app.validate.rules import accepted_types

DEGREE_STATUSES = {
    "has_degree": "I have my degree certificate",
    "has_provisional": "I have my provisional degree certificate",
    "awaiting_provisional": "I am waiting for my provisional certificate",
    "awaiting_final_semester": "I am waiting for my final semester result",
}
# Applications can still change until a verifier has decided.
EDITABLE = {
    SubmissionStatus.DRAFT,
    SubmissionStatus.SUBMITTED,
    SubmissionStatus.NEEDS_REVIEW,
    SubmissionStatus.CHANGES_REQUESTED,
}

# Issue codes a student is told about, with what to do. Anything else stays with staff.
STUDENT_MESSAGES = {
    "missing_document": "This document is missing. Please upload it.",
    "expected_document_substituted": "For your answer above we expect a different document here "
    "({expected}). Upload it if you have it, or change your answer.",
    "unconfirmed_document": "We could not recognise this file. Check that it is the right "
    "document and that the whole page is visible.",
    "document_unreadable": "This file could not be opened. Please upload it again.",
    "page_quality_poor": "Some pages are hard to read (page {pages}). Please upload a clearer "
    "scan or photo.",
    "page_needs_confirmation": "We could not read some details clearly (page {pages}). Type them "
    "below exactly as printed, or upload a clearer copy.",
    "page_text_unreadable": "We could not read the text clearly (page {pages}). Please upload a "
    "clearer scan or photo: the whole page, flat, in good light, filling the picture.",
    "semester_marksheets_missing": "Some semester marksheets seem to be missing: we found "
    "{covered} for results up to semester {highest}.",
    "name_mismatch": "The name on this document does not match your application. Check that you "
    "uploaded your own document.",
    "name_partial_match": "The name on this document is different from your application. If your "
    "name changed, a verifier will contact you.",
}
TYPE_NAMES = {
    "degree_certificate": "degree certificate",
    "provisional_degree_certificate": "provisional certificate",
    "semester_marksheet": "final semester marksheet",
}


def open_intakes(db: Session) -> list[dict]:
    rows = db.execute(
        select(RequirementSet, Institution)
        .join(Institution, Institution.id == RequirementSet.institution_id)
        .where(RequirementSet.is_active.is_(True))
        .order_by(Institution.name, RequirementSet.name)
    ).all()
    return [{"id": str(rs.id), "name": rs.name, "institution": inst.name} for rs, inst in rows]


def find_submission(db: Session, user: User, intake_id: uuid.UUID) -> Submission | None:
    return db.scalar(
        select(Submission)
        .join(Applicant, Applicant.id == Submission.applicant_id)
        .where(Applicant.user_id == user.id, Submission.requirement_set_id == intake_id)
    )


@dataclass
class Answers:
    full_name: str
    phone: str | None
    degree_status: str
    claimed_aggregate_percent: float


def save_answers(db: Session, user: User, intake_id: uuid.UUID, a: Answers) -> Submission:
    """Start the application, or update its answers while it can still change."""
    rs = db.get(RequirementSet, intake_id)
    sub = find_submission(db, user, intake_id)
    if sub is None:
        applicant = db.scalar(
            select(Applicant).where(
                Applicant.user_id == user.id, Applicant.institution_id == rs.institution_id
            )
        )
        if applicant is None:
            applicant = Applicant(
                institution_id=rs.institution_id,
                user_id=user.id,
                email=user.email,
                full_name=a.full_name,
                source=ApplicantSource.PORTAL,
            )
            db.add(applicant)
            db.flush()
        sub = Submission(
            applicant_id=applicant.id,
            requirement_set_id=rs.id,
            status=SubmissionStatus.DRAFT,
            attributes={},
        )
        db.add(sub)
        db.flush()
        record_event(
            db,
            action="application.started",
            entity_type="submission",
            entity_id=sub.id,
            actor_user_id=user.id,
            institution_id=rs.institution_id,
        )
    sub.applicant.full_name = a.full_name
    sub.applicant.phone = a.phone
    if not user.full_name:
        user.full_name = a.full_name
    sub.attributes = {
        **(sub.attributes or {}),
        "degree_status": a.degree_status,
        "claimed_aggregate_percent": a.claimed_aggregate_percent,
    }
    db.flush()
    return sub


def _issue_text(issue: ValidationIssue) -> str | None:
    template = STUDENT_MESSAGES.get(issue.code)
    if template is None:
        return None
    d = issue.details or {}
    expected = " or ".join(
        TYPE_NAMES.get(t, t.replace("_", " ")) for t in d.get("expected_types", [])
    )
    pages = ", ".join(str(p) for p in d.get("pages", []))
    return template.format(
        expected=expected, pages=pages, covered=d.get("covered", "?"), highest=d.get("highest", "?")
    )


def _confirm_json(p, labels: dict[str, str]) -> dict | None:
    """What a CONFIRM page asks the student (never what the machine read: they type it)."""
    if p.readability != Readability.CONFIRM:
        return None
    d = p.readability_details or {}
    kind = {NAME: "name", **dict.fromkeys(ID_KEYS, "id")}
    return {
        "fields": [
            {"key": k, "label": labels.get(k, k.replace("_", " ")), "kind": kind.get(k, "number")}
            for k in d.get("ask", [])
        ],
        "mismatched": [labels.get(k, k) for k in d.get("mismatched", [])],
        "attempts_left": max(0, MAX_ATTEMPTS - int(d.get("attempts", 0))),
    }


def _document_json(db: Session, doc: Document, codes: dict, labels: dict) -> dict:
    job = latest_job(db, doc.id)
    processing = job is not None and job.status not in (JobStatus.DONE, JobStatus.FAILED)
    pages = sorted(doc.pages, key=lambda p: p.page_number)
    return {
        "id": str(doc.id),
        "uploaded_at": iso_utc(doc.created_at),
        "processing": processing,
        "stage": job.stage.value if job else None,
        "failed": doc.status == DocumentStatus.FAILED,
        "error_message": (doc.processing_notes or {}).get("error_message"),
        "recognised_as": sorted({codes[p.detected_type_id] for p in pages if p.detected_type_id}),
        "pages": [
            {
                "id": str(p.id),
                "number": p.page_number,
                "quality": p.quality_verdict.value if p.quality_verdict else None,
                "problems": [
                    QUALITY_MESSAGES[r] for r in p.quality_reasons if r in QUALITY_MESSAGES
                ],
                "has_image": p.image_file_id is not None,
                "text_unreadable": p.readability == Readability.RETAKE,
                "confirm": _confirm_json(p, labels.get(p.detected_type_id, {})),
            }
            for p in pages
        ],
    }


def application_json(db: Session, sub: Submission) -> dict:
    codes = dict(db.execute(select(DocumentType.id, DocumentType.code)).all())
    names = dict(db.execute(select(DocumentType.code, DocumentType.name)).all())
    labels = {
        tid: {f["key"]: f["label"] for f in schema}
        for tid, schema in db.execute(select(DocumentType.id, DocumentType.field_schema))
    }
    items = db.scalars(
        select(RequirementItem)
        .where(RequirementItem.requirement_set_id == sub.requirement_set_id)
        .order_by(RequirementItem.position)
    ).all()
    docs = db.scalars(
        select(Document).where(Document.submission_id == sub.id).order_by(Document.created_at)
    ).all()
    issues = db.scalars(
        select(ValidationIssue).where(
            ValidationIssue.submission_id == sub.id, ValidationIssue.status == IssueStatus.OPEN
        )
    ).all()
    by_item: dict[str, list[str]] = {}
    doc_slot = {d.id: d.slot_key for d in docs}
    general = []
    for i in issues:
        text = _issue_text(i)
        if text is None:
            continue
        slot = (i.details or {}).get("item") or doc_slot.get(i.document_id)
        if i.code == "semester_marksheets_missing":
            slot = "semester_marksheets"
        if slot:
            by_item.setdefault(slot, []).append(text)
        else:
            general.append(text)

    checklist = []
    for it in items:
        mine = [_document_json(db, d, codes, labels) for d in docs if d.slot_key == it.key]
        problems = by_item.get(it.key, [])
        retake = any(p["quality"] == QualityVerdict.RETAKE.value for d in mine for p in d["pages"])
        if not mine:
            state = "missing"
        elif retake:
            state = "attention"  # known from the quick check: no need to wait for the read
        elif any(d["processing"] for d in mine):
            state = "processing"
        elif problems or any(d["failed"] for d in mine):
            state = "attention"
        else:
            state = "ok"
        checklist.append(
            {
                "key": it.key,
                "label": it.label,
                "required": it.is_required,
                "expected": [names.get(t, t) for t in accepted_types(it, sub.attributes or {})],
                "state": state,
                "problems": problems,
                "documents": mine,
            }
        )
    a = sub.applicant
    attrs = sub.attributes or {}
    rs = db.get(RequirementSet, sub.requirement_set_id)
    inst = db.get(Institution, a.institution_id)
    return {
        "id": str(sub.id),
        "intake": {"id": str(rs.id), "name": rs.name, "institution": inst.name if inst else None},
        "status": sub.status.value,
        "editable": sub.status in EDITABLE,
        "answers": {
            "full_name": a.full_name,
            "phone": a.phone,
            "degree_status": attrs.get("degree_status"),
            "claimed_aggregate_percent": attrs.get("claimed_aggregate_percent"),
        },
        "checklist": checklist,
        "general_problems": general,
        # The verifier's note is shown when the student has something to do or know.
        "decision_note": sub.decision_note
        if sub.status in (SubmissionStatus.CHANGES_REQUESTED, SubmissionStatus.REJECTED)
        else None,
        "messages": messages_json(db, sub.id),
        "ready_to_submit": all(c["state"] != "missing" for c in checklist if c["required"]),
    }
