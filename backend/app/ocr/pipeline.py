"""Run OCR on processed pages: prepare → choose rotation → read → judge readability → store.

Tesseract's own orientation detection (OSD) was right on only 28/40 labelled pages (it skips
short certificates and misreads dot-matrix sheets). Reading the page at each candidate
rotation and keeping the most confident result was right on 40/40, but costs up to four full
reads. So OSD is trusted only when it is sure (confidence >= 2: never wrong on the 40
labelled pages, 76/77 agreeing with the search on a wider sample, ~1 s instead of 3-7 s) and
the search runs otherwise (see ``decide_rotation``).
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
from app.ocr.engine import OcrEngine, OcrResult, TesseractEngine
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

# OSD answers below this confidence were often wrong; then the rotation search decides.
OSD_MIN_CONFIDENCE = 2.0

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


def decide_rotation(engine: TesseractEngine, rgb: np.ndarray) -> tuple[int, PageReading | None]:
    """The page's rotation: from OSD when it is sure, else from the full rotation search.
    Returns the search's reading too (when one was needed) so it is stored, not wasted."""
    osd = engine.orientation(engine.prepare(rgb))
    if osd is not None and osd[1] >= OSD_MIN_CONFIDENCE:
        return osd[0], None
    reading = read_page(engine, rgb)
    return reading.rotation, reading


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
    started = time.perf_counter()
    try:
        rgb = load_rgb(storage, page)
        rotation = given_rotation(db, page.id, rgb, rotation_engine)
        reading: PageReading | Exception = read_page(engine, rgb, rotation)
    except Exception as exc:  # engine crash/timeout: record it, keep going with other pages
        reading = exc
    return save_reading(db, page, engine, reading, started)


def load_rgb(storage: ObjectStorage, page: DocumentPage) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(read_file(storage, page.image_file))).convert("RGB"))


def save_reading(
    db: Session,
    page: DocumentPage,
    engine: OcrEngine,
    reading: "PageReading | Exception",
    started: float,
) -> PageOcr:
    """Upsert the page's PageOcr row for ``engine`` (an exception is stored as FAILED)."""
    row = db.scalar(
        select(PageOcr).where(PageOcr.page_id == page.id, PageOcr.engine == engine.name)
    )
    if row is None:
        row = PageOcr(page_id=page.id, engine=engine.name)
        db.add(row)
    row.engine_version = engine.version
    row.pipeline_version = OCR_PIPELINE_VERSION
    if isinstance(reading, Exception):
        logger.warning("OCR failed for page %s: %s", page.id, type(reading).__name__)
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


def read_new_page(
    db: Session,
    storage: ObjectStorage,
    tesseract: TesseractEngine,
    reader: OcrEngine | None,
    page: DocumentPage,
) -> None:
    """The worker's path for an uploaded page (caller commits): rotation from OSD when it is
    sure, then one read by the main engine. Tesseract reads the whole page only when the
    rotation search needed it anyway, when there is no other engine, or when the main
    engine fails; ``second_opinion`` adds its reading later where a value is in doubt."""
    started = time.perf_counter()
    try:
        rgb = load_rgb(storage, page)
        rotation, searched = decide_rotation(tesseract, rgb)
    except Exception as exc:
        save_reading(db, page, tesseract, exc, started)
        return
    if searched is not None:
        save_reading(db, page, tesseract, searched, started)
    main = reader if reader is not None else tesseract
    if main is tesseract and searched is not None:
        return
    started = time.perf_counter()
    try:
        reading: PageReading | Exception = read_page(main, rgb, rotation)
    except Exception as exc:
        reading = exc
    row = save_reading(db, page, main, reading, started)
    if row.status == OcrStatus.FAILED and main is not tesseract and searched is None:
        started = time.perf_counter()
        try:
            fallback: PageReading | Exception = read_page(tesseract, rgb, rotation)
        except Exception as exc:
            fallback = exc
        save_reading(db, page, tesseract, fallback, started)


def second_opinion(
    db: Session, storage: ObjectStorage, tesseract: TesseractEngine, page: DocumentPage
) -> bool:
    """Read the page with Tesseract too, upright as the main engine read it (caller commits).
    Returns False when it already has a Tesseract reading."""
    if db.scalar(
        select(PageOcr.id).where(PageOcr.page_id == page.id, PageOcr.engine == tesseract.name)
    ):
        return False
    main = preferred_readings(db, [page.id]).get(page.id)
    started = time.perf_counter()
    try:
        reading: PageReading | Exception = read_page(
            tesseract, load_rgb(storage, page), main.rotation if main else None
        )
    except Exception as exc:
        reading = exc
    save_reading(db, page, tesseract, reading, started)
    return True


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
