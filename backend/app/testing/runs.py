"""Testing Lab runs: a named batch of test students, processed like real uploads (behind
them in the queue), never mixed with real applicants, and deleted as a whole."""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.admin.erasure import Erasure, erase_applicant
from app.db.base import iso_utc
from app.jobs.queue import latest_job
from app.models import (
    Applicant,
    Document,
    DocumentPage,
    ExtractedField,
    RequirementSet,
    Submission,
    TestRun,
    User,
    ValidationIssue,
)
from app.models.enums import (
    ApplicantSource,
    ClassificationMethod,
    FieldSource,
    IssueStatus,
    JobStatus,
    SubmissionStatus,
)
from app.services.audit import record_event
from app.storage import ObjectStorage
from app.validate.engine import validate_submission, verdict


def create_run(
    db: Session, user: User, name: str, notes: str | None, requirement_set_id: uuid.UUID | None
) -> TestRun:
    rs = (
        db.get(RequirementSet, requirement_set_id)
        if requirement_set_id
        else db.scalar(
            select(RequirementSet)
            .where(RequirementSet.is_active.is_(True))
            .order_by(RequirementSet.name)
            .limit(1)
        )
    )
    if rs is None:
        raise ValueError("no checklist to test against")
    run = TestRun(name=name.strip(), notes=(notes or "").strip() or None,
                  requirement_set_id=rs.id, created_by_id=user.id)  # fmt: skip
    db.add(run)
    db.flush()
    record_event(db, action="test_run.created", entity_type="test_run", entity_id=run.id,
                 actor_user_id=user.id)  # fmt: skip
    return run


@dataclass
class TestStudent:
    __test__ = False  # not a pytest test class

    name: str
    degree_status: str = "has_degree"
    claimed_aggregate_percent: float | None = None
    attributes: dict = field(default_factory=dict)


def add_student(db: Session, run: TestRun, s: TestStudent) -> Submission:
    """A test applicant (no account, no email) with one submission in this run."""
    rs = db.get(RequirementSet, run.requirement_set_id)
    n = db.scalar(select(func.count()).where(Submission.test_run_id == run.id)) or 0
    applicant = Applicant(
        institution_id=rs.institution_id,
        full_name=" ".join(s.name.split()) or f"Test student {n + 1}",
        source=ApplicantSource.TEST,
    )
    db.add(applicant)
    db.flush()
    attrs = {"degree_status": s.degree_status}
    if s.claimed_aggregate_percent is not None:
        attrs["claimed_aggregate_percent"] = s.claimed_aggregate_percent
    sub = Submission(
        applicant_id=applicant.id,
        requirement_set_id=rs.id,
        test_run_id=run.id,
        status=SubmissionStatus.SUBMITTED,
        attributes=attrs,
    )
    db.add(sub)
    db.flush()
    validate_submission(db, sub)
    return sub


def delete_run(db: Session, run: TestRun, actor: User, key: bytes) -> Erasure:
    """Every test student (files included) and the run. Caller commits, then purges."""
    total = Erasure()
    applicants = db.scalars(
        select(Applicant)
        .join(Submission, Submission.applicant_id == Applicant.id)
        .where(Submission.test_run_id == run.id)
    ).all()
    for a in applicants:
        out = erase_applicant(db, a, actor, key)
        total.submissions += out.submissions
        total.documents += out.documents
        total._keys += out._keys
    record_event(db, action="test_run.deleted", entity_type="test_run", entity_id=run.id,
                 actor_user_id=actor.id, details=total.counts())  # fmt: skip
    db.delete(db.get(TestRun, run.id))
    db.flush()
    return total


def run_submissions(db: Session, run_id: uuid.UUID) -> list[uuid.UUID]:
    return list(db.scalars(select(Submission.id).where(Submission.test_run_id == run_id)))


def students_json(db: Session, run: TestRun) -> list[dict]:
    out = []
    subs = db.execute(
        select(Submission, Applicant)
        .join(Applicant, Applicant.id == Submission.applicant_id)
        .where(Submission.test_run_id == run.id)
        .order_by(Submission.created_at)
    ).all()
    for sub, a in subs:
        docs = db.scalars(select(Document).where(Document.submission_id == sub.id)).all()
        pages = [p for d in docs for p in d.pages]
        open_sev = list(
            db.scalars(
                select(ValidationIssue.severity).where(
                    ValidationIssue.submission_id == sub.id,
                    ValidationIssue.status == IssueStatus.OPEN,
                )
            )
        )
        reviewed = {
            pid
            for pid in db.scalars(
                select(ExtractedField.page_id).where(
                    ExtractedField.document_id.in_([d.id for d in docs]),
                    ExtractedField.source == FieldSource.HUMAN,
                    ExtractedField.corrected_by_id.is_not(None),
                )
            )
        } | {p.id for p in pages if p.classification_method == ClassificationMethod.HUMAN}
        processing = failed = 0
        for d in docs:
            job = latest_job(db, d.id)
            processing += job is not None and job.status in (JobStatus.QUEUED, JobStatus.RUNNING)
            failed += job is not None and job.status == JobStatus.FAILED
        out.append(
            {
                "id": str(sub.id),
                "name": a.full_name,
                "documents": len(docs),
                "pages": len(pages),
                "processing": processing,
                "failed": failed,  # files whose processing gave up: see "Retry"
                "verdict": verdict(open_sev),
                "reviewed_pages": len(reviewed),
                "created_at": iso_utc(sub.created_at),
            }
        )
    return out


def run_summary(db: Session, run: TestRun) -> dict:
    subs = run_submissions(db, run.id)
    pages = (
        db.scalar(
            select(func.count())
            .select_from(DocumentPage)
            .join(Document, Document.id == DocumentPage.document_id)
            .where(Document.submission_id.in_(subs))
        )
        if subs
        else 0
    )
    creator = db.get(User, run.created_by_id) if run.created_by_id else None
    return {
        "id": str(run.id),
        "name": run.name,
        "notes": run.notes,
        "created_at": iso_utc(run.created_at),
        "created_by": creator.full_name if creator else None,
        "students": len(subs),
        "pages": pages,
    }


def storage_purge(out: Erasure, storage: ObjectStorage) -> int:
    return out.purge_files(storage)
