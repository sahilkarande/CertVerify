"""Fetch pending Drive file refs into encrypted storage and create Documents for them.

Each file is committed on its own, so an interrupted run resumes where it stopped.
"""

import hashlib
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.importers.gdrive.client import DriveClient, DriveFileError
from app.models import Document, ImportFileRef, StoredFile, Submission
from app.models.enums import ImportFileStatus
from app.services.files import store_file
from app.storage import ObjectStorage

logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 25 * 1024 * 1024


@dataclass
class DownloadReport:
    fetched: int = 0
    failed: int = 0
    bytes_downloaded: int = 0
    errors: list[str] = field(default_factory=list)  # "<ref id>: <reason>", no PII


def _fetch_one(
    db: Session,
    storage: ObjectStorage,
    client: DriveClient,
    ref: ImportFileRef,
    max_bytes: int,
) -> int:
    meta = client.get_metadata(ref.external_id)
    if meta.size is not None and meta.size > max_bytes:
        raise DriveFileError(f"file is {meta.size} bytes (limit {max_bytes})", permanent=True)

    data = client.download(ref.external_id)
    if len(data) > max_bytes:
        raise DriveFileError(f"file is {len(data)} bytes (limit {max_bytes})", permanent=True)
    if meta.size is not None and len(data) != meta.size:
        raise DriveFileError(
            f"size mismatch: got {len(data)}, Drive says {meta.size}", permanent=False
        )
    if meta.md5 and hashlib.md5(data, usedforsecurity=False).hexdigest() != meta.md5:
        raise DriveFileError("checksum mismatch (corrupted download)", permanent=False)

    submission = db.get(Submission, ref.submission_id)
    institution_id = submission.applicant.institution_id if submission else None
    stored: StoredFile = store_file(
        db,
        storage,
        data,
        mime_type=meta.mime_type,
        original_filename=meta.name,
        institution_id=institution_id,
    )
    try:
        doc = Document(
            submission_id=ref.submission_id,
            file_id=stored.id,
            slot_key=ref.slot_key,
            source_ref=f"{ref.source}:{ref.external_id}",
        )
        db.add(doc)
        db.flush()
        ref.status = ImportFileStatus.FETCHED
        ref.document_id = doc.id
        ref.fetched_at = utcnow()
        ref.last_error = None
        db.commit()
    except Exception:
        # The blob was written before the commit failed; remove it so nothing is orphaned.
        storage.delete(stored.object_key)
        raise
    return len(data)


def download_pending(
    db: Session,
    storage: ObjectStorage,
    client: DriveClient,
    *,
    limit: int | None = None,
    retry_failed: bool = False,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> DownloadReport:
    statuses = [ImportFileStatus.PENDING]
    if retry_failed:
        statuses.append(ImportFileStatus.FAILED)
    query = (
        select(ImportFileRef.id)
        .where(ImportFileRef.status.in_(statuses))
        .order_by(ImportFileRef.created_at, ImportFileRef.id)
    )
    if limit is not None:
        query = query.limit(limit)
    ref_ids = list(db.scalars(query))

    report = DownloadReport()
    for n, ref_id in enumerate(ref_ids, start=1):
        ref = db.get(ImportFileRef, ref_id)
        ref.attempts += 1
        db.commit()
        try:
            size = _fetch_one(db, storage, client, ref, max_bytes)
        except Exception as exc:
            db.rollback()
            ref = db.get(ImportFileRef, ref_id)
            ref.status = ImportFileStatus.FAILED
            ref.last_error = str(exc)[:500]
            db.commit()
            report.failed += 1
            report.errors.append(f"{ref_id}: {exc}")
            logger.warning("[%d/%d] ref %s failed: %s", n, len(ref_ids), ref_id, exc)
            continue
        report.fetched += 1
        report.bytes_downloaded += size
        logger.info("[%d/%d] ref %s fetched (%d bytes)", n, len(ref_ids), ref_id, size)
    return report
