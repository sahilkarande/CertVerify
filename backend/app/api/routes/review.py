"""Read-only review API for the verifier dashboard.

Signed-in staff only (password + authenticator code). Verifiers and institution admins see
their own institution's applicants; platform admins see all. Another institution's records
answer 404, exactly like missing ones. Every opened applicant and page image is audited.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_db_session, get_storage, require_staff
from app.models import DocumentPage, ExtractedField, Submission, User, ValidationIssue
from app.models.enums import IssueStatus, UserRole
from app.review import actions, queries
from app.services.audit import record_event
from app.storage import ObjectStorage


def scope(user: User) -> uuid.UUID | None:
    """Institution whose records the user may see; None = all (platform admin only)."""
    if user.role == UserRole.PLATFORM_ADMIN:
        return None
    if user.institution_id is None:  # misconfigured account: never "everything"
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your account has no institution.")
    return user.institution_id


router = APIRouter(prefix="/review")
DbSession = Annotated[Session, Depends(get_db_session)]
Staff = Annotated[User, Depends(require_staff())]
PRIVATE = {"Cache-Control": "private, no-store"}


@router.get("/submissions")
def list_submissions(user: Staff, db: DbSession, response: Response) -> list[dict]:
    response.headers.update(PRIVATE)
    return queries.submission_list(db, scope(user))


@router.get("/submissions/{submission_id}")
def get_submission(
    submission_id: uuid.UUID, user: Staff, db: DbSession, response: Response
) -> dict:
    detail = queries.submission_detail(db, submission_id, scope(user))
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Submission not found")
    record_event(
        db,
        action="submission.viewed",
        entity_type="submission",
        entity_id=submission_id,
        actor_user_id=user.id,
        institution_id=user.institution_id,
    )
    db.commit()
    response.headers.update(PRIVATE)
    return detail


@router.get("/pages/{page_id}/image")
def get_page_image(
    page_id: uuid.UUID,
    user: Staff,
    db: DbSession,
    storage: Annotated[ObjectStorage, Depends(get_storage)],
) -> Response:
    data = queries.page_image(db, storage, page_id, scope(user))
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Page image not found")
    record_event(
        db,
        action="page_image.viewed",
        entity_type="document_page",
        entity_id=page_id,
        actor_user_id=user.id,
        institution_id=user.institution_id,
    )
    db.commit()
    return Response(data, media_type="image/jpeg", headers=PRIVATE)


# ---------- verifier actions ----------


class FieldIn(BaseModel):
    value: str = Field(max_length=500)
    expected_value: str | None = Field(default=None, max_length=500)


class NewFieldIn(BaseModel):
    key: str = Field(max_length=64)
    value: str = Field(max_length=500)


class IssueStatusIn(BaseModel):
    status: IssueStatus
    note: str | None = Field(default=None, max_length=4000)


class DecisionIn(BaseModel):
    decision: str = Field(pattern="^(approve|reject|request_changes|reopen)$")
    note: str | None = Field(default=None, max_length=4000)
    expected_status: str = Field(max_length=32)


class MessageIn(BaseModel):
    body: str = Field(max_length=4000)


def _check_scope(user: User, institution_id) -> None:
    inst = scope(user)
    if inst is not None and institution_id != inst:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")


def _sub(db: Session, user: User, sub_id: uuid.UUID) -> Submission:
    sub = db.get(Submission, sub_id)
    if sub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    _check_scope(user, sub.applicant.institution_id)
    return sub


@router.patch("/fields/{field_id}")
def correct_field(field_id: uuid.UUID, body: FieldIn, user: Staff, db: DbSession) -> dict:
    f = db.get(ExtractedField, field_id)
    if f is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    _check_scope(user, f.document.submission.applicant.institution_id)
    actions.correct_field(db, user, f, body.value, body.expected_value)
    db.commit()
    return {"id": str(f.id), "value": f.value, "source": f.source.value}


@router.post("/pages/{page_id}/fields", status_code=status.HTTP_201_CREATED)
def add_field(page_id: uuid.UUID, body: NewFieldIn, user: Staff, db: DbSession) -> dict:
    page = db.get(DocumentPage, page_id)
    if page is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    _check_scope(user, page.document.submission.applicant.institution_id)
    f = actions.add_field(db, user, page, body.key, body.value)
    db.commit()
    return {"id": str(f.id), "key": f.field_key, "value": f.value}


@router.post("/issues/{issue_id}/status")
def issue_status(issue_id: uuid.UUID, body: IssueStatusIn, user: Staff, db: DbSession) -> dict:
    issue = db.get(ValidationIssue, issue_id)
    if issue is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    _check_scope(user, db.get(Submission, issue.submission_id).applicant.institution_id)
    actions.set_issue_status(db, user, issue, body.status, body.note)
    db.commit()
    return {"id": str(issue.id), "status": issue.status.value}


@router.post("/submissions/{submission_id}/decision")
def decision(
    submission_id: uuid.UUID, body: DecisionIn, request: Request, user: Staff, db: DbSession
) -> dict:
    sub = _sub(db, user, submission_id)
    actions.decide(
        db, user, sub, body.decision, body.note, body.expected_status, request.app.state.email
    )
    db.commit()
    return {"status": sub.status.value}


@router.post("/submissions/{submission_id}/messages", status_code=status.HTTP_201_CREATED)
def message(
    submission_id: uuid.UUID, body: MessageIn, request: Request, user: Staff, db: DbSession
) -> dict:
    sub = _sub(db, user, submission_id)
    actions.send_message(db, user, sub, body.body, request.app.state.email)
    db.commit()
    return {"messages": actions.messages_json(db, sub.id)}
