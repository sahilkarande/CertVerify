"""What a verifier can change: values, issues, the decision, messages to the student.

Every action is audited (who, what; never the values themselves) and re-runs validation so
issues reflect the corrected data. Edits carry the value/status the verifier last saw; if
someone else changed it meanwhile the action is refused instead of silently overwriting.
Corrections keep the machine's reading next to the verified value: training labels.
"""

import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import iso_utc, utcnow
from app.models import (
    DocumentPage,
    DocumentType,
    ExtractedField,
    Submission,
    SubmissionMessage,
    User,
    ValidationIssue,
)
from app.models.enums import FieldSource, IssueSeverity, IssueStatus, SubmissionStatus
from app.notify.email import EmailSender
from app.services.audit import record_event
from app.validate.engine import validate_submission

DECIDABLE = {SubmissionStatus.SUBMITTED, SubmissionStatus.NEEDS_REVIEW}
BLOCKING = {IssueSeverity.ERROR, IssueSeverity.WARNING}
MAX_TEXT = 4000


def _conflict(what: str) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        f"{what} was changed by someone else meanwhile. Reload and try again.",
    )


def _audit(
    db: Session, user: User, action: str, entity: str, entity_id, sub: Submission, **details
) -> None:
    record_event(
        db,
        action=action,
        entity_type=entity,
        entity_id=entity_id,
        actor_user_id=user.id,
        institution_id=sub.applicant.institution_id,
        details=details,
    )


# ---------- values ----------


def correct_field(
    db: Session, user: User, f: ExtractedField, value: str, expected: str | None
) -> ExtractedField:
    """Set the verified value (same as the machine's = confirmed). ``expected`` is the value
    the verifier was looking at."""
    if f.value != expected:
        raise _conflict("This value")
    value = " ".join(value.split())[:500]
    if not value:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Enter a value.")
    f.value = value
    f.source = FieldSource.HUMAN
    f.corrected_by_id = user.id
    sub = f.document.submission
    _audit(
        db,
        user,
        "field.confirmed" if value == f.machine_value else "field.corrected",
        "extracted_field",
        f.id,
        sub,
        field=f.field_key,
    )
    db.flush()
    validate_submission(db, sub)
    return f


def add_field(db: Session, user: User, page: DocumentPage, key: str, value: str) -> ExtractedField:
    """A value the machine missed. Only keys of the page's document type are allowed."""
    doc_type = db.get(DocumentType, page.detected_type_id) if page.detected_type_id else None
    keys = {f["key"] for f in (doc_type.field_schema if doc_type else [])}
    if key not in keys:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "That value does not belong to this type of document.",
        )
    if db.scalar(
        select(ExtractedField).where(
            ExtractedField.page_id == page.id, ExtractedField.field_key == key
        )
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "This value already exists; edit it instead.")
    value = " ".join(value.split())[:500]
    if not value:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Enter a value.")
    f = ExtractedField(
        document_id=page.document_id,
        page_id=page.id,
        field_key=key,
        machine_value=None,
        value=value,
        confidence=None,
        source=FieldSource.HUMAN,
        corrected_by_id=user.id,
    )
    db.add(f)
    sub = page.document.submission
    db.flush()
    _audit(db, user, "field.added", "extracted_field", f.id, sub, field=key)
    validate_submission(db, sub)
    return f


# ---------- issues ----------


def set_issue_status(
    db: Session, user: User, issue: ValidationIssue, new: IssueStatus, note: str | None
) -> ValidationIssue:
    note = (note or "").strip()[:MAX_TEXT] or None
    if new == IssueStatus.DISMISSED and not note:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Say why this issue does not apply (it is kept on record).",
        )
    issue.status = new
    if new == IssueStatus.OPEN:
        issue.resolved_by_id = issue.resolved_at = issue.resolution_note = None
    else:
        issue.resolved_by_id, issue.resolved_at, issue.resolution_note = user.id, utcnow(), note
    sub = db.get(Submission, issue.submission_id)
    _audit(db, user, f"issue.{new.value}", "validation_issue", issue.id, sub, code=issue.code)
    db.flush()
    return issue


# ---------- decision ----------


def decide(
    db: Session,
    user: User,
    sub: Submission,
    decision: str,
    note: str | None,
    expected_status: str,
    sender: EmailSender | None,
) -> Submission:
    if sub.status.value != expected_status:
        raise _conflict("This application")
    note = (note or "").strip()[:MAX_TEXT] or None
    if decision in ("approve", "reject", "request_changes") and sub.status not in DECIDABLE:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only submitted applications can be decided.")
    if decision == "approve":
        blocking = db.scalars(
            select(ValidationIssue.code).where(
                ValidationIssue.submission_id == sub.id,
                ValidationIssue.status == IssueStatus.OPEN,
                ValidationIssue.severity.in_(BLOCKING),
            )
        ).all()
        if blocking:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Resolve or dismiss the {len(blocking)} open error/warning issue(s) first.",
            )
        new = SubmissionStatus.VERIFIED
    elif decision == "reject":
        if not note:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "Give the reason for rejecting."
            )
        new = SubmissionStatus.REJECTED
    elif decision == "request_changes":
        if not note:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "Tell the student what to change."
            )
        new = SubmissionStatus.CHANGES_REQUESTED
    elif decision == "reopen":
        if sub.status not in (SubmissionStatus.VERIFIED, SubmissionStatus.REJECTED):
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Only decided applications can be reopened."
            )
        if not note:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Say why it is reopened.")
        new = SubmissionStatus.NEEDS_REVIEW
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown decision.")
    sub.status, sub.decided_by_id, sub.decided_at, sub.decision_note = new, user.id, utcnow(), note
    _audit(db, user, f"application.{decision}", "submission", sub.id, sub)
    if decision in ("reject", "request_changes"):
        _message(
            db,
            user,
            sub,
            note,
            sender,
            subject={
                "reject": "Your application was not accepted",
                "request_changes": "Your application needs changes",
            }[decision],
        )
    elif decision == "approve" and sender is not None:
        _notify(
            sender,
            sub,
            "Your application is verified",
            "Your documents have been verified. No further action is needed.",
        )
    db.flush()
    return sub


# ---------- messages ----------


def _notify(sender: EmailSender, sub: Submission, subject: str, body: str) -> None:
    if sub.applicant.email:
        sender.send(
            sub.applicant.email,
            subject,
            f"{body}\n\nSign in to CertVerify to see your application.\n",
        )


def _message(
    db: Session,
    user: User,
    sub: Submission,
    body: str,
    sender: EmailSender | None,
    subject: str = "A message about your application",
) -> SubmissionMessage:
    msg = SubmissionMessage(submission_id=sub.id, author_id=user.id, from_staff=True, body=body)
    db.add(msg)
    db.flush()
    if sender is not None:
        _notify(sender, sub, subject, f"Message from the verification team:\n\n{body}")
    return msg


def send_message(
    db: Session, user: User, sub: Submission, body: str, sender: EmailSender | None
) -> SubmissionMessage:
    body = body.strip()[:MAX_TEXT]
    if not body:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Write a message.")
    msg = _message(db, user, sub, body, sender)
    _audit(db, user, "message.sent", "submission", sub.id, sub)
    return msg


def student_message(db: Session, user: User, sub: Submission, body: str) -> SubmissionMessage:
    body = body.strip()[:MAX_TEXT]
    if not body:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Write a message.")
    msg = SubmissionMessage(submission_id=sub.id, author_id=user.id, from_staff=False, body=body)
    db.add(msg)
    db.flush()
    _audit(db, user, "message.from_student", "submission", sub.id, sub)
    return msg


def messages_json(db: Session, sub_id: uuid.UUID) -> list[dict]:
    rows = db.execute(
        select(SubmissionMessage, User.full_name)
        .outerjoin(User, User.id == SubmissionMessage.author_id)
        .where(SubmissionMessage.submission_id == sub_id)
        .order_by(SubmissionMessage.created_at)
    ).all()
    return [
        {
            "id": str(m.id),
            "from_staff": m.from_staff,
            "body": m.body,
            "author": name if m.from_staff else None,
            "at": iso_utc(m.created_at),
        }
        for m, name in rows
    ]


def set_page_type(db: Session, user: User, page: DocumentPage, code: str) -> DocumentPage:
    """A verifier's page type: kept on re-classification, re-read at once, and a training
    label for the next page-type model."""
    t = db.scalar(
        select(DocumentType).where(DocumentType.code == code, DocumentType.is_active.is_(True))
    )
    if t is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown document type.")
    from app.extract.pipeline import extract_page_fields
    from app.extract.readability import assess_page
    from app.models.enums import ClassificationMethod

    page.detected_type_id = t.id
    page.classification_method = ClassificationMethod.HUMAN
    page.classification_confidence = 1.0
    page.classification_evidence = [f"human:{user.id}"]
    sub = page.document.submission
    db.flush()
    extract_page_fields(db, page)  # the values to read depend on the type
    assess_page(db, page)
    doc = page.document
    types = {p.detected_type_id for p in doc.pages}
    doc.detected_type_id = types.pop() if len(types) == 1 else None
    _audit(db, user, "page.type_set", "document_page", page.id, sub, type=code)
    db.flush()
    validate_submission(db, sub)
    return page
