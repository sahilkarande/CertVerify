"""Minimal helpers to build a valid object graph in tests."""

from sqlalchemy.orm import Session

from app.models import (
    Applicant,
    Document,
    Institution,
    RequirementItem,
    RequirementSet,
    StoredFile,
    Submission,
)
from app.models.enums import ApplicantSource


def make_institution(db: Session, slug: str = "cdac-kharghar") -> Institution:
    inst = Institution(name="C-DAC Kharghar", slug=slug)
    db.add(inst)
    db.flush()
    return inst


def make_requirement_set(db: Session, inst: Institution) -> RequirementSet:
    rs = RequirementSet(
        institution_id=inst.id,
        code="pg-feb-2026",
        name="PG admission Feb 2026",
        items=[
            RequirementItem(key="ssc", label="10th", accepted_type_codes=["ssc_marksheet"]),
            RequirementItem(
                key="hsc_or_diploma",
                label="12th or Diploma",
                position=1,
                accepted_type_codes=["hsc_marksheet", "diploma_marksheet"],
            ),
        ],
    )
    db.add(rs)
    db.flush()
    return rs


def make_submission(db: Session, ref: str = "100000001") -> Submission:
    inst = make_institution(db, slug=f"inst-{ref}")
    rs = make_requirement_set(db, inst)
    applicant = Applicant(
        institution_id=inst.id,
        external_ref=ref,
        full_name="Test Student",
        source=ApplicantSource.FORM_IMPORT,
    )
    db.add(applicant)
    db.flush()
    sub = Submission(applicant_id=applicant.id, requirement_set_id=rs.id)
    db.add(sub)
    db.flush()
    return sub


def make_stored_file(db: Session, sha: str = "a" * 64, key: str | None = None) -> StoredFile:
    f = StoredFile(
        object_key=key or sha[:32],
        sha256=sha,
        size_bytes=10,
        mime_type="application/pdf",
    )
    db.add(f)
    db.flush()
    return f


def make_document(db: Session, sub: Submission, f: StoredFile) -> Document:
    doc = Document(submission_id=sub.id, file_id=f.id, slot_key="ssc")
    db.add(doc)
    db.flush()
    return doc
