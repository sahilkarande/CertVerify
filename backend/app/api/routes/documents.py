"""Staff adding a document to a submission, and following its processing."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import get_db_session, get_storage, require_staff
from app.api.routes.review import PRIVATE, scope
from app.auth.ratelimit import RateLimiter
from app.jobs.queue import latest_job
from app.models import Document, Submission, User
from app.models.enums import JobStatus
from app.portal.uploads import add_document, read_capped
from app.storage import ObjectStorage

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db_session)]
Staff = Annotated[User, Depends(require_staff())]

UPLOAD_LIMIT = RateLimiter(limit=30, window_s=600)  # per user


def _visible_submission(db: Session, user: User, submission_id: uuid.UUID) -> Submission:
    sub = db.get(Submission, submission_id)
    inst = scope(user)
    if sub is None or (inst is not None and sub.applicant.institution_id != inst):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Submission not found")
    return sub


@router.post("/submissions/{submission_id}/documents", status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    submission_id: uuid.UUID,
    user: Staff,
    db: DbSession,
    storage: Annotated[ObjectStorage, Depends(get_storage)],
    slot: Annotated[str, Form(max_length=64)],
    file: Annotated[UploadFile, File()],
) -> dict:
    if not UPLOAD_LIMIT.hit(str(user.id)):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Too many uploads. Wait a few minutes."
        )
    sub = _visible_submission(db, user, submission_id)
    data = await read_capped(file)
    doc, job_id = add_document(db, storage, sub, slot, data, file.filename, user.id)
    db.commit()
    return {"document_id": str(doc.id), "job_id": str(job_id)}


@router.get("/documents/{document_id}/status")
def document_status(document_id: uuid.UUID, user: Staff, db: DbSession, response: Response) -> dict:
    """Polled by the browser while a document is processed."""
    doc = db.get(Document, document_id)
    inst = scope(user)
    if doc is None or (inst is not None and doc.submission.applicant.institution_id != inst):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    response.headers.update(PRIVATE)
    return status_json(db, doc)


def status_json(db: Session, doc: Document) -> dict:
    job = latest_job(db, doc.id)
    return {
        "document_id": str(doc.id),
        "document_status": doc.status.value,
        "processing_error": doc.processing_error,
        "error_message": (doc.processing_notes or {}).get("error_message"),
        "job": None
        if job is None
        else {
            "status": job.status.value,
            "stage": job.stage.value,
            "attempts": job.attempts,
            "error": job.last_error,
            "finished": job.status in (JobStatus.DONE, JobStatus.FAILED),
        },
        "pages": [
            {
                "number": p.page_number,
                "quality": p.quality_verdict.value if p.quality_verdict else None,
                "reasons": p.quality_reasons,
            }
            for p in sorted(doc.pages, key=lambda p: p.page_number)
        ],
    }
