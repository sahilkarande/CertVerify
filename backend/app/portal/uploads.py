"""Accepting an uploaded file into a submission (shared by students and staff).

Upload safety: size capped while reading, real type sniffed from the content (names and
extensions are not trusted), only PDF/JPEG/PNG/HEIC/WEBP accepted, stored encrypted, then
processed by the background worker, which also enforces page and pixel limits.
"""

import uuid

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.jobs.queue import UPLOAD_PRIORITY, enqueue_document
from app.models import Document, RequirementItem, StoredFile, Submission
from app.models.enums import DocumentStatus
from app.services.audit import record_event
from app.services.files import store_file
from app.services.filetypes import HEIC, JPEG, PDF, PNG, WEBP, sniff_mime
from app.storage import ObjectStorage

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ACCEPTED = {PDF, JPEG, PNG, HEIC, WEBP}
CHUNK = 1024 * 1024


async def read_capped(file: UploadFile, limit: int | None = None) -> bytes:
    limit = MAX_UPLOAD_BYTES if limit is None else limit
    data = bytearray()
    while chunk := await file.read(CHUNK):
        data += chunk
        if len(data) > limit:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"The file is larger than {limit // (1024 * 1024)} MB.",
            )
    return bytes(data)


def slot_keys(db: Session, sub: Submission) -> set[str]:
    return set(
        db.scalars(
            select(RequirementItem.key).where(
                RequirementItem.requirement_set_id == sub.requirement_set_id
            )
        )
    )


def add_document(
    db: Session,
    storage: ObjectStorage,
    sub: Submission,
    slot: str,
    data: bytes,
    filename: str | None,
    actor_id: uuid.UUID,
) -> tuple[Document, uuid.UUID]:
    """Validate and store the file, create the document and queue its processing.
    Returns (document, job id). Caller commits."""
    if slot not in slot_keys(db, sub):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown document slot.")
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The file is empty.")
    mime = sniff_mime(data)
    if mime not in ACCEPTED:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "Only PDF files and photos (JPEG, PNG, HEIC, WEBP) can be uploaded.",
        )
    name = (filename or "").replace("\\", "/").split("/")[-1][:200] or None
    inst = sub.applicant.institution_id
    stored = store_file(
        db,
        storage,
        data,
        mime_type=mime,
        original_filename=name,
        actor_user_id=actor_id,
        institution_id=inst,
    )
    doc = Document(
        submission_id=sub.id,
        file_id=stored.id,
        slot_key=slot,
        status=DocumentStatus.UPLOADED,
        source_ref=f"upload:{actor_id}",
    )
    db.add(doc)
    db.flush()
    job = enqueue_document(db, doc.id, priority=UPLOAD_PRIORITY)
    record_event(
        db,
        action="document.uploaded",
        entity_type="document",
        entity_id=doc.id,
        actor_user_id=actor_id,
        institution_id=inst,
        details={"slot": slot, "mime": mime, "bytes": len(data)},
    )
    return doc, job.id


def remove_document(
    db: Session, storage: ObjectStorage, doc: Document, actor_id: uuid.UUID
) -> None:
    """Delete a document, its derived page images and (if nothing else uses it) the original
    file, from the database and from storage. Caller commits."""
    file_ids = [doc.file_id] + [p.image_file_id for p in doc.pages if p.image_file_id]
    record_event(
        db,
        action="document.removed",
        entity_type="document",
        entity_id=doc.id,
        actor_user_id=actor_id,
        institution_id=doc.submission.applicant.institution_id,
    )
    # One DELETE; the database cascades to pages, OCR, fields, issues and jobs.
    db.execute(sa_delete(Document).where(Document.id == doc.id))
    db.expire_all()
    for fid in file_ids:
        still_used = db.scalar(select(Document.id).where(Document.file_id == fid).limit(1))
        f = db.get(StoredFile, fid)
        if f is not None and still_used is None:
            key = f.object_key
            db.delete(f)
            db.flush()
            storage.delete(key)
