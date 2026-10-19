import json
import shutil
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DocumentPage, PageOcr
from app.models.enums import OcrStatus
from app.ocr.engine import OcrResult, TesseractEngine, Word
from app.ocr.evaluate import evaluate, key_kind, key_recall
from app.ocr.pipeline import (
    OCR_PIPELINE_VERSION,
    ocr_page,
    pages_needing_ocr,
    read_page,
)
from app.ocr.preprocess import remove_background, rotate
from app.services.files import store_file
from app.storage import EncryptedLocalStorage
from tests.factories import make_submission

needs_tesseract = pytest.mark.skipif(
    shutil.which("tesseract") is None, reason="tesseract-ocr not installed"
)

LINES = [
    "STATEMENT OF GRADES",
    "NAME : ASHA RAMESH PATIL",
    "SEAT NO : B190244471",
    "SEMESTER : SIXTH",
    "SGPA : 8.72   CGPA : 8.41",
    "RESULT : PASS",
]


def _font(size: int) -> ImageFont.ImageFont:
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
    ):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def security_paper(w: int, h: int) -> np.ndarray:
    """Tinted, unevenly lit, finely textured paper like the seed certificates."""
    rng = np.random.default_rng(0)
    x = np.linspace(0, 1, w)[None, :, None]
    y = np.linspace(0, 1, h)[:, None, None]
    tint = np.array([150, 190, 225]) * (1 - x) + np.array([230, 170, 190]) * x  # blue -> pink
    shade = 0.75 + 0.25 * y  # darker at the top, like a phone photo
    texture = rng.normal(0, 12, (h, w, 1))
    return np.clip(tint * shade + texture, 0, 255).astype(np.uint8)


def document(lines=LINES, w=1240, h=1754, pattern=False) -> np.ndarray:
    img = Image.fromarray(security_paper(w, h)) if pattern else Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    font = _font(40)
    for i, line in enumerate(lines):
        d.text((100, 150 + i * 110), line, fill="black", font=font)
    return np.asarray(img)


# ---------- pure logic ----------


def test_key_recall_ignores_case_spacing_and_punctuation() -> None:
    assert key_recall("Name: asha  PATIL\nSGPA 8 .72", ["PATIL", "8.72", "9.14"]) == [
        True,
        True,
        False,
    ]


@pytest.mark.parametrize(
    ("key", "kind"), [("8.72", "number"), ("B190244471", "id"), ("PATIL", "word")]
)
def test_key_kind(key: str, kind: str) -> None:
    assert key_kind(key) == kind


def test_result_text_groups_lines_and_score_prefers_more_confident_text() -> None:
    words = [
        Word("SGPA", 90, 0, 0, 10, 10, "1.1.1"),
        Word("8.72", 80, 20, 0, 10, 10, "1.1.1"),
        Word("PASS", 95, 0, 20, 10, 10, "1.1.2"),
    ]
    r = OcrResult(words)
    assert r.text == "SGPA 8.72\nPASS"
    assert r.mean_confidence == pytest.approx(88.33, abs=0.01)
    assert r.score() > OcrResult(words[:1]).score() > OcrResult().score() == 0.0
    assert words[0].as_list() == ["SGPA", 90, 0, 0, 10, 10, "1.1.1"]


def test_rotate_round_trip() -> None:
    img = np.arange(12, dtype=np.uint8).reshape(3, 4)
    assert rotate(img, 90).shape == (4, 3)
    assert np.array_equal(rotate(rotate(img, 90), 270), img)
    assert np.array_equal(rotate(img, 0), img)


def test_background_removal_keeps_text_and_flattens_tinted_paper() -> None:
    gray = cv2.cvtColor(document(pattern=True), cv2.COLOR_RGB2GRAY)
    cleaned = remove_background(gray)
    text_mask = cv2.cvtColor(document(), cv2.COLOR_RGB2GRAY) < 128
    background = ~cv2.dilate(text_mask.astype(np.uint8), np.ones((9, 9), np.uint8)).astype(bool)
    # Shading and tint are gone (top and bottom of the page look alike); text stays dark.
    h = gray.shape[0]
    top, bottom = background[: h // 4], background[3 * h // 4 :]
    raw_gap = abs(gray[: h // 4][top].mean() - gray[3 * h // 4 :][bottom].mean())
    new_gap = abs(cleaned[: h // 4][top].mean() - cleaned[3 * h // 4 :][bottom].mean())
    assert new_gap < raw_gap / 3
    assert cleaned[background].mean() - cleaned[text_mask].mean() > 80


# ---------- with the real engine ----------


@needs_tesseract
def test_reads_a_clean_document() -> None:
    reading = read_page(TesseractEngine(), document())
    assert reading.status is OcrStatus.OK and reading.rotation == 0
    found = key_recall(reading.result.text, ["PATIL", "B190244471", "8.72", "8.41", "PASS"])
    assert sum(found) >= 4


@needs_tesseract
@pytest.mark.parametrize("turned", [90, 270])
def test_sideways_page_is_turned_upright(turned: int) -> None:
    sideways = rotate(document(), turned)
    reading = read_page(TesseractEngine(), sideways)
    assert (reading.rotation + turned) % 360 == 0
    assert sum(key_recall(reading.result.text, ["PATIL", "SGPA", "PASS"])) >= 2


@needs_tesseract
def test_tinted_security_paper_is_still_read() -> None:
    reading = read_page(TesseractEngine(), document(pattern=True))
    assert sum(key_recall(reading.result.text, ["PATIL", "B190244471", "PASS"])) >= 2


@needs_tesseract
def test_blank_page_has_no_text() -> None:
    blank = np.full((1754, 1240, 3), 250, np.uint8)
    assert read_page(TesseractEngine(), blank).status is OcrStatus.NO_TEXT


# ---------- pipeline with database ----------


_refs = iter(range(100000001, 100009999))


def _page(db: Session, storage, rgb: np.ndarray, slot="ssc") -> DocumentPage:
    from io import BytesIO

    from app.models import Document

    sub = make_submission(db, ref=str(next(_refs)))
    original = store_file(db, storage, b"%PDF-1.4 original", mime_type="application/pdf")
    doc = Document(submission_id=sub.id, file_id=original.id, slot_key=slot)
    db.add(doc)
    buf = BytesIO()
    Image.fromarray(rgb).save(buf, "JPEG", quality=92)
    image = store_file(db, storage, buf.getvalue(), mime_type="image/jpeg")
    page = DocumentPage(page_number=1, image_file_id=image.id, quality_verdict="ok")
    doc.pages.append(page)
    db.commit()
    return page


class FakeEngine:
    name, version = "fake", "1"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def read(self, gray: np.ndarray) -> OcrResult:
        if self.fail:
            raise RuntimeError("engine crashed")
        return OcrResult([Word(f"w{i}", 90, i, 0, 5, 5, "1.1.1") for i in range(60)])


def test_ocr_page_upserts_and_is_tracked_by_version(
    session: Session, storage: EncryptedLocalStorage
) -> None:
    page = _page(session, storage, document())
    engine = FakeEngine()
    assert pages_needing_ocr(session, engine.name) == [page.id]

    row = ocr_page(session, storage, engine, page)
    session.commit()
    assert (row.status, row.word_count, row.pipeline_version) == (
        OcrStatus.OK,
        60,
        OCR_PIPELINE_VERSION,
    )
    assert pages_needing_ocr(session, engine.name) == []
    assert pages_needing_ocr(session, engine.name, force=True) == [page.id]

    ocr_page(session, storage, engine, page)  # re-run replaces, never duplicates
    session.commit()
    assert len(session.scalars(select(PageOcr)).all()) == 1


def test_engine_failure_is_recorded_not_raised(
    session: Session, storage: EncryptedLocalStorage
) -> None:
    page = _page(session, storage, document())
    row = ocr_page(session, storage, FakeEngine(fail=True), page)
    assert (row.status, row.word_count, row.text) == (OcrStatus.FAILED, 0, "")


def test_identity_images_and_blank_pages_are_not_ocrd(
    session: Session, storage: EncryptedLocalStorage
) -> None:
    photo = _page(session, storage, document(), slot="photo")
    blank = _page(session, storage, document())
    blank.quality_verdict = "blank"
    session.commit()
    todo = pages_needing_ocr(session, "fake")
    assert photo.id not in todo and blank.id not in todo


@needs_tesseract
def test_evaluate_reports_recall_without_document_text(
    session: Session, storage: EncryptedLocalStorage, tmp_path: Path
) -> None:
    page = _page(session, storage, document())
    gt = tmp_path / "gt.json"
    gt.write_text(
        json.dumps(
            [
                {
                    "page_id": str(page.id),
                    "group": "semester",
                    "verdict": "ok",
                    "rot": 0,
                    "keys": ["PATIL", "B190244471", "NOT-ON-PAGE"],
                }
            ]
        )
    )
    report = evaluate(session, storage, TesseractEngine(), gt)
    assert report.pages == 1 and report.rotation_correct == 1
    assert report.keys_total == 3 and 1 <= report.keys_found <= 2
    text = "\n".join(report.lines())
    assert "PATIL" not in text and "B190244471" not in text
