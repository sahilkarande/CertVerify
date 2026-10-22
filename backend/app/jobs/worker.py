"""The background worker: ``make worker`` (python -m app.jobs.worker).

Loops: claim a job, run it, record the outcome. Ctrl+C (or SIGTERM) finishes the current
job and then stops; a second Ctrl+C stops at once (the job is retried later).
"""

import logging
import os
import signal
import socket
import time

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.security import load_storage_key
from app.db.session import Database
from app.jobs.pipeline import Engines, run_job
from app.jobs.queue import CHECK_DOCUMENT, KINDS, claim, fail, finish
from app.ocr.engine import TesseractEngine
from app.storage import EncryptedLocalStorage

logger = logging.getLogger("app.worker")
IDLE_SLEEP_S = 2.0


def build_engines(settings, *, reader: bool = True) -> Engines:
    engines = Engines(rotation=TesseractEngine(), models_dir=settings.models_dir)
    if not reader:
        return engines
    if settings.paddle_python.is_file():
        from app.ocr.paddle import PaddleEngine

        engines.reader = PaddleEngine(
            settings.paddle_python,
            det_model=settings.paddle_det_model,
            rec_model=settings.paddle_rec_model,
        )
        logger.info("OCR: Tesseract OSD for rotation, PaddleOCR for reading")
    else:
        logger.warning("PaddleOCR not installed (make paddle-setup): reading with Tesseract only")
    return engines


class Worker:
    def __init__(
        self, db: Database, storage, engines: Engines, worker_id: str, kinds=KINDS
    ) -> None:
        self.db, self.storage, self.engines, self.worker_id = db, storage, engines, worker_id
        self.kinds = tuple(kinds)
        self.stopping = False

    def run_once(self) -> bool:
        """Run one job if there is one. Returns whether a job was run."""
        with self.db.session_factory() as session:
            job = claim(session, self.worker_id, self.kinds)
            if job is None:
                return False
            started = time.perf_counter()
            try:
                run_job(session, self.storage, self.engines, job)
            except Exception as exc:  # recorded on the job; the worker keeps going
                session.rollback()
                logger.warning(
                    "job %s failed (attempt %s): %s", job.id, job.attempts, type(exc).__name__
                )
                fail(session, session.get(type(job), job.id), exc)
            else:
                finish(session, job)
                logger.info("job %s done in %.1fs", job.id, time.perf_counter() - started)
            return True

    def run_forever(self) -> None:
        while not self.stopping:
            if not self.run_once():
                time.sleep(IDLE_SLEEP_S)


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="CertVerify background worker")
    ap.add_argument(
        "--checks-only",
        action="store_true",
        help="only the quick upload checks (no OCR engines loaded): keeps students' feedback "
        "instant while another worker reads documents",
    )
    args = ap.parse_args()
    settings = get_settings()
    configure_logging(settings.log_level)
    db = Database(settings.database_url)
    storage = EncryptedLocalStorage(settings.storage_dir, load_storage_key(settings))
    kinds = (CHECK_DOCUMENT,) if args.checks_only else KINDS
    engines = build_engines(settings, reader=not args.checks_only)
    worker = Worker(db, storage, engines, f"{socket.gethostname()}:{os.getpid()}", kinds)

    def stop(signum, _frame) -> None:
        if worker.stopping:
            raise KeyboardInterrupt
        logger.info("stopping after the current job (press Ctrl+C again to stop now)")
        worker.stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    logger.info("worker %s started (%s)", worker.worker_id, ", ".join(kinds))
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        logger.info("stopped immediately; the interrupted job will be retried")
    finally:
        worker.engines.close()
        db.dispose()


if __name__ == "__main__":
    main()
