"""Score stored page classifications against hand-labelled ground truth.

Ground truth (git-ignored): JSON list of {"page_id", "split", "type", "sems", "pg"} where
type is one of ssc/hsc/diploma/semester/provisional/degree/other. Aggregates only.
"""

import json
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from app.classify import rules
from app.models import DocumentPage, DocumentType

LABEL_TO_CODE = {
    "ssc": rules.SSC,
    "hsc": rules.HSC,
    "diploma": rules.DIPLOMA,
    "semester": rules.SEMESTER,
    "provisional": rules.PROVISIONAL,
    "degree": rules.DEGREE,
    "other": rules.SUPPORTING,
}


@dataclass
class ClassEvalReport:
    by_split: dict[str, list[int]] = field(default_factory=dict)  # split -> [correct, total]
    semester: list[int] = field(default_factory=lambda: [0, 0])
    wrong_type: int = 0  # labelled with a different type (worse than unknown)
    unknown: int = 0
    confusions: Counter = field(default_factory=Counter)

    def lines(self) -> list[str]:
        out = [
            "type accuracy: "
            + ", ".join(
                f"{s}={a}/{b} ({a / b:.0%})" for s, (a, b) in sorted(self.by_split.items())
            ),
            f"semester exact: {self.semester[0]}/{self.semester[1]} | "
            f"errors: wrong type={self.wrong_type}, unknown={self.unknown}",
        ]
        if self.confusions:
            out.append(
                "confusions: " + ", ".join(f"{a}->{b}={n}" for (a, b), n in self.confusions.items())
            )
        return out


def evaluate(db: Session, labels_file: Path) -> ClassEvalReport:
    labels = json.loads(labels_file.read_text())
    codes = {t.id: t.code for t in db.query(DocumentType)}
    report = ClassEvalReport()
    for label in labels:
        page = db.get(DocumentPage, uuid.UUID(label["page_id"]))
        got = codes.get(page.detected_type_id) if page and page.detected_type_id else None
        want = LABEL_TO_CODE[label["type"]]
        split = report.by_split.setdefault(label.get("split", "all"), [0, 0])
        split[1] += 1
        if got == want:
            split[0] += 1
        elif got is None:
            report.unknown += 1
            report.confusions[(label["type"], "unknown")] += 1
        else:
            report.wrong_type += 1
            report.confusions[(label["type"], got)] += 1
        if label["type"] in ("semester", "diploma") and label["sems"]:
            report.semester[1] += 1
            report.semester[0] += bool(page) and list(page.semesters or []) == label["sems"]
    return report
