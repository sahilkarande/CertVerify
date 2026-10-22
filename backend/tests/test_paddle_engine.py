import os
import sys
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy.orm import Session

from app.models import DocumentPage, PageOcr
from app.models.enums import OcrStatus
from app.ocr.paddle import MAX_LONG_SIDE_PX, WORKER_SCRIPT, PaddleEngine, WorkerError
from app.ocr.pipeline import ocr_page, preferred_readings
from app.storage import EncryptedLocalStorage
from tests.test_ocr import FakeEngine, _page, document

FAKE_WORKER = Path(__file__).parent / "fixtures" / "fake_ocr_worker.py"


def fake(mode: str = "ok", **kw) -> PaddleEngine:
    return PaddleEngine(Path(sys.executable), det_model=mode, script=FAKE_WORKER, **kw)


def test_reads_words_through_the_worker() -> None:
    with fake() as engine:
        assert engine.version == "fake ok"
        result = engine.read(np.zeros((321, 200, 3), np.uint8))
        assert [w.text for w in result.words] == ["word", "321"]  # image arrived intact
        assert result.words[0].conf == 95.0 and result.text == "word 321"
        engine.read(np.zeros((50, 50), np.uint8))  # same worker serves many pages


def test_worker_error_fails_the_page_only() -> None:
    with fake("error") as engine:
        with pytest.raises(WorkerError, match="ValueError"):
            engine.read(np.zeros((10, 10, 3), np.uint8))
        assert engine._proc.poll() is None  # still running for the next page


def test_crashed_worker_is_restarted_for_the_next_page() -> None:
    with fake("crash_first") as engine:
        with pytest.raises(WorkerError, match="exited"):
            engine.read(np.zeros((10, 10, 3), np.uint8))
        assert engine.read(np.zeros((10, 10, 3), np.uint8)).words


def test_hung_worker_is_killed() -> None:
    engine = fake("hang", page_timeout_s=0.5)
    with pytest.raises(WorkerError, match="timed out"):
        engine.read(np.zeros((10, 10, 3), np.uint8))
    assert engine._proc is None


def test_worker_that_never_starts_times_out() -> None:
    with pytest.raises(WorkerError, match="timed out"):
        _ = fake("silent", startup_timeout_s=0.5).version


def test_missing_environment_is_explained(tmp_path: Path) -> None:
    with pytest.raises(WorkerError, match="paddle-setup"):
        _ = PaddleEngine(tmp_path / "nope" / "python").version


def test_prepare_bounds_large_images_and_keeps_colour() -> None:
    engine = fake()
    big = engine.prepare(np.zeros((5000, 3000, 3), np.uint8))
    assert max(big.shape[:2]) == MAX_LONG_SIDE_PX and big.ndim == 3
    small = np.zeros((1000, 800, 3), np.uint8)
    assert engine.prepare(small) is small


# ---------- pipeline: rotation from Tesseract, reading from Paddle ----------


class Recorder(FakeEngine):
    name = "reader"


def test_reader_uses_rotation_stored_by_the_rotation_engine(
    session: Session, storage: EncryptedLocalStorage
) -> None:
    page = _page(session, storage, document(w=400, h=600))
    rotator = FakeEngine()
    session.add(PageOcr(page_id=page.id, engine="fake", engine_version="1", pipeline_version=1,
                        status=OcrStatus.OK, rotation=90, text="", words=[]))  # fmt: skip
    session.commit()

    reader = Recorder()
    row = ocr_page(session, storage, reader, page, rotation_engine=rotator)
    assert row.rotation == 90 and row.engine == "reader"
    assert reader.shapes == [(400, 600, 3)]  # read once, already turned
    assert rotator.shapes == []  # stored rotation reused, not recomputed


def test_rotation_is_computed_when_not_stored(
    session: Session, storage: EncryptedLocalStorage
) -> None:
    page = _page(session, storage, document(w=400, h=600))
    rotator, reader = FakeEngine(), Recorder()
    row = ocr_page(session, storage, reader, page, rotation_engine=rotator)
    assert row.rotation == 0 and len(reader.shapes) == 1 and rotator.shapes


def _reading(db: Session, page: DocumentPage, engine: str, status=OcrStatus.OK) -> None:
    db.add(PageOcr(page_id=page.id, engine=engine, engine_version="1", pipeline_version=1,
                   status=status, text=engine, words=[]))  # fmt: skip


def test_preferred_reading_is_paddle_unless_it_failed(
    session: Session, storage: EncryptedLocalStorage
) -> None:
    both = _page(session, storage, document(w=100, h=100))
    paddle_failed = _page(session, storage, document(w=100, h=100))
    only_failed = _page(session, storage, document(w=100, h=100))
    _reading(session, both, "tesseract")
    _reading(session, both, "paddle")
    _reading(session, paddle_failed, "tesseract")
    _reading(session, paddle_failed, "paddle", OcrStatus.FAILED)
    _reading(session, only_failed, "tesseract", OcrStatus.FAILED)
    session.commit()

    got = preferred_readings(session, [both.id, paddle_failed.id, only_failed.id])
    assert got[both.id].engine == "paddle"
    assert got[paddle_failed.id].engine == "tesseract"
    assert got[only_failed.id].status is OcrStatus.FAILED  # still there for fallback rules
    assert preferred_readings(session, []) == {}


# ---------- the real worker (slow: loads the models) ----------

PADDLE_PYTHON = Path(__file__).resolve().parents[1] / ".paddle-venv" / "bin" / "python"


@pytest.mark.skipif(
    not (PADDLE_PYTHON.is_file() and os.environ.get("CERTVERIFY_TEST_PADDLE")),
    reason="set CERTVERIFY_TEST_PADDLE=1 (needs make paddle-setup)",
)
def test_real_paddle_worker_reads_a_document() -> None:
    from tests.test_ocr import LINES

    with PaddleEngine(PADDLE_PYTHON, script=WORKER_SCRIPT) as engine:
        assert engine.version.startswith("paddleocr ")
        result = engine.read(engine.prepare(document()))
    text = result.text.upper()
    found = [ln for ln in LINES if ln.split()[0].upper() in text]
    assert len(found) >= len(LINES) - 1, text
