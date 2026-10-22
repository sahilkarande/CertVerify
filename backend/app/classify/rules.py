"""Content rules for page types, measured on 80 hand-labelled seed pages
(docs/DATA_ANALYSIS.md, "Page classification").

Patterns are data: supporting a new board or university means adding a title here and a
labelled example to the evaluation set, not changing the decision logic.
"""

import re
from dataclasses import dataclass, field

from app.classify.text import best_ratio, compact, header_text, norm
from app.models.enums import AcademicLevel

# Registry codes (app/seeds/document_types.py).
SSC = "ssc_marksheet"
HSC = "hsc_marksheet"
DIPLOMA = "diploma_marksheet"
SEMESTER = "semester_marksheet"
PROVISIONAL = "provisional_degree_certificate"
DEGREE = "degree_certificate"
SUPPORTING = "supporting_page"

# School exam titles, matched on the space-free header. Board names such as "Board of Secondary
# and Higher Secondary Education" appear on both 10th and 12th sheets, so only titles count.
HSC_PATTERNS = [
    r"HIGHERSECONDARY(SCHOOL)?CERTIFICATE",
    r"SENIORSCHOOLCERTIFICATE",
    r"SENIORSECONDARY",
    r"10\+2",
    r"INTERMEDIATEEXAMINATION",
    r"PREUNIVERSITY",
    r"CLASSXII",
]
SSC_PATTERNS = [
    r"SECONDARYSCHOOLCERTIFICATE",
    r"SECONDARYSCHOOLEXAMINATION",
    r"CERTIFICATEOFSECONDARYEDUCATION",
    r"CLASSX(?!I)",
    r"HIGHSCHOOLEXAMINATION",
    r"MATRICULATION",
]
# Full titles for approximate matching of OCR-damaged headers ("SECONDARY SCHOO! CERTIFICATE").
HSC_TITLES = [
    "HIGHERSECONDARYCERTIFICATEEXAMINATION",
    "HIGHERSECONDARYSCHOOLCERTIFICATE",
    "SENIORSCHOOLCERTIFICATEEXAMINATION",
]
SSC_TITLES = [
    "SECONDARYSCHOOLCERTIFICATEEXAMINATION",
    "SECONDARYSCHOOLEXAMINATION",
    "CERTIFICATEOFSECONDARYEDUCATION",
    "SECONDARYEDUCATIONEXAMINATION",
]
FUZZY_MIN = 0.8
FUZZY_MARGIN = 0.03  # the two families must differ, or the title is too damaged to tell

DIPLOMA_PATTERNS = [r"BOARD OF TECHNICAL EDUCATION", r"POLYTECHNIC", r"DIPLOMA IN\b"]
PROVISIONAL_PATTERNS = [
    r"PROVISIONAL",
    r"PASSING CERTIFICATE",
    r"CERTIFY THAT .{0,80}PASSED\b",
    r"ELIGIBLE FOR THE (AWARD OF )?(THE )?(SAID )?DEGREE",
    r"PASSED THE .{0,80}DEGREE EXAMINATION",
    r"FULFILLED THE .{0,40}REQUIREMENT",
]
# Degree certificates are often only a sentence ("We, the Chancellor ... confer the degree").
DEGREE_PATTERNS = [
    r"DEGREE CERTIFICATE",
    r"CONFERRED",
    r"CONFERS? (UPON|THE DEGREE)",
    r"CONFER THE DEGREE",
    r"DULY QUALIFIED FOR THE DEGREE",
    r"CERTIFIES THAT",
    r"HAS BEEN ADMITTED TO THE DEGREE",
    r"DEGREE SERIAL",
    r"THIS DEGREE OF",
    r"\bWE,? THE CHANC",
    r"AWARDED THE DEGREE",
]
# Words only a provisional/passing certificate uses; checked before the degree wording, which
# provisional certificates also contain ("eligible for the said Degree Certificate").
PROVISIONAL_STRONG = [r"PROVISIONAL", r"PASSING CERTIFICATE"]
# Signals of a marks/grades statement; certificates have few of these.
MARKSHEET_PATTERNS = [
    r"SEMESTER",
    r"\bSEM\b",
    r"SGPA",
    r"\bSPI\b",
    r"SGPI",
    r"GRADE CARD",
    r"GRADE REPORT",
    r"STATEMENT OF (MARKS|GRADE)",
    r"PERFORMANCE REPORT",
    r"COURSE (CODE|TITLE|NAME)",
    r"CREDITS?",
]
BACKSIDE_PATTERNS = [r"SPEED POST", r"CONVOCATION NUMBER"]
PG_PATTERNS = [
    r"\bM\.? ?SC\b",
    r"\bMCA\b",
    r"MASTER OF",
    r"\bM\.? ?TECH\b",
    r"\bMBA\b",
    r"\bM\.? ?COM\b",
]

# Thresholds on the number of marksheet signals (measured).
PROVISIONAL_MAX_SIGNALS = 3
DEGREE_MAX_SIGNALS = 2
MARKSHEET_MIN_SIGNALS = 2
BACKSIDE_MAX_WORDS = 60

ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10}
ORDINAL = {
    "FIRST": 1,
    "SECOND": 2,
    "THIRD": 3,
    "FOURTH": 4,
    "FIFTH": 5,
    "SIXTH": 6,
    "SEVENTH": 7,
    "EIGHTH": 8,
}
# Roman numerals as OCR writes them: 'I' is often read as L, 1 or |  (VIII -> VILL, IV -> LV).
_NUM = r"([IVXL1|]{1,4}|[1-8]|FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH|EIGHTH)"
SEMESTER_PATTERNS = [
    rf"SEM(?:ESTER)?\s*[:.\-]*\s*\(?{_NUM}\b",
    rf"\b{_NUM}\s*(?:ST|ND|RD|TH)?\s*SEM(?:ESTER)?\b",
    rf"TERM\s*[:.\-]*\s*{_NUM}\b",
]
# SPPU "STATEMENT OF MARKS FOR F.E./S.E./T.E./B.E." covers both semesters of that year.
YEAR_STATEMENT = re.compile(r"\bFOR\s+(F|S|T|B)\s?\.?\s?E\s?\.")
YEAR_FIRST_SEMESTER = {"F": 1, "S": 3, "T": 5, "B": 7}
# SPPU year statements number semesters within the year ("SEM.:1", "SEM.:2" on an S.E. sheet)
# but end with e.g. "SECOND YEAR SGPA : 8.02", which gives the year when the title is damaged.
YEAR_SGPA = re.compile(r"\b(FIRST|SECOND|THIRD|FOURTH|FINAL) YEAR SGPA")
YEAR_SGPA_FIRST_SEMESTER = {"FIRST": 1, "SECOND": 3, "THIRD": 5, "FOURTH": 7, "FINAL": 7}


@dataclass
class Classification:
    type_code: str | None  # None = unknown
    fuzzy: bool = False
    confidence: float = 0.0
    semesters: list[int] = field(default_factory=list)
    level: AcademicLevel | None = None
    evidence: list[str] = field(default_factory=list)


def _first(text: str, patterns: list[str]) -> str | None:
    for p in patterns:
        if re.search(p, text):
            return p
    return None


def to_semester(token: str) -> int | None:
    token = token.upper()
    if token in ORDINAL:
        return ORDINAL[token]
    if token.isdigit():
        value = int(token)
        return value if 1 <= value <= 10 else None
    return ROMAN.get(re.sub(r"[L1|]", "I", token))


def find_semesters(header: str, full: str = "") -> list[int]:
    year = YEAR_STATEMENT.search(header)
    if year:
        first = YEAR_FIRST_SEMESTER[year.group(1)]
        return [first, first + 1]
    year = YEAR_SGPA.search(full)
    if year:
        first = YEAR_SGPA_FIRST_SEMESTER[year.group(1)]
        return [first, first + 1]
    found: list[int] = []
    for pattern in SEMESTER_PATTERNS:
        for m in re.finditer(pattern, header):
            value = to_semester(m.group(1))
            if value and value not in found:
                found.append(value)
    return sorted(found)[:2]


# "High School" is the 10th-grade exam on several boards (MP, UP), whose sheets also print
# "(10+2)" for the scheme; it must win over the 10+2 pattern.
HIGH_SCHOOL_10TH = r"(?<!SENIOR)(?<!HIGHER)HIGHSCHOOLCERTIFICATE"


def school_level(header_compact: str) -> tuple[str | None, bool, str]:
    """(type, fuzzy, evidence) for 10th/12th titles; exact patterns win over fuzzy ones."""
    if re.search(HIGH_SCHOOL_10TH, header_compact):
        return SSC, False, f"exact:{HIGH_SCHOOL_10TH}"
    if p := _first(header_compact, HSC_PATTERNS):
        return HSC, False, f"exact:{p}"
    if p := _first(header_compact, SSC_PATTERNS):
        return SSC, False, f"exact:{p}"
    h = max(best_ratio(header_compact, t) for t in HSC_TITLES)
    s = max(best_ratio(header_compact, t) for t in SSC_TITLES)
    if max(h, s) < FUZZY_MIN or abs(h - s) < FUZZY_MARGIN:
        return None, False, ""
    if h > s:
        return HSC, True, f"fuzzy:hsc_title:{h:.2f}"
    return SSC, True, f"fuzzy:ssc_title:{s:.2f}"


def classification_for(
    code: str, text: str, words: list, confidence: float, evidence: list[str]
) -> Classification:
    """A classification for a type decided elsewhere (model, admin phrase, verifier), with the
    same semester and level details the rules would add."""
    full = norm(text)
    level = AcademicLevel.SCHOOL if code in (SSC, HSC, DIPLOMA) else None
    if code in (SEMESTER, PROVISIONAL, DEGREE):
        level = AcademicLevel.PG if _first(full, PG_PATTERNS) else AcademicLevel.UG
    sems = find_semesters(header_text(words), full) if code in (SEMESTER, DIPLOMA) else []
    return Classification(
        type_code=code, confidence=confidence, semesters=sems, level=level, evidence=evidence
    )


def classify_text(text: str, words: list) -> Classification:
    full = norm(text)
    header = header_text(words)
    signals = [p for p in MARKSHEET_PATTERNS if re.search(p, full)]
    n = len(signals)

    def done(code: str, evidence: str, fuzzy: bool = False) -> Classification:
        level = AcademicLevel.SCHOOL if code in (SSC, HSC, DIPLOMA) else None
        if code in (SEMESTER, PROVISIONAL, DEGREE):
            level = AcademicLevel.PG if _first(full, PG_PATTERNS) else AcademicLevel.UG
        sems = find_semesters(header, full) if code in (SEMESTER, DIPLOMA) else []
        return Classification(
            type_code=code,
            fuzzy=fuzzy,
            confidence=0.7 if fuzzy else 0.9,
            semesters=sems,
            level=level,
            evidence=[evidence, f"marksheet_signals:{n}"],
        )

    if len(full.split()) < BACKSIDE_MAX_WORDS and (p := _first(full, BACKSIDE_PATTERNS)):
        return done(SUPPORTING, f"exact:{p}")
    code, fuzzy, why = school_level(compact(header))
    if code:
        return done(code, why, fuzzy)
    if p := _first(header, DIPLOMA_PATTERNS):
        return done(DIPLOMA, f"exact:{p}")
    if n >= MARKSHEET_MIN_SIGNALS and (p := _first(full, DIPLOMA_PATTERNS[:2])):
        return done(DIPLOMA, f"exact:{p}")
    if n <= PROVISIONAL_MAX_SIGNALS and (p := _first(full, PROVISIONAL_STRONG)):
        return done(PROVISIONAL, f"exact:{p}")
    if n <= DEGREE_MAX_SIGNALS and (p := _first(full, DEGREE_PATTERNS)):
        return done(DEGREE, f"exact:{p}")
    if n <= PROVISIONAL_MAX_SIGNALS and (p := _first(full, PROVISIONAL_PATTERNS)):
        return done(PROVISIONAL, f"exact:{p}")
    if n >= MARKSHEET_MIN_SIGNALS:
        return done(SEMESTER, f"signals:{','.join(signals[:4])}")
    return Classification(type_code=None, evidence=[f"marksheet_signals:{n}"])
