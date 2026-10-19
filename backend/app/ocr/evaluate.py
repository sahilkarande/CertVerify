"""Measure OCR against a hand-labelled ground-truth set.

Ground truth (git-ignored, contains PII) is a JSON list of
{"page_id", "group", "verdict", "rot", "keys": [strings that must be recovered]}.
The report contains aggregates only, never document text.
"""

import io
import json
import re
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image
from sqlalchemy.orm import Session

from app.models import DocumentPage
from app.models.enums import OcrStatus
from app.ocr.engine import OcrEngine
from app.ocr.pipeline import read_page
from app.services.files import read_file
from app.storage import ObjectStorage


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9.]", "", text.lower())


def key_recall(text: str, keys: list[str]) -> list[bool]:
    """Whether each key appears in the text, ignoring case, spaces and punctuation
    (OCR often splits '8.72' into '8 .72')."""
    t = _norm(text)
    return [_norm(k) in t for k in keys]


def key_kind(key: str) -> str:
    if re.fullmatch(r"[\d.]+", key):
        return "number"
    return "id" if re.search(r"\d", key) else "word"


@dataclass
class EvalReport:
    pages: int = 0
    keys_total: int = 0
    keys_found: int = 0
    rotation_correct: int = 0
    seconds_per_page: float = 0.0
    by_group: dict[str, list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0]))
    by_kind: dict[str, list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0]))
    by_status: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))

    @property
    def recall(self) -> float:
        return self.keys_found / self.keys_total if self.keys_total else 0.0

    def lines(self) -> list[str]:
        out = [
            f"pages: {self.pages} | key recall: {self.recall:.1%} "
            f"({self.keys_found}/{self.keys_total}) | rotation correct: "
            f"{self.rotation_correct}/{self.pages} | {self.seconds_per_page:.1f}s/page",
            "by group: "
            + ", ".join(f"{k}={a / b:.0%}" for k, (a, b) in sorted(self.by_group.items())),
            "by kind:  "
            + ", ".join(f"{k}={a / b:.0%}" for k, (a, b) in sorted(self.by_kind.items())),
        ]
        for status, recalls in sorted(self.by_status.items()):
            out.append(
                f"status {status:15} pages={len(recalls):3} mean key recall={np.mean(recalls):.0%}"
            )
        return out


def evaluate(
    db: Session, storage: ObjectStorage, engine: OcrEngine, ground_truth: Path
) -> EvalReport:
    items = json.loads(ground_truth.read_text())
    report = EvalReport(pages=len(items))
    started = time.perf_counter()
    for item in items:
        page = db.get(DocumentPage, uuid.UUID(item["page_id"]))
        rgb = np.asarray(Image.open(io.BytesIO(read_file(storage, page.image_file))).convert("RGB"))
        reading = read_page(engine, rgb)
        found = key_recall(reading.result.text, item["keys"])

        report.keys_total += len(found)
        report.keys_found += sum(found)
        report.rotation_correct += reading.rotation == item["rot"]
        group = report.by_group[f"{item['group']}/{item['verdict']}"]
        group[0] += sum(found)
        group[1] += len(found)
        for key, ok in zip(item["keys"], found, strict=True):
            kind = report.by_kind[key_kind(key)]
            kind[0] += ok
            kind[1] += 1
        report.by_status[reading.status.value].append(sum(found) / len(found))
    report.seconds_per_page = (time.perf_counter() - started) / max(len(items), 1)
    # Keep every status visible in the report even if no page got it.
    for s in OcrStatus:
        report.by_status.setdefault(s.value, [])
    report.by_status = {k: v for k, v in report.by_status.items() if v}
    return report
