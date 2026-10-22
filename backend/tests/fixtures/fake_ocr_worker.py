"""Stand-in for workers/paddle_ocr/worker.py: same protocol, no PaddleOCR.

The --det value picks the behaviour: ok | error | crash_first | hang | silent.
Replies with one word per image row-count so tests can check the image arrived intact.
"""

import argparse
import base64
import json
import sys
import time

ap = argparse.ArgumentParser()
ap.add_argument("--det", default="ok")
ap.add_argument("--rec", default="")
mode = ap.parse_args().det

if mode == "silent":
    time.sleep(60)
print("loading models...", file=sys.stderr)
print(json.dumps({"ready": True, "version": f"fake {mode}"}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    png = base64.b64decode(req["png_b64"])
    if mode == "crash_first" and req["id"] == 1:
        sys.exit(3)
    if mode == "hang":
        time.sleep(60)
    if mode == "error":
        print(json.dumps({"id": req["id"], "error": "ValueError"}), flush=True)
        continue
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    height = int.from_bytes(png[20:24], "big")  # IHDR height
    words = [["word", 95.0, 0, 0, 10, 10, "p.0"], [str(height), 88.0, 20, 0, 10, 10, "p.0"]]
    print(json.dumps({"id": req["id"], "words": words}), flush=True)
