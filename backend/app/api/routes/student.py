"""Student portal API. A student only ever sees their own applications, documents and pages
(anything else answers 404, as if it did not exist)."""

import uuid
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.api.deps import get_app_settings, get_current_session, get_db_session, get_storage
from app.api.routes.auth import _client, _me, _set_cookie
from app.api.routes.documents import status_json
from app.api.routes.review import PRIVATE
from app.auth import service, student
from app.auth.ratelimit import RateLimiter
from app.core.config import Settings
from app.models import Document, DocumentPage, RequirementSet, Submission, User, UserSession
from app.models.enums import SubmissionStatus, UserRole
from app.portal import application as appl
from app.portal.uploads import add_document, read_capped, remove_document
from app.review import actions, queries
from app.services.audit import record_event
from app.storage import ObjectStorage
from app.validate.engine import validate_submission

router = APIRouter(prefix="/student")
DbSession = Annotated[Session, Depends(get_db_session)]
Storage = Annotated[ObjectStorage, Depends(get_storage)]

CODE_REQUESTS = RateLimiter(limit=10, window_s=900)  # per address
CODE_GUESSES = RateLimiter(limit=20, window_s=900)
STUDENT_UPLOADS = RateLimiter(limit=40, window_s=600)  # per student
GENERIC = "If that email can receive codes, one is on its way. It works for 10 minutes."


def require_student(session: Annotated[UserSession, Depends(get_current_session)]) -> User:
    if session.user.role != UserRole.STUDENT or not session.mfa_passed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This page is for students.")
    return session.user


Student = Annotated[User, Depends(require_student)]


# ---------- sign-in ----------


class EmailIn(BaseModel):
    email: str = Field(max_length=320)


class CodeIn(BaseModel):
    email: str = Field(max_length=320)
    code: str = Field(max_length=12)


def _email(raw: str) -> str:
    email = student.normalize_email(raw)
    if email is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Enter a valid email address.")
    return email


@router.post("/auth/request-code", status_code=status.HTTP_202_ACCEPTED)
def request_code(body: EmailIn, request: Request, db: DbSession) -> dict:
    if not CODE_REQUESTS.hit(_client(request)):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests. Wait a few minutes."
        )
    student.request_code(
        db,
        request.app.state.email,
        request.app.state.code_key,
        _email(body.email),
        _client(request),
    )
    db.commit()
    return {"message": GENERIC}


@router.post("/auth/verify-code")
def verify_code(
    body: CodeIn,
    request: Request,
    response: Response,
    db: DbSession,
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> dict:
    if not CODE_GUESSES.hit(_client(request)):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Wait a few minutes."
        )
    user = student.verify_code(db, request.app.state.code_key, _email(body.email), body.code)
    if user is None:
        db.commit()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "That code is not right or has expired. Ask for a new one."
        )
    token = service.start_session(
        db, user, mfa_passed=True, ip=_client(request), user_agent=request.headers.get("user-agent")
    )
    db.commit()
    _set_cookie(response, settings, token)
    return _me(service.find_session(db, token))


# ---------- applications ----------


class AnswersIn(BaseModel):
    full_name: str = Field(min_length=2, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    degree_status: str
    claimed_aggregate_percent: float = Field(ge=0, le=100)

    @field_validator("degree_status")
    @classmethod
    def known_status(cls, v: str) -> str:
        if v not in appl.DEGREE_STATUSES:
            raise ValueError("unknown degree status")
        return v

    @field_validator("full_name")
    @classmethod
    def tidy_name(cls, v: str) -> str:
        return " ".join(v.split())


@router.get("/intakes")
def intakes(user: Student, db: DbSession) -> dict:
    return {"intakes": appl.open_intakes(db), "degree_statuses": appl.DEGREE_STATUSES}


def _intake(db: Session, intake_id: uuid.UUID) -> RequirementSet:
    rs = db.get(RequirementSet, intake_id)
    if rs is None or not rs.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This application is not open.")
    return rs


def _own_submission(db: Session, user: User, intake_id: uuid.UUID) -> Submission:
    sub = appl.find_submission(db, user, intake_id)
    if sub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application not started.")
    return sub


def _editable(sub: Submission) -> None:
    if sub.status not in appl.EDITABLE:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This application has been decided and can no longer change."
        )


@router.get("/applications/{intake_id}")
def get_application(intake_id: uuid.UUID, user: Student, db: DbSession, response: Response) -> dict:
    _intake(db, intake_id)
    response.headers.update(PRIVATE)
    return appl.application_json(db, _own_submission(db, user, intake_id))


@router.put("/applications/{intake_id}")
def save_application(intake_id: uuid.UUID, body: AnswersIn, user: Student, db: DbSession) -> dict:
    _intake(db, intake_id)
    existing = appl.find_submission(db, user, intake_id)
    if existing is not None:
        _editable(existing)
    sub = appl.save_answers(db, user, intake_id, appl.Answers(**body.model_dump()))
    validate_submission(db, sub)
    db.commit()
    return appl.application_json(db, sub)


@router.post("/applications/{intake_id}/documents", status_code=status.HTTP_202_ACCEPTED)
async def upload(
    intake_id: uuid.UUID,
    user: Student,
    db: DbSession,
    storage: Storage,
    slot: Annotated[str, Form(max_length=64)],
    file: Annotated[UploadFile, File()],
) -> dict:
    if not STUDENT_UPLOADS.hit(str(user.id)):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Too many uploads. Wait a few minutes."
        )
    sub = _own_submission(db, user, intake_id)
    _editable(sub)
    data = await read_capped(file)
    doc, job_id = add_document(db, storage, sub, slot, data, file.filename, user.id)
    db.commit()
    return {"document_id": str(doc.id), "job_id": str(job_id)}


def _own_document(db: Session, user: User, document_id: uuid.UUID) -> Document:
    doc = db.get(Document, document_id)
    if doc is None or doc.submission.applicant.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    return doc


@router.get("/documents/{document_id}/status")
def document_status(
    document_id: uuid.UUID, user: Student, db: DbSession, response: Response
) -> dict:
    response.headers.update(PRIVATE)
    return status_json(db, _own_document(db, user, document_id))


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    document_id: uuid.UUID, user: Student, db: DbSession, storage: Storage
) -> Response:
    doc = _own_document(db, user, document_id)
    sub = doc.submission
    _editable(sub)
    remove_document(db, storage, doc, user.id)
    validate_submission(db, sub)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/pages/{page_id}/image")
def page_image(page_id: uuid.UUID, user: Student, db: DbSession, storage: Storage) -> Response:
    page = db.get(DocumentPage, page_id)
    if page is None or page.document.submission.applicant.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Page not found")
    data = queries.page_image(db, storage, page_id, None)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Page not found")
    return Response(data, media_type="image/jpeg", headers=PRIVATE)


@router.post("/applications/{intake_id}/submit")
def submit(intake_id: uuid.UUID, user: Student, db: DbSession) -> dict:
    sub = _own_submission(db, user, intake_id)
    _editable(sub)
    current = appl.application_json(db, sub)
    if not current["ready_to_submit"]:
        missing = [
            c["label"] for c in current["checklist"] if c["required"] and c["state"] == "missing"
        ]
        raise HTTPException(status.HTTP_409_CONFLICT, "Upload these first: " + "; ".join(missing))
    if sub.status in (SubmissionStatus.DRAFT, SubmissionStatus.CHANGES_REQUESTED):
        from app.db.base import utcnow

        sub.status = SubmissionStatus.SUBMITTED
        sub.submitted_at = utcnow()
        record_event(
            db,
            action="application.submitted",
            entity_type="submission",
            entity_id=sub.id,
            actor_user_id=user.id,
            institution_id=sub.applicant.institution_id,
        )
    validate_submission(db, sub)
    db.commit()
    return appl.application_json(db, sub)


@router.get("/applications")
def my_applications(user: Student, db: DbSession) -> dict:
    out = []
    for it in appl.open_intakes(db):
        sub = appl.find_submission(db, user, uuid.UUID(it["id"]))
        out.append({**it, "started": sub is not None, "status": sub.status.value if sub else None})
    return {"applications": out}


class StudentMessageIn(BaseModel):
    body: str = Field(max_length=4000)


@router.post("/applications/{intake_id}/messages", status_code=status.HTTP_201_CREATED)
def message(intake_id: uuid.UUID, body: StudentMessageIn, user: Student, db: DbSession) -> dict:
    sub = _own_submission(db, user, intake_id)
    actions.student_message(db, user, sub, body.body)
    db.commit()
    return {"messages": actions.messages_json(db, sub.id)}
