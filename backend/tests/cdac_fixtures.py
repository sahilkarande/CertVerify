"""Synthetic C-DAC form rows (fictional people) built on the real header row."""

import csv
import json
from pathlib import Path

HEADER: list[str] = json.loads(
    (Path(__file__).parent / "fixtures" / "cdac_form_header.json").read_text()
)

# Column indexes in the real export (see docs/DATA_ANALYSIS.md).
SLOT_COLS = {
    9: "photo",
    10: "signature",
    11: "ssc",
    12: "hsc_or_diploma",
    14: "semester_marksheets",
    19: "provisional_or_latest_sem",
    20: "degree_or_fallback",
}


def drive_link(file_id: str) -> str:
    return f"https://drive.google.com/open?id={file_id}"


def make_row(form_no: str, **overrides: str) -> list[str]:
    row = [""] * len(HEADER)
    row[0] = "2/13/2026 18:38:25"
    row[1] = row[4] = form_no
    row[2] = "Test  Student   Example"
    row[3] = "PGCP-BDA"
    row[5] = "9000000001"
    row[6] = "9000000002"
    row[7] = "Test.Student@Example.com "
    row[8] = "Yes"
    row[13] = "Already having Degree Certificate"
    row[16] = row[18] = "I agree to this Undertaking."
    row[21] = "75"
    for col in SLOT_COLS:
        row[col] = drive_link(f"{form_no}-file-{col:02d}")
    for key, value in overrides.items():
        row[int(key.removeprefix("c"))] = value
    return row


def write_csv(path: Path, rows: list[list[str]], header: list[str] | None = None) -> Path:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header or HEADER)
        w.writerows(rows)
    return path
