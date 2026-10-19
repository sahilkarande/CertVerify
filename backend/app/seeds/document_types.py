"""Initial document-type registry, based on the documents seen in the seed dataset
(see docs/DATA_ANALYSIS.md). Field lists describe *what* to extract; *how* comes in Phases 6–7."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DocumentType
from app.models.enums import DocumentCategory


def _f(key: str, label: str, type_: str = "string", required: bool = True) -> dict[str, Any]:
    return {"key": key, "label": label, "type": type_, "required": required}


_SCHOOL_FIELDS = [
    _f("candidate_name", "Candidate name"),
    _f("mother_name", "Mother's name", required=False),
    _f("board", "Board"),
    _f("seat_number", "Seat number"),
    _f("exam_month_year", "Month & year of exam"),
    _f("total_marks_obtained", "Total marks obtained", "number"),
    _f("total_marks_max", "Total maximum marks", "number"),
    _f("percentage", "Percentage", "number"),
    _f("result", "Result"),
]

_DEGREE_FIELDS = [
    _f("candidate_name", "Candidate name"),
    _f("mother_name", "Mother's name", required=False),
    _f("university", "University"),
    _f("institute", "Institute / college", required=False),
    _f("degree", "Degree"),
    _f("branch", "Branch / specialisation", required=False),
    _f("exam_session", "Examination session"),
    _f("class_awarded", "Class / division", required=False),
    _f("cgpa", "CGPA / CPI", "number", required=False),
]

DOCUMENT_TYPES: list[dict[str, Any]] = [
    {
        "code": "ssc_marksheet",
        "name": "10th / SSC statement of marks",
        "category": DocumentCategory.ACADEMIC_RECORD,
        "field_schema": _SCHOOL_FIELDS,
    },
    {
        "code": "hsc_marksheet",
        "name": "12th / HSC statement of marks",
        "category": DocumentCategory.ACADEMIC_RECORD,
        "field_schema": [*_SCHOOL_FIELDS, _f("stream", "Stream", required=False)],
    },
    {
        "code": "diploma_marksheet",
        "name": "Diploma marksheet",
        "category": DocumentCategory.ACADEMIC_RECORD,
        "field_schema": [
            _f("candidate_name", "Candidate name"),
            _f("board", "Board / university"),
            _f("program", "Diploma programme"),
            _f("exam_month_year", "Month & year of exam"),
            _f("percentage", "Percentage", "number", required=False),
            _f("result", "Result"),
        ],
    },
    {
        "code": "semester_marksheet",
        "name": "Degree semester marksheet / grade report",
        "category": DocumentCategory.ACADEMIC_RECORD,
        "field_schema": [
            _f("candidate_name", "Candidate name"),
            _f("mother_name", "Mother's name", required=False),
            _f("university", "University"),
            _f("institute", "Institute / college", required=False),
            _f("program", "Programme / branch"),
            _f("student_id", "PRN / GR / enrolment number"),
            _f("semester", "Semester number", "integer"),
            _f("exam_session", "Examination session"),
            _f("sgpa", "SGPA / SPI", "number", required=False),
            _f("cgpa", "CGPA / CPI", "number", required=False),
            _f("result", "Result", required=False),
        ],
    },
    {
        "code": "provisional_degree_certificate",
        "name": "Provisional degree certificate",
        "category": DocumentCategory.ACADEMIC_RECORD,
        "field_schema": _DEGREE_FIELDS,
    },
    {
        "code": "degree_certificate",
        "name": "Degree certificate",
        "category": DocumentCategory.ACADEMIC_RECORD,
        "field_schema": _DEGREE_FIELDS,
    },
    {
        "code": "passport_photo",
        "name": "Passport-size photograph",
        "category": DocumentCategory.IDENTITY_IMAGE,
        "field_schema": [],
    },
    {
        "code": "signature",
        "name": "Signature",
        "category": DocumentCategory.IDENTITY_IMAGE,
        "field_schema": [],
    },
]


def seed_document_types(db: Session) -> tuple[int, int]:
    """Insert missing types and update existing ones by code. Returns (created, updated)."""
    existing = {t.code: t for t in db.scalars(select(DocumentType))}
    created = updated = 0
    for spec in DOCUMENT_TYPES:
        row = existing.get(spec["code"])
        if row is None:
            db.add(DocumentType(**spec))
            created += 1
            continue
        changed = False
        for attr in ("name", "category", "field_schema"):
            if getattr(row, attr) != spec[attr]:
                setattr(row, attr, spec[attr])
                changed = True
        updated += changed
    db.flush()
    return created, updated
