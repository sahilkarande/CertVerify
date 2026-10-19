"""Parse and normalise the form CSV into typed rows. No database access here.

Warnings never contain personal data: they reference the CSV row number, the form number
and the field name only.
"""

import csv
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.importers.cdac_form import schema

DRIVE_ID_PATTERNS = (
    re.compile(r"[?&]id=([\w-]{10,})"),
    re.compile(r"/file/d/([\w-]{10,})"),
)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


class FormFormatError(ValueError):
    """The CSV does not have the expected columns; nothing can be imported."""


@dataclass
class FormRow:
    row_number: int  # 1-based line in the CSV, header = line 1
    form_number: str
    full_name: str
    email: str | None
    phone: str | None
    guardian_phone: str | None
    course: str
    submitted_at: datetime | None
    hostel_opted: bool | None
    degree_option_raw: str
    degree_status: str | None
    claimed_aggregate_percent: float | None
    # slot key -> Drive file ids (a cell may contain several links)
    drive_files: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class ParseResult:
    rows: list[FormRow]
    warnings: list[str]


def collapse_spaces(value: str) -> str:
    return " ".join(value.split())


def extract_drive_ids(cell: str) -> list[str]:
    ids: list[str] = []
    for part in re.split(r"[,\s]+", cell.strip()):
        for pattern in DRIVE_ID_PATTERNS:
            m = pattern.search(part)
            if m:
                ids.append(m.group(1))
                break
    return ids


def normalise_phone(value: str) -> str | None:
    digits = re.sub(r"\D", "", value)
    return digits or None


def _locate_columns(header: list[str]) -> dict[str, int]:
    prefixes = [
        schema.COL_TIMESTAMP,
        schema.COL_FORM_NUMBER,
        schema.COL_NAME,
        schema.COL_COURSE,
        schema.COL_MOBILE,
        schema.COL_GUARDIAN_MOBILE,
        schema.COL_EMAIL,
        schema.COL_HOSTEL,
        schema.COL_DEGREE_OPTION,
        schema.COL_AGGREGATE,
        *(slot.header_prefix for slot in schema.UPLOAD_SLOTS),
    ]
    found: dict[str, int] = {}
    problems: list[str] = []
    for prefix in prefixes:
        matches = [i for i, h in enumerate(header) if h.strip().startswith(prefix)]
        if len(matches) != 1:
            problems.append(f"{prefix!r}: {len(matches)} matching columns")
        else:
            found[prefix] = matches[0]
    if problems:
        raise FormFormatError("Unexpected CSV columns: " + "; ".join(problems))

    dup = [i for i, h in enumerate(header) if h.strip() == schema.COL_FORM_NO_DUP]
    if dup:
        found[schema.COL_FORM_NO_DUP] = dup[0]
    return found


def parse_rows(lines: Iterable[list[str]]) -> ParseResult:
    it = iter(lines)
    try:
        header = next(it)
    except StopIteration:
        raise FormFormatError("CSV is empty") from None
    cols = _locate_columns(header)
    tz = ZoneInfo(schema.FORM_TIMEZONE)

    rows: list[FormRow] = []
    warnings: list[str] = []
    seen: set[str] = set()

    for line_no, raw in enumerate(it, start=2):
        if not any(cell.strip() for cell in raw):
            continue

        def cell(prefix: str, raw: list[str] = raw) -> str:
            idx = cols[prefix]
            return raw[idx].strip() if idx < len(raw) else ""

        form_no = cell(schema.COL_FORM_NUMBER)

        def warn(msg: str, line_no: int = line_no, form_no: str = form_no) -> None:
            warnings.append(f"row {line_no} (form {form_no or '?'}): {msg}")

        if not form_no:
            warn("missing form number; row skipped")
            continue
        if form_no in seen:
            warn("duplicate form number; later row skipped")
            continue
        seen.add(form_no)

        if schema.COL_FORM_NO_DUP in cols and cell(schema.COL_FORM_NO_DUP) not in ("", form_no):
            warn("'Form Number' and 'Form No.' differ")

        name = collapse_spaces(cell(schema.COL_NAME))
        if not name:
            warn("missing name")

        email = cell(schema.COL_EMAIL).lower() or None
        if email and not EMAIL_RE.fullmatch(email):
            warn("email looks invalid")

        phone = normalise_phone(cell(schema.COL_MOBILE))
        if phone and len(phone) != 10:
            warn(f"mobile number has {len(phone)} digits (expected 10)")
        guardian = normalise_phone(cell(schema.COL_GUARDIAN_MOBILE))
        if guardian and len(guardian) != 10:
            warn(f"guardian number has {len(guardian)} digits (expected 10)")

        submitted_at = None
        ts = cell(schema.COL_TIMESTAMP)
        try:
            # Form times are IST; store UTC like every other timestamp (SQLite drops offsets).
            local = datetime.strptime(ts, schema.TIMESTAMP_FORMAT).replace(tzinfo=tz)
            submitted_at = local.astimezone(UTC)
        except ValueError:
            warn("unreadable timestamp")

        hostel_raw = cell(schema.COL_HOSTEL).lower()
        hostel = {"yes": True, "no": False}.get(hostel_raw)

        degree_raw = collapse_spaces(cell(schema.COL_DEGREE_OPTION))
        degree_status = schema.DEGREE_STATUS.get(degree_raw)
        if degree_status is None:
            warn("unknown degree option")

        aggregate = None
        agg_raw = cell(schema.COL_AGGREGATE).rstrip("%").strip()
        try:
            aggregate = float(agg_raw)
            if not 0 < aggregate <= 100:
                warn("aggregate percentage outside 0-100")
            elif aggregate <= 10:
                warn("aggregate looks like a CGPA, not a percentage")
        except ValueError:
            warn("aggregate percentage is not a number")

        drive_files: dict[str, list[str]] = {}
        for slot in schema.UPLOAD_SLOTS:
            value = cell(slot.header_prefix)
            ids = extract_drive_ids(value)
            if value and not ids:
                warn(f"slot '{slot.key}' has no recognisable Drive link")
            if ids:
                drive_files[slot.key] = ids

        rows.append(
            FormRow(
                row_number=line_no,
                form_number=form_no,
                full_name=name,
                email=email,
                phone=phone,
                guardian_phone=guardian,
                course=cell(schema.COL_COURSE),
                submitted_at=submitted_at,
                hostel_opted=hostel,
                degree_option_raw=degree_raw,
                degree_status=degree_status,
                claimed_aggregate_percent=aggregate,
                drive_files=drive_files,
            )
        )
    return ParseResult(rows=rows, warnings=warnings)


def parse_csv(path: Path) -> ParseResult:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return parse_rows(csv.reader(f))
