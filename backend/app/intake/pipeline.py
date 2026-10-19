"""Process one stored document: open → render pages → clean → assess → store page images.

Every document is processed in its own transaction. Page images are derived data: re-processing
replaces them, and the original upload is never touched.
"""

import io
import logging
from dataclasses import dataclass, field

from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.intake.cleanup import clean_page
from app.intake.errors import IntakeError
from app.intake.quality import assess_document, assess_identity_image
from app.intake.render import render_pages
from app.intake.source import open_source
from app.models import Document, DocumentPage, DocumentType, RequirementItem, Submission
from app.models.enums import DocumentCategory, DocumentStatus, QualityVerdict
from app.services.files import read_file, store_file
from app.storage import ObjectStorage

logger = logging.getLogger(__name__)

# Bump when rendering/cleanup/quality logic changes so documents get re-processed.
PROCESSING_VERSION = 2
PAGE_JPEG_QUALITY = 90


@dataclass
class ProcessResult:
    status: DocumentStatus
    pages: int = 0
    verdicts: dict[str, int] = field(default_factory=dict)
    error_code: str | None = None


def is_identity_slot(db: Session, doc: Document) -> bool:
    """True if every document type accepted in this upload slot is an identity image
    (photo/signature). Those are stored and shown, not read, so text-quality checks don't apply."""
    if doc.slot_key is None:
        return False
    item = db.scalar(
        select(RequirementItem)
        .join(Submission, Submission.requirement_set_id == RequirementItem.requirement_set_id)
        .where(Submission.id == doc.submission_id, RequirementItem.key == doc.slot_key)
    )
    if item is None or not item.accepted_type_codes:
        return False
    categories = set(
        db.scalars(
            select(DocumentType.category).where(DocumentType.code.in_(item.accepted_type_codes))
        )
    )
    return categories == {DocumentCategory.IDENTITY_IMAGE}


def _encode_jpeg(rgb) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, "JPEG", quality=PAGE_JPEG_QUALITY, optimize=True)
    return buf.getvalue()


def _drop_pages(db: Session, doc: Document) -> list[str]:
    """Remove page rows and their image records from the session. Returns the blob keys to
    delete *after* the transaction commits, so a rollback never leaves rows without blobs."""
    keys = []
    for page in list(doc.pages):
        image = page.image_file
        doc.pages.remove(page)
        db.flush()
        if image is not None:
            keys.append(image.object_key)
            db.delete(image)
    db.flush()
    return keys


def process_document(
    db: Session, storage: ObjectStorage, doc: Document, *, force: bool = False
) -> ProcessResult | None:
    """Returns None if the document is already processed at the current version."""
    if not force and doc.processing_version == PROCESSING_VERSION:
        return None

    identity = is_identity_slot(db, doc)
    written: list[str] = []
    stale: list[str] = []
    try:
        stale = _drop_pages(db, doc)
        src = open_source(read_file(storage, doc.file))
        try:
            result = ProcessResult(status=DocumentStatus.PROCESSED)
            for rendered in render_pages(src):
                if identity:
                    cleaned_rgb, crop, angle = rendered.image, None, 0.0
                    quality = assess_identity_image(rendered.image)
                else:
                    cleaned = clean_page(rendered.image)
                    cleaned_rgb, crop, angle = cleaned.image, cleaned.crop_box, cleaned.deskew_angle
                    # Trimming margins doesn't add pixels: scale the source size to the content.
                    frac = cleaned.content_fraction if crop else 1.0
                    source_px = round(rendered.source_short_px * max(frac, 1e-6) ** 0.5)
                    quality = assess_document(cleaned_rgb, source_px)
                    quality.metrics["content_fraction"] = cleaned.content_fraction

                stored = store_file(db, storage, _encode_jpeg(cleaned_rgb), mime_type="image/jpeg")
                written.append(stored.object_key)
                h, w = cleaned_rgb.shape[:2]
                doc.pages.append(
                    DocumentPage(
                        page_number=rendered.number,
                        image_file_id=stored.id,
                        width_px=w,
                        height_px=h,
                        source_short_px=quality.metrics.get("source_short_px"),
                        crop_box=crop,
                        deskew_angle=angle,
                        quality_verdict=quality.verdict,
                        quality_reasons=quality.reasons,
                        quality_metrics=quality.metrics,
                    )
                )
                result.pages += 1
                result.verdicts[quality.verdict.value] = (
                    result.verdicts.get(quality.verdict.value, 0) + 1
                )
            notes = dict(src.notes)
        finally:
            src.close()

        doc.page_count = result.pages
        doc.status = DocumentStatus.PROCESSED
        doc.processing_error = None
        doc.processing_notes = notes
    except IntakeError as exc:
        db.rollback()
        for key in written:
            storage.delete(key)
        doc = db.get(Document, doc.id)
        stale = _drop_pages(db, doc)
        doc.status = DocumentStatus.FAILED
        doc.processing_error = exc.code
        doc.processing_notes = {}
        result = ProcessResult(status=DocumentStatus.FAILED, error_code=exc.code)
    except Exception:
        db.rollback()
        for key in written:
            storage.delete(key)
        raise

    doc.processing_version = PROCESSING_VERSION
    doc.processed_at = utcnow()
    db.commit()
    for key in stale:
        storage.delete(key)
    return result


def summarize_verdicts(results: list[ProcessResult]) -> dict[str, int]:
    total: dict[str, int] = {v.value: 0 for v in QualityVerdict}
    for r in results:
        for k, n in r.verdicts.items():
            total[k] += n
    return total
