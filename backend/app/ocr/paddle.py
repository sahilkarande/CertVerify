"""PaddleOCR engine, run as a separate worker process (workers/paddle_ocr/worker.py).

PaddleOCR pins numpy/OpenCV versions that conflict with the app's, so it lives in its own
venv and is driven over a pipe. A worker crash or hang fails only the current page: the
worker is restarted for the next one.
"""

import base64
import json
import logging
import os
import selectors
import subprocess
import time
from pathlib import Path

import cv2
import numpy as np

from app.ocr.engine import OcrResult, Word

logger = logging.getLogger(__name__)

WORKER_SCRIPT = Path(__file__).resolve().parents[2] / "workers" / "paddle_ocr" / "worker.py"
# Bounds the image sent to the worker (memory, pipe size); labelled pages were all smaller.
MAX_LONG_SIDE_PX = 2500


class WorkerError(RuntimeError):
    pass


class PaddleEngine:
    name = "paddle"

    def __init__(
        self,
        python: Path,
        *,
        det_model: str = "PP-OCRv6_tiny_det",
        rec_model: str = "PP-OCRv6_tiny_rec",
        script: Path = WORKER_SCRIPT,
        startup_timeout_s: float = 300,
        page_timeout_s: float = 120,
    ) -> None:
        self.python = Path(python)
        self.script = Path(script)
        self.args = ["--det", det_model, "--rec", rec_model]
        self.startup_timeout_s = startup_timeout_s
        self.page_timeout_s = page_timeout_s
        self._proc: subprocess.Popen | None = None
        self._version: str | None = None
        self._next_id = 0

    # --- lifecycle ---------------------------------------------------------------
    def _start(self) -> None:
        if not self.python.is_file():
            raise WorkerError(f"PaddleOCR environment not found: {self.python} (make paddle-setup)")
        self._proc = subprocess.Popen(
            [str(self.python), str(self.script), *self.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,  # model-loading chatter; errors come back as messages
            text=True,
            bufsize=1,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        msg = self._receive(self.startup_timeout_s)
        if not msg.get("ready"):
            self.close()
            raise WorkerError("worker did not start")
        self._version = msg.get("version", "unknown")

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
            proc.wait()

    def __enter__(self) -> "PaddleEngine":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- protocol ----------------------------------------------------------------
    def _receive(self, timeout_s: float) -> dict:
        assert self._proc is not None
        deadline = time.monotonic() + timeout_s
        with selectors.DefaultSelector() as sel:
            sel.register(self._proc.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not sel.select(remaining):
                    self.close()  # hung: kill it; the next page gets a fresh worker
                    raise WorkerError("worker timed out")
                line = self._proc.stdout.readline()
                if not line:
                    self.close()
                    raise WorkerError("worker exited")
                return json.loads(line)

    @property
    def version(self) -> str:
        if self._version is None:
            self._start()
        return self._version

    def prepare(self, rgb: np.ndarray) -> np.ndarray:
        """Paddle was measured on the colour page; background removal is tuned for Tesseract."""
        h, w = rgb.shape[:2]
        scale = MAX_LONG_SIDE_PX / max(h, w)
        if scale < 1:
            size = (round(w * scale), round(h * scale))
            rgb = cv2.resize(rgb, size, interpolation=cv2.INTER_AREA)
        return rgb

    def read(self, image: np.ndarray) -> OcrResult:
        if self._proc is None or self._proc.poll() is not None:
            self._start()
        bgr = cv2.cvtColor(image, cv2.COLOR_RGB2BGR) if image.ndim == 3 else image
        ok, png = cv2.imencode(".png", bgr)
        if not ok:
            raise WorkerError("could not encode image")
        self._next_id += 1
        req = {"id": self._next_id, "png_b64": base64.b64encode(png.tobytes()).decode()}
        try:
            self._proc.stdin.write(json.dumps(req) + "\n")
            self._proc.stdin.flush()
        except BrokenPipeError as exc:
            self.close()
            raise WorkerError("worker exited") from exc
        msg = self._receive(self.page_timeout_s)
        if msg.get("id") != self._next_id:
            self.close()
            raise WorkerError("out-of-order reply")
        if "error" in msg:
            raise WorkerError(f"worker failed: {msg['error']}")
        return OcrResult(
            [Word(t, float(c), x, y, w, h, ln) for t, c, x, y, w, h, ln in msg["words"]]
        )
