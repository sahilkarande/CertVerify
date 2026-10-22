"""Adding a document to a submission, and following its processing.

Upload safety: size capped while reading, real type sniffed from the content (names and
extensions are not trusted), only PDF/JPEG/PNG/HEIC/WEBP accepted, stored encrypted, then
processed by the background worker, which also enforces page and pixel limits.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db_session, get_storage, require_staff
from app.api.routes.review import PRIVATE, scope
from app.auth.ratelimit import RateLimiter
from app.jobs.queue import UPLOAD_PRIORITY, enqueue_document, latest_job
from app.models import Document, RequirementItem, Submission, User
from app.models.enums import DocumentStatus, JobStatus
from app.services.audit import record_event
from app.services.files import store_file
from app.services.filetypes import HEIC, JPEG, PDF, PNG, WEBP, sniff_mime
from app.storage import ObjectStorage

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db_session)]
Staff = Annotated[User, Depends(require_staff())]

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ACCEPTED = {PDF, JPEG, PNG, HEIC, WEBP}
CHUNK = 1024 * 1024
UPLOAD_LIMIT = RateLimiter(limit=30, window_s=600)  # per user


def _visible_submission(db: Session, user: User, submission_id: uuid.UUID) -> Submission:
    sub = db.get(Submission, submission_id)
    inst = scope(user)
    if sub is None or (inst is not None and sub.applicant.institution_id != inst):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Submission not found")
    return sub


async def _read_capped(file: UploadFile) -> bytes:
    data = bytearray()
    while chunk := await file.read(CHUNK):
        data += chunk
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
            )
    return bytes(data)


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
    slots = set(
        db.scalars(
            select(RequirementItem.key).where(
                RequirementItem.requirement_set_id == sub.requirement_set_id
            )
        )
    )
    if slot not in slots:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown document slot.")
    data = await _read_capped(file)
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The file is empty.")
    mime = sniff_mime(data)
    if mime not in ACCEPTED:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "Only PDF files and photos (JPEG, PNG, HEIC, WEBP) can be uploaded.",
        )
    name = (file.filename or "").replace("\\", "/").split("/")[-1][:200] or None
    stored = store_file(
        db,
        storage,
        data,
        mime_type=mime,
        original_filename=name,
        actor_user_id=user.id,
        institution_id=sub.applicant.institution_id,
    )
    doc = Document(
        submission_id=sub.id,
        file_id=stored.id,
        slot_key=slot,
        status=DocumentStatus.UPLOADED,
        source_ref=f"upload:{user.id}",
    )
    db.add(doc)
    db.flush()
    job = enqueue_document(db, doc.id, priority=UPLOAD_PRIORITY)
    record_event(
        db,
        action="document.uploaded",
        entity_type="document",
        entity_id=doc.id,
        actor_user_id=user.id,
        institution_id=sub.applicant.institution_id,
        details={"slot": slot, "mime": mime, "bytes": len(data)},
    )
    db.commit()
    return {"document_id": str(doc.id), "job_id": str(job.id)}


@router.get("/documents/{document_id}/status")
def document_status(document_id: uuid.UUID, user: Staff, db: DbSession, response: Response) -> dict:
    """Polled by the browser while a document is processed."""
    doc = db.get(Document, document_id)
    inst = scope(user)
    if doc is None or (inst is not None and doc.submission.applicant.institution_id != inst):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    job = latest_job(db, doc.id)
    response.headers.update(PRIVATE)
    return {
        "document_id": str(doc.id),
        "document_status": doc.status.value,
        "processing_error": doc.processing_error,
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
