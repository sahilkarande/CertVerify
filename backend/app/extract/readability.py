"""Is the text OCR read from a page trustworthy enough to use?

The image quality check (app/intake/quality.py) judges the picture: blur, size, contrast. A
sharp photo can still be misread, e.g. faint dot-matrix print, where PaddleOCR returns
confident-looking nonsense ("CANHE PONOJA" for "GADHE POOJA"). This layer judges the reading:

* CLEAR: the key values (name, ID, marks) were read on well-read lines, or something
  independent agrees with a weakly read one (the form name; the other OCR engine).
* CONFIRM: unsure. The student types those values as printed, without being shown what was
  read, and they are accepted only if they match the OCR text closely (tolerating the kind
  of damage OCR does). A fraudster cannot type arbitrary values: they must fit the page.
* CONFIRMED: the typed values matched; they replace the machine values (source STUDENT,
  with the machine reading kept alongside for the verifier).
* RETAKE: too little was read well, or the typed values did not match twice: a clearer copy
  is needed.

Thresholds are calibrated on PaddleOCR line confidences over the seed pages (see
docs/DATA_ANALYSIS.md); pages read only by another engine are not assessed.
"""

import re
import uuid
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.extract.values import _numbers, fix_digits
from app.models import DocumentPage, DocumentType, ExtractedField, PageOcr
from app.models.enums import DocumentCategory, FieldSource, OcrStatus, Readability
from app.ocr.pipeline import preferred_readings
from app.services.names import normalize_name

# Bump when the rules below change so pages are re-assessed.
READABILITY_VERSION = 2
CALIBRATED_ENGINES = {"paddle"}

WEAK_WORD_BELOW = 80.0  # a word on a line read with less confidence than this is weak
CONFIRM_WEAK_SHARE = 0.35  # share of the page's text on weak lines: page is in doubt
RETAKE_WEAK_SHARE = 0.8  # so little read well that asking the student would not help
KEY_LINE_MIN = 85.0  # a key value read on a weaker line than this is unsure
MAX_ATTEMPTS = 2  # typed values that do not match twice: ask for a clearer copy

NAME = "candidate_name"
ID_KEYS = ("seat_number", "student_id")
GRADE_KEYS = ("total_marks_obtained", "percentage", "sgpa", "cgpa")

# Typed-name matching: every typed word must resemble a word on one line, and on average
# closely ("GADHE POOJA DEEPAKRAO" ~ "CANHE PONOJA DEEPAIORAT"; another person's name not).
NAME_WORD_MIN = 0.5
NAME_MEAN_MIN = 0.75
ID_MIN_LENGTH_FOR_SLIP = 6  # longer IDs may differ from the reading by one damaged character

# app/extract/fields.py gives a name this confidence only when every form-name word was found.
FULL_FORM_NAME_CONFIDENCE = 0.95

EXACT_CONFIDENCE = 0.9
CLOSE_CONFIDENCE = 0.7


# ---------- assessing a page ----------


def _weak_share(words: list) -> float:
    total = sum(len(str(w[0])) for w in words)
    weak = sum(len(str(w[0])) for w in words if w[1] < WEAK_WORD_BELOW)
    return weak / total if total else 1.0


def _line_confidence(f: ExtractedField, words: list, engine: str) -> float | None:
    """Lowest OCR confidence of the words under a field's box (None if unknown)."""
    b = f.bbox
    if not b or b.get("engine") != engine:
        return None
    confs = [
        w[1]
        for w in words
        if b["x"] - 2 <= w[2] + w[4] / 2 <= b["x"] + b["w"] + 2
        and b["y"] - 2 <= w[3] + w[5] / 2 <= b["y"] + b["h"] + 2
    ]
    return min(confs) if confs else None


def assess_page(db: Session, page: DocumentPage) -> Readability | None:
    """(Re-)assess one page after extraction. Caller commits. A page the student has
    already answered for keeps its state (re-extraction must not ask them again)."""
    details = page.readability_details or {}
    if page.readability == Readability.CONFIRMED or details.get("attempts"):
        return page.readability
    doc_type = db.get(DocumentType, page.detected_type_id) if page.detected_type_id else None
    reading = preferred_readings(db, [page.id]).get(page.id)
    if (
        doc_type is None
        or doc_type.category != DocumentCategory.ACADEMIC_RECORD
        or reading is None
        or reading.engine not in CALIBRATED_ENGINES
    ):
        page.readability, page.readability_details = None, {}
        return None

    words = reading.words or []
    weak_share = round(_weak_share(words), 3)
    new = {"version": READABILITY_VERSION, "engine": reading.engine, "weak_share": weak_share}
    if reading.status == OcrStatus.NO_TEXT or weak_share >= RETAKE_WEAK_SHARE:
        page.readability, page.readability_details = Readability.RETAKE, {**new, "ask": []}
        return page.readability

    schema = {f["key"]: f for f in doc_type.field_schema}
    fields = {
        f.field_key: f
        for f in db.scalars(select(ExtractedField).where(ExtractedField.page_id == page.id))
    }
    page_in_doubt = weak_share >= CONFIRM_WEAK_SHARE

    other_lines: list[str] | None = None

    def corroborated(key: str, f: ExtractedField) -> bool:
        """A weakly read value is still trusted when something independent agrees: for the
        name, every word of the applicant's form name (garbage OCR does not spell it out);
        otherwise the other OCR engine reading exactly the same value on this page."""
        nonlocal other_lines
        if key == NAME:
            return (f.confidence or 0) >= FULL_FORM_NAME_CONFIDENCE
        if other_lines is None:
            others = db.scalars(
                select(PageOcr).where(
                    PageOcr.page_id == page.id,
                    PageOcr.engine != reading.engine,
                    PageOcr.status != OcrStatus.FAILED,
                )
            ).all()
            other_lines = _lines([r.words or [] for r in others])
        m = match_value(key, f.machine_value or "", other_lines)
        return m.ok and m.exact

    def unsure(key: str) -> str | None:
        f = fields.get(key)
        if f is None:
            return "missing"
        if f.source != FieldSource.MACHINE:
            return None  # a person already gave this value
        conf = _line_confidence(f, words, reading.engine)
        if conf is None or conf >= KEY_LINE_MIN or corroborated(key, f):
            return None
        return "weak"

    # One value of each kind at most: the name, an identifier, a grade.
    candidates: list[tuple[str, str]] = []
    if NAME in schema and (u := unsure(NAME)):
        candidates.append((NAME, u))
    ids = [k for k in ID_KEYS if k in schema]
    found_id = next((k for k in ids if k in fields), None)
    if found_id and (u := unsure(found_id)):
        candidates.append((found_id, u))
    elif not found_id and (req := next((k for k in ids if schema[k].get("required")), None)):
        candidates.append((req, "missing"))
    weak_grade = next((k for k in GRADE_KEYS if k in fields and unsure(k) == "weak"), None)
    if weak_grade:
        candidates.append((weak_grade, "weak"))

    # A value read on a weak line is always in doubt; a missing one only when the page as a
    # whole was read poorly (a well-read page simply may not print it, e.g. a back side).
    ask = [k for k, why in candidates if why == "weak" or page_in_doubt]
    page.readability = Readability.CONFIRM if ask else Readability.CLEAR
    page.readability_details = {**new, "ask": ask, "why": dict(candidates)}
    return page.readability


def pages_needing_assessment(db: Session, *, force: bool = False) -> list[uuid.UUID]:
    """Classified academic pages not yet assessed with the current rules (pages the student
    already answered for are skipped by ``assess_page`` itself)."""
    q = (
        select(DocumentPage)
        .join(DocumentType, DocumentType.id == DocumentPage.detected_type_id)
        .where(DocumentType.category == DocumentCategory.ACADEMIC_RECORD)
        .order_by(DocumentPage.document_id, DocumentPage.page_number)
    )
    return [
        p.id
        for p in db.scalars(q)
        if force or (p.readability_details or {}).get("version") != READABILITY_VERSION
    ]


# ---------- checking what the student typed ----------


@dataclass
class Match:
    ok: bool
    exact: bool = False
    read_as: str | None = None  # the OCR text it matched, kept as the machine value


def _lines(word_lists: list[list]) -> list[str]:
    out = []
    for words in word_lists:
        by_line: dict[str, list] = {}
        for w in words:
            by_line.setdefault(str(w[6]), []).append(w)
        out += [
            " ".join(str(w[0]) for w in sorted(ws, key=lambda w: w[2])) for ws in by_line.values()
        ]
    return out


def tidy_name(typed: str) -> str:
    return normalize_name(typed).upper()


def match_name(typed: str, lines: list[str]) -> Match:
    want = tidy_name(typed).split()
    if not want:
        return Match(False)
    best: tuple[float, Match] = (0.0, Match(False))
    for line in lines:
        toks = re.sub(r"[^A-Z]+", " ", line.upper()).split()
        toks += [a + b for a, b in zip(toks, toks[1:], strict=False)]  # OCR splits words
        if not toks:
            continue
        scores = [max(SequenceMatcher(None, t, x).ratio() for x in toks) for t in want]
        mean = sum(scores) / len(scores)
        if min(scores) >= NAME_WORD_MIN and mean >= NAME_MEAN_MIN and mean > best[0]:
            best = (mean, Match(True, min(scores) == 1.0, line.strip()[:120]))
    return best[1]


def tidy_id(typed: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", typed.upper())


def _damaged_slip(read: str, want: str) -> bool:
    """Same length, one position differs, and there OCR produced a non-digit: damage, not a
    different number (consecutive seat numbers differ by one digit, so a digit-for-digit
    difference is never forgiven)."""
    if len(read) != len(want):
        return False
    diff = [(a, b) for a, b in zip(read, want, strict=True) if a != b]
    return len(diff) == 1 and not diff[0][0].isdigit()


def match_id(typed: str, lines: list[str]) -> Match:
    want = tidy_id(typed)
    if len(want) < 3:
        return Match(False)
    close = None
    for line in lines:
        toks = re.findall(r"[A-Za-z0-9|]+", line)
        toks += [a + b for a, b in zip(toks, toks[1:], strict=False)]
        for t in toks:
            for v in {t.upper(), fix_digits(t)[0].upper()}:
                if v == want:
                    return Match(True, True, t)
                if close is None and len(want) >= ID_MIN_LENGTH_FOR_SLIP and _damaged_slip(v, want):
                    close = Match(True, False, t)
    return close or Match(False)


def tidy_number(typed: str) -> str | None:
    t = typed.strip().replace(",", ".").rstrip("%").strip()
    return t if re.fullmatch(r"\d{1,5}(\.\d{1,3})?", t) else None


def match_number(typed: str, lines: list[str]) -> Match:
    want = tidy_number(typed)
    if want is None:
        return Match(False)
    for line in lines:
        for value, repaired in _numbers(line):
            if abs(float(value) - float(want)) < 1e-9:
                return Match(True, not repaired, value)
    return Match(False)


def match_value(key: str, typed: str, lines: list[str]) -> Match:
    if key == NAME:
        return match_name(typed, lines)
    if key in ID_KEYS:
        return match_id(typed, lines)
    return match_number(typed, lines)


def tidy_value(key: str, typed: str) -> str | None:
    if key == NAME:
        return tidy_name(typed) or None
    if key in ID_KEYS:
        return tidy_id(typed) or None
    return tidy_number(typed)


@dataclass
class ConfirmResult:
    readability: Readability
    mismatched: list[str] = field(default_factory=list)
    attempts_left: int = 0


def confirm_page(db: Session, page: DocumentPage, answers: dict[str, str | None]) -> ConfirmResult:
    """Check the student's values for a CONFIRM page. ``answers`` maps every asked key to
    the value as printed, or None for "not printed on this page". Caller commits."""
    details = dict(page.readability_details or {})
    ask = list(details.get("ask", []))
    if page.readability != Readability.CONFIRM or set(answers) != set(ask):
        raise ValueError("these values were not asked for")
    for key, v in answers.items():
        if v is not None and tidy_value(key, v) is None:
            raise ValueError(f"{key}: not a valid value")

    readings = db.scalars(select(PageOcr).where(PageOcr.page_id == page.id)).all()
    lines = _lines([r.words or [] for r in readings if r.status != OcrStatus.FAILED])
    matches = {k: match_value(k, v, lines) for k, v in answers.items() if v is not None}
    mismatched = [k for k, m in matches.items() if not m.ok]
    attempts = int(details.get("attempts", 0)) + 1
    details.update(attempts=attempts, mismatched=mismatched)

    if mismatched:
        page.readability = Readability.RETAKE if attempts >= MAX_ATTEMPTS else Readability.CONFIRM
        page.readability_details = details
        return ConfirmResult(page.readability, mismatched, max(0, MAX_ATTEMPTS - attempts))

    existing = {
        f.field_key: f
        for f in db.scalars(select(ExtractedField).where(ExtractedField.page_id == page.id))
    }
    for key, m in matches.items():
        old = existing.get(key)
        if old is not None and old.source == FieldSource.HUMAN:
            continue  # a verifier's correction wins
        if old is not None:
            db.delete(old)
            db.flush()
        db.add(
            ExtractedField(
                document_id=page.document_id,
                page_id=page.id,
                field_key=key,
                value=tidy_value(key, answers[key]),
                machine_value=m.read_as,
                confidence=EXACT_CONFIDENCE if m.exact else CLOSE_CONFIDENCE,
                source=FieldSource.STUDENT,
                bbox=old.bbox if old is not None else None,
            )
        )
    details.update(
        not_printed=[k for k, v in answers.items() if v is None],
        inexact=[k for k, m in matches.items() if not m.exact],
    )
    page.readability = Readability.CONFIRMED
    page.readability_details = details
    db.flush()
    return ConfirmResult(page.readability)
