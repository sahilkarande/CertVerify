"""One document through every step: quality -> OCR -> classify -> extract -> validate.

The same functions the batch commands use, run for a single document. Each step commits,
so the uploader sees progress and a crash resumes from the last finished step's results
(every step is safe to repeat).
"""

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.classify.pipeline import classify_document, type_id_map
from app.extract.pipeline import extract_page_fields
from app.intake.pipeline import is_identity_slot, process_document
from app.jobs.queue import progress
from app.models import Document, DocumentPage, Job, Submission
from app.models.enums import DocumentStatus, JobStage, QualityVerdict
from app.ocr.engine import OcrEngine
from app.ocr.pipeline import ocr_page
from app.storage import ObjectStorage
from app.validate.engine import validate_submission

logger = logging.getLogger(__name__)


@dataclass
class Engines:
    """OCR engines kept alive by the worker (PaddleOCR loads its models once)."""

    rotation: OcrEngine  # Tesseract: decides each page's rotation
    reader: OcrEngine | None = None  # PaddleOCR, when installed
    extra: dict = field(default_factory=dict)

    def close(self) -> None:
        if self.reader is not None and hasattr(self.reader, "close"):
            self.reader.close()


def run_document_job(db: Session, storage: ObjectStorage, engines: Engines, job: Job) -> None:
    doc = db.get(Document, job.document_id)
    if doc is None:  # deleted while queued
        return

    progress(db, job, JobStage.QUALITY)
    process_document(db, storage, doc, force=True)  # commits
    db.refresh(doc)

    if doc.status != DocumentStatus.FAILED and not is_identity_slot(db, doc):
        progress(db, job, JobStage.READING)
        for page in _readable_pages(db, doc):
            ocr_page(db, storage, engines.rotation, page)
            db.commit()
            if engines.reader is not None:
                ocr_page(db, storage, engines.reader, page, rotation_engine=engines.rotation)
                db.commit()

        progress(db, job, JobStage.CLASSIFYING)
        classify_document(db, doc, type_id_map(db))
        db.commit()

        progress(db, job, JobStage.EXTRACTING)
        for page in doc.pages:
            extract_page_fields(db, page)
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
