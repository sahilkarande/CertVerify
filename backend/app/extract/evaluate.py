"""Measure extraction against hand-labelled page values.

Labels (git-ignored, contain PII): JSON list of {"page_id", "name", "mother", "ids": [...],
"sgpa", "cgpa", "percentage", "total", "result"}; a key is present only if the page shows it.
The report has counts only, never values.
"""

import json
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from app.extract.fields import Candidate, extract_page
from app.models import Document, DocumentPage
from app.ocr.pipeline import preferred_readings

LABEL_TO_FIELD = {
    "name": "candidate_name",
    "mother": "mother_name",
    "sgpa": "sgpa",
    "cgpa": "cgpa",
    "percentage": "percentage",
    "total": "total_marks_obtained",
    "result": "result",
}
NUMERIC = {"sgpa", "cgpa", "percentage", "total"}
ID_FIELDS = ("seat_number", "student_id")
HIGH_CONFIDENCE = 0.9


def value_matches(label_key: str, got: str, want: str) -> bool:
    if label_key in NUMERIC:
        try:
            return abs(float(got) - float(want)) < 1e-6
        except ValueError:
            return False
    if label_key in ("name", "mother"):
        # Every labelled word must be there; documents may add father's/mother's names.
        return set(want.upper().split()) <= set(got.upper().split())
    return got.upper() == want.upper()


@dataclass
class FieldEvalReport:
    pages: int = 0
    counts: Counter = field(default_factory=Counter)  # "<key> n|ok|wrong|hi|hi_ok"

    def lines(self) -> list[str]:
        out = [f"pages: {self.pages}", "field          correct  wrong  missing   high-conf correct"]
        for key in [*LABEL_TO_FIELD, "ids"]:
            n, ok, wrong = (self.counts[f"{key} {s}"] for s in ("n", "ok", "wrong"))
            hi, hi_ok = self.counts[f"{key} hi"], self.counts[f"{key} hi_ok"]
            out.append(f"{key:13} {ok:3}/{n:<3}  {wrong:5}  {n - ok - wrong:7}   {hi_ok:3}/{hi:<3}")
        n = sum(self.counts[f"{k} n"] for k in [*LABEL_TO_FIELD, "ids"])
        ok = sum(self.counts[f"{k} ok"] for k in [*LABEL_TO_FIELD, "ids"])
        hi = sum(self.counts[f"{k} hi"] for k in [*LABEL_TO_FIELD, "ids"])
        hi_ok = sum(self.counts[f"{k} hi_ok"] for k in [*LABEL_TO_FIELD, "ids"])
        out.append(f"overall: {ok}/{n} ({ok / max(n, 1):.0%}) | high-confidence: "
                   f"{hi_ok}/{hi} correct ({hi_ok / max(hi, 1):.0%})")  # fmt: skip
        return out


def _score(report: FieldEvalReport, key: str, got: Candidate | None, want: str) -> None:
    report.counts[f"{key} n"] += 1
    if got is None:
        return
    ok = value_matches(key, got.value, want)
    report.counts[f"{key} {'ok' if ok else 'wrong'}"] += 1
    if got.confidence >= HIGH_CONFIDENCE:
        report.counts[f"{key} hi"] += 1
        report.counts[f"{key} hi_ok"] += ok


def evaluate(db: Session, labels_file: Path) -> FieldEvalReport:
    items = json.loads(labels_file.read_text())
    report = FieldEvalReport(pages=len(items))
    readings = preferred_readings(db, [uuid.UUID(i["page_id"]) for i in items])
    for item in items:
        page = db.get(DocumentPage, uuid.UUID(item["page_id"]))
        doc = db.get(Document, page.document_id)
        reading = readings.get(page.id)
        got = extract_page(reading.words if reading else [], doc.submission.applicant.full_name)
        for label_key, field_key in LABEL_TO_FIELD.items():
            if label_key in item:
                _score(report, label_key, got.get(field_key), item[label_key])
        for want in item.get("ids", []):
            found = [got[k] for k in ID_FIELDS if k in got]
            match = next((c for c in found if c.value == want.upper()), None)
            report.counts["ids n"] += 1
            if match:
                report.counts["ids ok"] += 1
                if match.confidence >= HIGH_CONFIDENCE:
                    report.counts["ids hi"] += 1
                    report.counts["ids hi_ok"] += 1
    return report
