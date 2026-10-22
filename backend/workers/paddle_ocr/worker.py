"""PaddleOCR worker: runs in its own venv (backend/.paddle-venv), driven by app/ocr/paddle.py.

Protocol: one JSON object per line.
  worker -> app on start:  {"ready": true, "version": "..."}
  app -> worker:           {"id": 1, "png_b64": "..."}       (upright page image)
  worker -> app:           {"id": 1, "words": [[text, conf0-100, x, y, w, h, line_key], ...]}
                       or  {"id": 1, "error": "ExceptionName"}
Images travel through the pipe only, so page images (personal data) never touch the disk here.
This file must not import the application: the app's dependencies are not installed here.
"""

import argparse
import base64
import json
import os
import sys

# Libraries may print to stdout; keep the real stdout for the protocol and send
# everything else (including C-level output) to stderr.
_proto = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr


def send(msg: dict) -> None:
    _proto.write(json.dumps(msg) + "\n")
    _proto.flush()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--det", default="PP-OCRv6_tiny_det")
    ap.add_argument("--rec", default="PP-OCRv6_tiny_rec")
    ap.add_argument("--threads", type=int, default=None)
    args = ap.parse_args()

    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    import cv2
    import numpy as np
    import paddleocr
    from paddleocr import PaddleOCR

    kw = dict(
        text_detection_model_name=args.det,
        text_recognition_model_name=args.rec,
        # The app chooses the page rotation itself; skip Paddle's own orientation models.
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        # oneDNN: ~3x faster on Intel CPUs. Needs paddlepaddle 3.2.x (3.3.1 crashes with
        # ConvertPirAttribute2RuntimeAttribute); see requirements.txt.
        enable_mkldnn=True,
    )
    if args.threads:
        kw["cpu_threads"] = args.threads
    ocr = PaddleOCR(**kw)
    send({"ready": True, "version": f"paddleocr {paddleocr.__version__} {args.det}+{args.rec}"})

    for line in sys.stdin:
        if not line.strip():
            continue
        req = json.loads(line)
        try:
            buf = np.frombuffer(base64.b64decode(req["png_b64"]), np.uint8)
            img = cv2.imdecode(buf, cv2.IMREAD_COLOR)  # BGR, as Paddle expects
            if img is None:
                raise ValueError("undecodable image")
            words = []
            for res in ocr.predict(img):
                r = res.json["res"]
                for n, (text, score, box) in enumerate(
                    zip(r["rec_texts"], r["rec_scores"], r["rec_boxes"], strict=False)
                ):
                    x0, y0, x1, y1 = (int(v) for v in box)
                    # Paddle returns text lines; split them into words with proportional
                    # boxes so word-based extraction works the same for every engine.
                    parts = text.split()
                    total = sum(len(p) for p in parts) or 1
                    cx = x0
                    for p in parts:
                        pw = max(1, int((x1 - x0) * len(p) / total))
                        conf = round(float(score) * 100, 1)
                        words.append([p, conf, cx, y0, pw, y1 - y0, f"p.{n}"])
                        cx += pw
            send({"id": req.get("id"), "words": words})
        except Exception as exc:  # report and keep serving; the app decides what to do
            send({"id": req.get("id"), "error": type(exc).__name__})
    return 0


if __name__ == "__main__":
    sys.exit(main())
