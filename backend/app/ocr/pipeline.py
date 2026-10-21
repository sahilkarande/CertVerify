"""Run OCR on processed pages: prepare → choose rotation → read → judge readability → store.

Tesseract's own orientation detection (OSD) was right on only 28/40 labelled pages (it skips
short certificates and misreads dot-matrix sheets). Reading the page at each candidate
rotation and keeping the most confident result was right on 40/40, so that is used instead.
"""

import io
import logging
import time
from dataclasses import dataclass

import numpy as np
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Document, DocumentPage, PageOcr
from app.models.enums import OcrStatus, QualityVerdict
from app.ocr.engine import OcrEngine, OcrResult
from app.ocr.preprocess import rotate
from app.services.files import read_file
from app.storage import ObjectStorage

logger = logging.getLogger(__name__)

# Bump when preprocessing/rotation/readability logic changes.
OCR_PIPELINE_VERSION = 1

# A page that is upright and clearly readable needs no rotation search.
UPRIGHT_CONFIDENCE = 70.0
UPRIGHT_MIN_WORDS = 40
CANDIDATE_ROTATIONS = (90, 270, 180)

# When a page has been read by several engines, later steps use the first available here
# (PaddleOCR read 90% of labelled numbers vs Tesseract's 53%, see docs/DATA_ANALYSIS.md).
ENGINE_PREFERENCE = ("paddle", "tesseract")

# Readability (calibrated on the labelled sample, see docs).
NO_TEXT_MAX_WORDS = 5
LOW_CONFIDENCE_BELOW = 50.0


@dataclass
class PageReading:
    rotation: int
    result: OcrResult
    status: OcrStatus


def read_page(engine: OcrEngine, rgb: np.ndarray, rotation: int | None = None) -> PageReading:
    """Read a page; search for its rotation unless one is given (e.g. found by another engine)."""
    prepared = engine.prepare(rgb)
    if rotation is not None:
        return _judge(rotation, engine.read(rotate(prepared, rotation)))
    best_rot, best = 0, engine.read(prepared)
    upright_ok = (
        best.mean_confidence is not None
        and best.mean_confidence >= UPRIGHT_CONFIDENCE
        and len(best.words) >= UPRIGHT_MIN_WORDS
    )
    if not upright_ok:
        for rot in CANDIDATE_ROTATIONS:
            candidate = engine.read(rotate(prepared, rot))
            if candidate.score() > best.score():
                best_rot, best = rot, candidate
    return _judge(best_rot, best)


def _judge(rotation: int, best: OcrResult) -> PageReading:
    if len(best.words) <= NO_TEXT_MAX_WORDS:
        status = OcrStatus.NO_TEXT
    elif best.mean_confidence < LOW_CONFIDENCE_BELOW:
        status = OcrStatus.LOW_CONFIDENCE
    else:
        status = OcrStatus.OK
    return PageReading(rotation, best, status)


def ocr_page(
    db: Session,
    storage: ObjectStorage,
    engine: OcrEngine,
    page: DocumentPage,
    *,
    rotation_engine: OcrEngine | None = None,
) -> PageOcr:
    """OCR one page and upsert its PageOcr row (caller commits).

    With ``rotation_engine``, the page rotation comes from that engine (its stored reading,
    or a fresh one) and ``engine`` reads the page only once, already upright.
    """
    row = db.scalar(
        select(PageOcr).where(PageOcr.page_id == page.id, PageOcr.engine == engine.name)
    )
    if row is None:
        row = PageOcr(page_id=page.id, engine=engine.name)
        db.add(row)
    row.engine_version = engine.version
    row.pipeline_version = OCR_PIPELINE_VERSION

    started = time.perf_counter()
    try:
        rgb = np.asarray(Image.open(io.BytesIO(read_file(storage, page.image_file))).convert("RGB"))
        rotation = given_rotation(db, page.id, rgb, rotation_engine)
        reading = read_page(engine, rgb, rotation)
    except Exception as exc:  # engine crash/timeout: record it, keep going with other pages
        logger.warning("OCR failed for page %s: %s", page.id, type(exc).__name__)
        row.status, row.rotation = OcrStatus.FAILED, 0
        row.mean_confidence, row.word_count, row.text, row.words = None, 0, "", []
    else:
        row.status = reading.status
        row.rotation = reading.rotation
        row.mean_confidence = (
            round(reading.result.mean_confidence, 1)
            if reading.result.mean_confidence is not None
            else None
        )
        row.word_count = len(reading.result.words)
        row.text = reading.result.text
        row.words = [w.as_list() for w in reading.result.words]
    row.timing_ms = round((time.perf_counter() - started) * 1000)
    db.flush()
    return row


def given_rotation(db: Session, page_id, rgb: np.ndarray, rotation_engine: OcrEngine | None):
    """Rotation decided by ``rotation_engine`` (stored if available), or None to search."""
    if rotation_engine is None:
        return None
    rotation = stored_rotation(db, page_id, rotation_engine.name)
    return rotation if rotation is not None else read_page(rotation_engine, rgb).rotation


def stored_rotation(db: Session, page_id, engine_name: str) -> int | None:
    return db.scalar(
        select(PageOcr.rotation).where(
            PageOcr.page_id == page_id,
            PageOcr.engine == engine_name,
            PageOcr.pipeline_version == OCR_PIPELINE_VERSION,
            PageOcr.status != OcrStatus.FAILED,
        )
    )


def pages_needing_ocr(db: Session, engine_name: str, *, force: bool = False) -> list:
    """Ids of document pages (not identity images, not blank) that lack current OCR."""
    q = (
        select(DocumentPage.id)
        .join(Document, Document.id == DocumentPage.document_id)
        .where(
            DocumentPage.image_file_id.is_not(None),
            DocumentPage.quality_verdict.is_not(None),
            DocumentPage.quality_verdict != QualityVerdict.BLANK,
            Document.slot_key.not_in(["photo", "signature"]) | Document.slot_key.is_(None),
        )
        .order_by(Document.created_at, DocumentPage.page_number)
    )
    if not force:
        current = select(PageOcr.page_id).where(
            PageOcr.engine == engine_name, PageOcr.pipeline_version == OCR_PIPELINE_VERSION
        )
        q = q.where(DocumentPage.id.not_in(current))
    return list(db.scalars(q))


def preferred_readings(db: Session, page_ids: list) -> dict:
    """page_id -> the PageOcr later steps should use: a successful reading from the most
    preferred engine, else a failed one (so the page still gets the fallback rules)."""
    if not page_ids:
        return {}
    rank = {name: i for i, name in enumerate(ENGINE_PREFERENCE)}

    def key(row: PageOcr) -> tuple:
        return (row.status == OcrStatus.FAILED, rank.get(row.engine, len(rank)))

    best: dict = {}
    for row in db.scalars(select(PageOcr).where(PageOcr.page_id.in_(page_ids))):
        if row.page_id not in best or key(row) < key(best[row.page_id]):
            best[row.page_id] = row
    return best
