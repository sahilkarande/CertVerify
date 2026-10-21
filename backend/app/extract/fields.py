"""Generic, layout-independent field finders.

Every finder returns candidates; the best one per field is kept. Values are found next to
their label (in the same OCR box, to its right on the same line, or below it in a table
column), so one set of rules covers the ~35 boards and universities in the seed data.
Confidence is a rule-based estimate in [0, 1]: how the value was found, minus penalties for
repaired characters and weak OCR; cross-checks (e.g. total / maximum = percentage) raise it.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from app.extract.layout import Box, Layout
from app.extract.values import (
    clean_name,
    parse_gpa,
    parse_ids,
    parse_percentage,
    parse_result,
    parse_total,
)
from app.services.names import normalize_name

SAME_BOX, RIGHT, BELOW = 0.9, 0.85, 0.75
REPAIRED_PENALTY = 0.15
WEAK_OCR_PENALTY = 0.1
WEAK_OCR_BELOW = 80.0
# Labels in the page header are usually about the institution (e.g. "NAAC ... CGPA 3.09").
HEADER_FRACTION = 0.12
HEADER_PENALTY = 0.3


@dataclass
class Candidate:
    key: str
    value: str
    confidence: float
    box: Box | None = None
    evidence: list[str] = field(default_factory=list)


def _conf(base: float, repaired: bool, *boxes: Box) -> float:
    c = base - (REPAIRED_PENALTY if repaired else 0)
    if any(b.conf < WEAK_OCR_BELOW for b in boxes):
        c -= WEAK_OCR_PENALTY
    return round(max(c, 0.05), 2)


def labelled(
    layout: Layout,
    key: str,
    label: re.Pattern,
    parse: Callable,
    *,
    use_below: bool = True,
    before: bool = False,
    tail: int = 1,
    tolerance: float = 0.6,
    max_gap: float = 8,
) -> list[Candidate]:
    """Values found next to every occurrence of ``label``: after it in the same box, in the
    boxes to its right (``tail`` > 1 parses that many joined together, for 'max  obtained'
    pairs), below it in a table column, or (``before``) just before it ('with 7.18 CGPI')."""
    out = []
    for box in layout.boxes:
        m = label.search(box.text.upper())
        if not m:
            continue
        header = box.y < HEADER_FRACTION * layout.height
        penalty = HEADER_PENALTY if header else 0
        rest = box.text[m.end() :]
        right = layout.right_of(box, limit=max(2, tail), tolerance=tolerance)
        if tail > 1 and right:
            rest = " ".join([rest, *(b.text for b in right[:tail])])
        found = parse(rest)
        if found:
            value, repaired = found
            c = _conf(SAME_BOX - penalty, repaired, box)
            out.append(Candidate(key, value, c, box, ["same_box" if tail == 1 else "row"]))
            continue
        if before:
            prev = re.search(r"(\d[.,]\s?\d{1,2})\s*\W*$", box.text[: m.start()])
            found = parse(prev.group(1)) if prev else None
            if found:
                value, repaired = found
                c = _conf(SAME_BOX - 0.1 - penalty, repaired, box)
                out.append(Candidate(key, value, c, box, ["before_label"]))
                continue
        places = [(RIGHT, "right", b) for b in right[:2]]
        if use_below:
            places += [(BELOW, "below", b) for b in layout.below(box, limit=3, max_gap=max_gap)]
        for base, how, b in places:
            if label.search(b.text.upper()):
                continue  # another label, e.g. a header row of the same table
            found = parse(b.text)
            if found:
                value, repaired = found
                out.append(Candidate(key, value, _conf(base - penalty, repaired, box, b), b, [how]))
                break
    return out


LATEST_MARGIN = 0.15


def best(cands: list[Candidate], *, latest: bool = False) -> Candidate | None:
    """Most confident candidate. With ``latest``, any nearly-as-confident candidate further
    down the page (then further right) wins: grade cards list semesters in order and put
    the summary (final SGPA, CGPA, grand total) last."""
    if not cands:
        return None
    top = max(c.confidence for c in cands)
    if not latest:
        return max(cands, key=lambda c: (c.confidence, c.box.y if c.box else 0))
    close = [c for c in cands if c.confidence >= top - LATEST_MARGIN and c.box]
    lowest = max(c.box.cy for c in close)
    last_row = [c for c in close if lowest - c.box.cy < 0.6 * c.box.h]
    return max(last_row, key=lambda c: (c.box.x, c.confidence))


# ---------- grades, marks, result ----------

# OCR variants seen: SGP1, 5GPE, CGP1, C.G.P.A.
SGPA_LABEL = re.compile(
    r"(?<![A-Z0-9.])[S5]\.?\s?G\.?\s?P\.?\s?[AI1LE]\.?(?![A-Z])|(?<![A-Z])(SPI|GPA)(?![A-Z])"
    r"|SEMESTER\s*GRADE\s*P\w*\s*(AVERAGE|INDEX)"
)
CGPA_LABEL = re.compile(
    r"(?<![A-Z0-9.])C\.?\s?G\.?\s?P\.?\s?[AI1LE]\.?(?![A-Z])|(?<![A-Z])(CPI|FGPA|OGPA)(?![A-Z])"
    r"|CUMULATIVE\s*GRADE\s*P\w*\s*(AVERAGE|INDEX)"
)
PERCENT_LABEL = re.compile(r"PERCENTAGE|(?<![A-Z])PERCENT(?![A-Z])|PERCENTILE|^%$")
TOTAL_LABEL = re.compile(
    r"GRAND\s*TOTAL|TOTAL\s*MARKS|MARKS\s*OBTAINED|TOTAL\s*MAX\W*MI\w\W*\w?BT"
    r"|^TOTAL\b(?!\s*(CREDIT|CR\b|GRADE|GP|NO|INCENTIVE|MAX))"
)
RESULT_LABEL = re.compile(r"(?<![A-Z])(RESULT|REMARKS?|STATUS)(?![A-Z])")


def _gpa(text: str):
    return parse_gpa(text)


def _total(text: str):
    found = parse_total(text)
    if not found:
        return None
    (obtained, maximum), repaired = found
    return f"{obtained}/{maximum}" if maximum else obtained, repaired


def grades(layout: Layout) -> dict[str, Candidate]:
    out: dict[str, Candidate] = {}
    for key, label, parse in (
        ("sgpa", SGPA_LABEL, _gpa),
        ("cgpa", CGPA_LABEL, _gpa),
        ("percentage", PERCENT_LABEL, parse_percentage),
    ):
        c = best(labelled(layout, key, label, parse, before=key != "percentage"), latest=True)
        if c:
            out[key] = c
    totals = labelled(layout, "total_marks_obtained", TOTAL_LABEL, _total,
                      tail=4, tolerance=1.0, max_gap=4)  # fmt: skip
    pct = out.get("percentage")
    chosen = _consistent_total(totals, pct) or best(totals, latest=True)
    if chosen:
        obtained, _, maximum = chosen.value.partition("/")
        out["total_marks_obtained"] = Candidate(
            "total_marks_obtained", obtained, chosen.confidence, chosen.box, chosen.evidence
        )
        if maximum:
            out["total_marks_max"] = Candidate(
                "total_marks_max", maximum, chosen.confidence, chosen.box, chosen.evidence
            )
    result = best(labelled(layout, "result", RESULT_LABEL, parse_result))
    if result is None:
        # A bare "PASS" anywhere on the page, without a label.
        for b in layout.boxes:
            found = parse_result(b.text)
            if found and len(b.text) < 40:
                result = Candidate("result", found[0], _conf(0.6, False, b), b, ["unlabelled"])
                break
    if result:
        out["result"] = result
    return out


def _consistent_total(totals: list[Candidate], pct: Candidate | None) -> Candidate | None:
    """The total whose obtained / maximum matches the stated percentage (both confirmed)."""
    if pct is None:
        return None
    for c in totals:
        obtained, _, maximum = c.value.partition("/")
        if maximum and abs(int(obtained) / int(maximum) * 100 - float(pct.value)) < 0.6:
            c.confidence = pct.confidence = 0.95
            c.evidence.append("matches_percentage")
            pct.evidence.append("matches_total")
            return c
    return None


# ---------- identifiers ----------

SEAT_LABEL = re.compile(r"(?<![A-Z])SEAT(\s*(NO|NUMBER))?|ROLL\s*(NO|NUMBER)|EXAM\w*\s*NO")
STUDENT_ID_LABEL = re.compile(
    r"(?<![A-Z])(PRN|USN|ERN|APAAR|ABC\s*ID)(?![A-Z])|PERM\W*REG|ENROL+\w*|REG\w*\W*(NO|NUMBER)"
    r"|UNIQUE\s*ID|STUDENT\s*ID|ELIGIBILITY\s*NO|REGISTRATION|(?<![A-Z])R\.E\.N\."
)


def _first_id(text: str):
    ids = parse_ids(text)
    return ids[0] if ids else None


def identifiers(layout: Layout) -> dict[str, Candidate]:
    out = {}
    for key, label in (("seat_number", SEAT_LABEL), ("student_id", STUDENT_ID_LABEL)):
        c = best(labelled(layout, key, label, _first_id))
        if c:
            out[key] = c
    if (
        "seat_number" in out
        and "student_id" in out
        and out["seat_number"].value == out["student_id"].value
    ):
        del out["student_id"]  # one number next to both labels: keep it once
    return out


# ---------- names ----------

NAME_LABEL = re.compile(
    r"CANDIDATE'?S\s*FULL\s*NAME[^A-Z]*(\(SURNAME\s*F\w*\))?|NAME\s*OF\s*(THE\s*)?(STUDENT|CANDIDATE)"
    r"|STUDENT'?S?\s*NAME|CANDIDATE'?S?\s*NAME|^NAME\b\s*:?|(?<![A-Z'])NAME\s*[:=]"
    r"|SHRI\s*/\s*SMT\.?|SHRI\s*/\s*SUSHRI|THIS\s*IS\s*TO\s*CERTIFY\s*THAT\s*(MR\.?|MS\.?|SHRI|SMT\.?)?"
)
MOTHER_LABEL = re.compile(r"(?<!/)MOTHER'?\s*-?S?\s*(N\w{1,4}E)?\s*(IS)?\s*:?")
NOT_PERSON = re.compile(r"MOTHER|FATHER|GUARDIAN|HUSBAND|COLLEGE|INSTITUTE|COURSE|SUBJECT|PAPER|"
                        r"BRANCH|PROGRAM|SCHOOL|CENTRE|UNIVERSITY|EXAM")  # fmt: skip


NON_NAME_WORDS = re.compile(
    r"\b(ENGINEERING|MATHEMATICS|SCIENCE|TECHNOLOGY|UNIVERSITY|COLLEGE|"
    r"INSTITUTE|BOARD|SEMESTER|EXAMINATION|STATEMENT|MARKS|GRADE)\b"
)


def _person(text: str):
    if sum(c.isdigit() for c in text) >= 3 or NON_NAME_WORDS.search(text.upper()):
        return None
    v = clean_name(text)
    return (v, False) if v else None


def _full_name(text: str):
    found = _person(text)
    return found if found and len(found[0].split()) >= 2 else None


def _similar(a: str, b: str) -> bool:
    if len(a) <= 3 or len(b) <= 3:
        return a == b
    return SequenceMatcher(None, a, b).ratio() >= 0.8


def _split_merged(words: list[str], wanted: list[str]) -> list[str]:
    """'AKSHAYABALKRISHNA' -> 'AKSHAYA BALKRISHNA' when it starts or ends with a form-name
    word (OCR drops spaces between name words)."""
    out = []
    for w in words:
        for t in wanted:
            T = t.upper()
            if len(w) > len(T) + 2 and len(T) >= 4:
                if w.startswith(T):
                    out += [T, w[len(T) :]]
                    break
                if w.endswith(T):
                    out += [w[: -len(T)], T]
                    break
        else:
            out.append(w)
    return out


def match_form_name(value: str, full_name: str | None) -> tuple[str, int, int]:
    """Align a document name with the form name: split merged words and replace near-miss
    spellings with the form's ('FOOJA' -> 'POOJA'). Returns (value, matched, form words)."""
    wanted = normalize_name(full_name or "").split()
    words = _split_merged(value.split(), wanted)
    matched: set[str] = set()
    fixed = []
    for w in words:
        hit = next((t for t in wanted if t not in matched and _similar(w.lower(), t)), None)
        if hit:
            matched.add(hit)
            fixed.append(hit.upper())
        else:
            fixed.append(w)
    return " ".join(fixed), len(matched), len(wanted)


def known_name(layout: Layout, full_name: str) -> Candidate | None:
    """Find the applicant's own (form) name on the page, tolerating OCR damage and any word
    order. The value is the name as printed: the matched words plus adjacent name words
    (Maharashtra documents add the father's and mother's names)."""
    wanted = normalize_name(full_name).split()
    if not wanted:
        return None
    best_c: Candidate | None = None
    for box in layout.boxes:
        if NON_NAME_WORDS.search(box.text.upper()):
            continue
        words = _split_merged(re.sub(r"[^A-Z\s]", " ", box.text.upper()).split(), wanted)
        hits = [i for i, w in enumerate(words) if any(_similar(w.lower(), t) for t in wanted)]
        if not hits or (len(wanted) == 1 and len(words) > 3):
            continue
        lo, hi = min(hits), max(hits)
        tail = clean_name(" ".join(words[hi + 1 :]), max_tokens=2)
        value, matched, total = match_form_name(
            clean_name(" ".join(words[lo : hi + 1]) + (" " + tail if tail else "")), full_name
        )
        coverage = matched / total
        if coverage < 0.66:
            continue
        c = Candidate("candidate_name", value, round(0.6 + 0.35 * coverage, 2), box,
                      [f"form_name_match:{matched}/{total}"])  # fmt: skip
        if best_c is None or c.confidence > best_c.confidence:
            best_c = c
    return best_c


def names(layout: Layout, applicant_name: str | None) -> dict[str, Candidate]:
    out = {}
    from_form = known_name(layout, applicant_name) if applicant_name else None
    labelled_names = [
        c
        for c in labelled(layout, "candidate_name", NAME_LABEL, _full_name)
        if not NOT_PERSON.search(c.box.text.upper()[:12] if c.box else "")
    ]
    by_label = best(labelled_names)
    if by_label and applicant_name:
        by_label.value, matched, total = match_form_name(by_label.value, applicant_name)
        by_label.evidence.append(f"form_name_match:{matched}/{total}")
    if from_form and from_form.confidence >= 0.95:
        out["candidate_name"] = from_form
    elif by_label:
        # A labelled name that differs from the form is kept as printed, for review.
        by_label.confidence = min(by_label.confidence, 0.6)
        out["candidate_name"] = by_label
    elif from_form:
        out["candidate_name"] = from_form
    mother = best(labelled(layout, "mother_name", MOTHER_LABEL, _person))
    if mother is None:
        for b in layout.boxes:  # "... and SONALI (Mother)"
            m = re.search(r"([A-Z][A-Z ]{1,40})\(\s*MOTHER\s*\)", b.text.upper())
            v = clean_name(re.split(r"\bAND\b", m.group(1))[-1]) if m else ""
            if v:
                mother = Candidate("mother_name", v, _conf(0.8, False, b), b, ["sentence"])
                break
    if mother:
        out["mother_name"] = mother
    return out


def extract_page(words: list, applicant_name: str | None = None) -> dict[str, Candidate]:
    layout = Layout(words)
    out = names(layout, applicant_name)
    out.update(identifiers(layout))
    out.update(grades(layout))
    return out
