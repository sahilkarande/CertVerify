"""One document through every step: check (render + picture quality) -> OCR -> classify ->
extract (and judge how well the text was read) -> validate. Uploads run the check as its own
high-priority job and the rest as a second job (see app/jobs/queue.py).

The same functions the batch commands use, run for a single document. Each step commits,
so the uploader sees progress and a crash resumes from the last finished step's results
(every step is safe to repeat).
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.classify.pipeline import classify_document, load_model, phrase_rules, type_id_map
from app.extract.pipeline import extract_page_fields
from app.extract.readability import assess_page
from app.intake.pipeline import is_identity_slot, process_document
from app.jobs.queue import (
    CHECK_DOCUMENT,
    PROCESS_DOCUMENT,
    READ_DOCUMENT,
    UPLOAD_PRIORITY,
    enqueue_document,
    progress,
)
from app.models import Document, DocumentPage, Job, Submission
from app.models.enums import DocumentStatus, JobStage, QualityVerdict, Readability
from app.ocr.engine import OcrEngine, TesseractEngine
from app.ocr.pipeline import read_new_page, second_opinion
from app.storage import ObjectStorage
from app.validate.engine import validate_submission

logger = logging.getLogger(__name__)


@dataclass
class Engines:
    """OCR engines kept alive by the worker (PaddleOCR loads its models once)."""

    rotation: TesseractEngine  # decides each page's rotation; second opinion on doubted pages
    reader: OcrEngine | None = None  # PaddleOCR, when installed
    models_dir: Path | None = None  # trained models (page-type model, when one is active)
    extra: dict = field(default_factory=dict)

    def close(self) -> None:
        if self.reader is not None and hasattr(self.reader, "close"):
            self.reader.close()


def run_job(db: Session, storage: ObjectStorage, engines: Engines, job: Job) -> None:
    doc = db.get(Document, job.document_id)
    if doc is None:  # deleted while queued
        return
    if job.kind == CHECK_DOCUMENT:
        if check_document(db, storage, job, doc):
            enqueue_document(db, doc.id, priority=UPLOAD_PRIORITY, kind=READ_DOCUMENT)
            db.commit()
    elif job.kind == READ_DOCUMENT:
        read_document(db, storage, engines, job, doc)
    elif job.kind == PROCESS_DOCUMENT:
        if check_document(db, storage, job, doc):
            read_document(db, storage, engines, job, doc)
    else:
        raise ValueError(f"unknown job kind {job.kind!r}")


def check_document(db: Session, storage: ObjectStorage, job: Job, doc: Document) -> bool:
    """Render the pages and judge the pictures (seconds), then validate so the uploader sees
    any problem at once. Returns whether the document should be read."""
    progress(db, job, JobStage.QUALITY)
    process_document(db, storage, doc, force=True)  # commits
    db.refresh(doc)
    validate_submission(db, db.get(Submission, doc.submission_id))
    db.commit()
    return doc.status != DocumentStatus.FAILED and not is_identity_slot(db, doc)


def read_document(
    db: Session, storage: ObjectStorage, engines: Engines, job: Job, doc: Document
) -> None:
    """OCR -> classify -> extract (and judge how well it was read) -> validate."""
    progress(db, job, JobStage.READING)
    for page in _readable_pages(db, doc):
        read_new_page(db, storage, engines.rotation, engines.reader, page)
        db.commit()

    progress(db, job, JobStage.CLASSIFYING)
    classify_document(
        db, doc, type_id_map(db), load_model(db, engines.models_dir), phrase_rules(db)
    )
    db.commit()

    progress(db, job, JobStage.EXTRACTING)
    for page in doc.pages:
        extract_page_fields(db, page)
        if assess_page(db, page) is Readability.CONFIRM and second_opinion(
            db, storage, engines.rotation, page
        ):
            assess_page(db, page)  # the other engine may agree with the doubted values
    db.commit()

    progress(db, job, JobStage.VALIDATING)
    validate_submission(db, db.get(Submission, doc.submission_id))
    db.commit()


def _readable_pages(db: Session, doc: Document) -> list[DocumentPage]:
    return list(
        db.scalars(
            select(DocumentPage)
            .where(
                DocumentPage.document_id == doc.id,
                DocumentPage.image_file_id.is_not(None),
                DocumentPage.quality_verdict != QualityVerdict.BLANK,
            )
            .order_by(DocumentPage.page_number)
        )
    )
