"""Administration API. Document types and the Testing Lab: platform admins. Checklists, and
deleting student data on request: institution admins (their own institution) and platform
admins."""

import csv
import io
import time
import uuid
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.admin import erasure, registry
from app.api.deps import get_db_session, get_storage, require_staff
from app.api.routes.review import PRIVATE
from app.api.routes.review import _check_scope as check_scope
from app.api.routes.review import _sub as review_sub
from app.jobs.queue import retry_failed
from app.models import (
    Applicant,
    Document,
    DocumentType,
    RequirementItem,
    RequirementSet,
    Submission,
    TestRun,
    User,
)
from app.models.enums import UserRole
from app.portal.application import DEGREE_STATUSES
from app.portal.uploads import add_document, read_capped
from app.services.names import normalize_name
from app.storage import ObjectStorage
from app.testing import metrics as testing_metrics
from app.testing import runs

router = APIRouter(prefix="/admin")
DbSession = Annotated[Session, Depends(get_db_session)]
Storage = Annotated[ObjectStorage, Depends(get_storage)]
PlatformAdmin = Annotated[User, Depends(require_staff(UserRole.PLATFORM_ADMIN))]
Admin = Annotated[User, Depends(require_staff(UserRole.INSTITUTION_ADMIN, UserRole.PLATFORM_ADMIN))]


class FieldSpec(BaseModel):
    key: str = Field(max_length=63)
    label: str | None = Field(default=None, max_length=80)
    type: str | None = None
    required: bool = False


class NewType(BaseModel):
    code: str = Field(max_length=63)
    name: str = Field(max_length=255)
    category: str = "academic_record"
    fields: list[FieldSpec] = []
    title_phrases: list[str] = []


class TypeChange(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    title_phrases: list[str] | None = None
    fields: list[FieldSpec] | None = None
    active: bool | None = None


class ItemChange(BaseModel):
    label: str | None = Field(default=None, max_length=255)
    required: bool | None = None
    accepted_types: list[str] | None = None


class NewItem(BaseModel):
    key: str = Field(max_length=63)
    label: str = Field(max_length=255)
    accepted_types: list[str]
    required: bool = True


@router.get("/document-types")
def list_types(user: Admin, db: DbSession) -> dict:
    return {
        "types": registry.types_json(db),
        "extractable": {k: label for k, (label, _) in registry.EXTRACTABLE.items()},
    }


@router.post("/document-types", status_code=status.HTTP_201_CREATED)
def create_type(body: NewType, user: PlatformAdmin, db: DbSession) -> dict:
    t = registry.create_type(
        db,
        user,
        body.code,
        body.name,
        body.category,
        [f.model_dump() for f in body.fields],
        body.title_phrases,
    )
    db.commit()
    return {"id": str(t.id), "code": t.code}


@router.patch("/document-types/{type_id}")
def update_type(type_id: uuid.UUID, body: TypeChange, user: PlatformAdmin, db: DbSession) -> dict:
    t = db.get(DocumentType, type_id)
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    registry.update_type(
        db,
        user,
        t,
        name=body.name,
        phrases=body.title_phrases,
        fields=[f.model_dump() for f in body.fields] if body.fields is not None else None,
        active=body.active,
    )
    db.commit()
    return {"id": str(t.id)}


def _scope(user: User):
    if user.role == UserRole.PLATFORM_ADMIN:
        return None
    if user.institution_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your account has no institution.")
    return user.institution_id


@router.get("/requirement-sets")
def requirement_sets(user: Admin, db: DbSession) -> dict:
    return {"requirement_sets": registry.requirement_sets_json(db, _scope(user))}


def _own_set(db: Session, user: User, set_id: uuid.UUID) -> RequirementSet:
    rs = db.get(RequirementSet, set_id)
    inst = _scope(user)
    if rs is None or (inst is not None and rs.institution_id != inst):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return rs


@router.patch("/requirement-items/{item_id}")
def update_item(item_id: uuid.UUID, body: ItemChange, user: Admin, db: DbSession) -> dict:
    item = db.get(RequirementItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    _own_set(db, user, item.requirement_set_id)
    registry.update_item(
        db, user, item, label=body.label, required=body.required, accepted=body.accepted_types
    )
    db.commit()
    return {"id": str(item.id)}


@router.post("/requirement-sets/{set_id}/items", status_code=status.HTTP_201_CREATED)
def add_item(set_id: uuid.UUID, body: NewItem, user: Admin, db: DbSession) -> dict:
    rs = _own_set(db, user, set_id)
    item = registry.add_item(db, user, rs, body.key, body.label, body.accepted_types, body.required)
    db.commit()
    return {"id": str(item.id)}


# ---------- deleting student data on request ----------


class ConfirmDelete(BaseModel):
    confirm: str = Field(max_length=255)  # the student's full name, typed by the admin


def _confirmed(applicant: Applicant, typed: str) -> None:
    """Irreversible: the admin types the student's full name, so the wrong record (or a
    mis-click) cannot be deleted."""
    if not typed.strip() or normalize_name(typed) != normalize_name(applicant.full_name or ""):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Type the student's full name exactly as shown to confirm.",
        )


@router.post("/submissions/{submission_id}/delete")
def delete_application(
    submission_id: uuid.UUID, body: ConfirmDelete, user: Admin, db: DbSession, storage: Storage
) -> dict:
    """One application with its documents and files; the person and account stay."""
    sub = review_sub(db, user, submission_id)
    _confirmed(sub.applicant, body.confirm)
    out = erasure.delete_application(db, sub, user)
    db.commit()
    return {**out.counts(), "files_removed": out.purge_files(storage)}


@router.post("/applicants/{applicant_id}/erase")
def erase_applicant(
    applicant_id: uuid.UUID,
    body: ConfirmDelete,
    user: Admin,
    db: DbSession,
    storage: Storage,
    request: Request,
) -> dict:
    """Everything about the person: applications, files, name, email, phone, login."""
    applicant = db.get(Applicant, applicant_id)
    if applicant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    check_scope(user, applicant.institution_id)
    _confirmed(applicant, body.confirm)
    out = erasure.erase_applicant(db, applicant, user, request.app.state.erasure_key)
    db.commit()
    return {**out.counts(), "files_removed": out.purge_files(storage)}


# ---------- Testing Lab (platform admins) ----------

BASELINE_TTL_S = 120
_baseline_cache: dict = {}


class NewRun(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    notes: str | None = Field(default=None, max_length=4000)
    requirement_set_id: uuid.UUID | None = None


class NewTestStudent(BaseModel):
    name: str = Field(default="", max_length=255)
    degree_status: str = Field(default="has_degree", max_length=64)
    claimed_aggregate_percent: float | None = Field(default=None, ge=0, le=100)


def _run(db: Session, run_id: uuid.UUID) -> TestRun:
    run = db.get(TestRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Test run not found")
    return run


def _metrics_for(db: Session, target: str) -> dict:
    """``baseline`` (real applicants, cached briefly: it changes slowly) or a run id."""
    if target == "baseline":
        hit = _baseline_cache.get("v")
        if hit and time.monotonic() - hit[0] < BASELINE_TTL_S:
            return hit[1]
        m = testing_metrics.compute(db, testing_metrics.baseline_ids(db))
        _baseline_cache["v"] = (time.monotonic(), m)
        return m
    try:
        run = _run(db, uuid.UUID(target))
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Test run not found") from exc
    return testing_metrics.compute(db, runs.run_submissions(db, run.id))


@router.get("/test-runs")
def list_runs(user: PlatformAdmin, db: DbSession) -> dict:
    all_runs = db.scalars(select(TestRun).order_by(TestRun.created_at.desc())).all()
    sets = db.scalars(select(RequirementSet).where(RequirementSet.is_active.is_(True))).all()
    return {
        "runs": [runs.run_summary(db, r) for r in all_runs],
        "checklists": [{"id": str(s.id), "name": s.name} for s in sets],
    }


@router.post("/test-runs", status_code=status.HTTP_201_CREATED)
def create_run(body: NewRun, user: PlatformAdmin, db: DbSession) -> dict:
    try:
        run = runs.create_run(db, user, body.name, body.notes, body.requirement_set_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Choose a checklist.") from exc
    db.commit()
    return runs.run_summary(db, run)


@router.get("/test-runs/{run_id}")
def get_run(run_id: uuid.UUID, user: PlatformAdmin, db: DbSession, response: Response) -> dict:
    run = _run(db, run_id)
    response.headers.update(PRIVATE)
    items = db.scalars(
        select(RequirementItem)
        .where(RequirementItem.requirement_set_id == run.requirement_set_id)
        .order_by(RequirementItem.position)
    ).all()
    return {
        "run": runs.run_summary(db, run),
        "students": runs.students_json(db, run),
        "checklist": [{"key": i.key, "label": i.label} for i in items],
        "degree_statuses": DEGREE_STATUSES,
        "metrics": testing_metrics.compute(db, runs.run_submissions(db, run.id)),
    }


@router.get("/test-runs/metrics/{target}")
def metrics(target: str, user: PlatformAdmin, db: DbSession) -> dict:
    """What to compare a run with: ``baseline`` (real applicants) or another run's id."""
    return _metrics_for(db, target)


@router.post("/test-runs/{run_id}/students", status_code=status.HTTP_201_CREATED)
def add_test_student(
    run_id: uuid.UUID, body: NewTestStudent, user: PlatformAdmin, db: DbSession
) -> dict:
    run = _run(db, run_id)
    if body.degree_status not in DEGREE_STATUSES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown degree status.")
    sub = runs.add_student(db, run, runs.TestStudent(
        body.name, body.degree_status, body.claimed_aggregate_percent))  # fmt: skip
    db.commit()
    return {"id": str(sub.id)}


@router.post("/test-runs/{run_id}/students/{submission_id}/documents", status_code=202)
async def upload_test_document(
    run_id: uuid.UUID,
    submission_id: uuid.UUID,
    user: PlatformAdmin,
    db: DbSession,
    storage: Storage,
    slot: Annotated[str, Form(max_length=64)],
    file: Annotated[UploadFile, File()],
) -> dict:
    sub = db.get(Submission, submission_id)
    if sub is None or sub.test_run_id != run_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Test student not found")
    data = await read_capped(file)
    doc, job_id = add_document(db, storage, sub, slot, data, file.filename, user.id)
    db.commit()
    return {"document_id": str(doc.id), "job_id": str(job_id)}


@router.post("/test-runs/{run_id}/retry")
def retry_run(run_id: uuid.UUID, user: PlatformAdmin, db: DbSession) -> dict:
    """Process again this run's files whose processing failed."""
    run = _run(db, run_id)
    docs = db.scalars(
        select(Document.id)
        .join(Submission, Submission.id == Document.submission_id)
        .where(Submission.test_run_id == run.id)
    ).all()
    n = retry_failed(db, docs)
    db.commit()
    return {"queued": n}


class ConfirmRunDelete(BaseModel):
    confirm: str = Field(max_length=255)  # the run's name


@router.post("/test-runs/{run_id}/delete")
def delete_run(
    run_id: uuid.UUID,
    body: ConfirmRunDelete,
    user: PlatformAdmin,
    db: DbSession,
    storage: Storage,
    request: Request,
) -> dict:
    run = _run(db, run_id)
    if body.confirm.strip() != run.name:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Type the test run's name to confirm."
        )
    out = runs.delete_run(db, run, user, request.app.state.erasure_key)
    db.commit()
    return {**out.counts(), "files_removed": out.purge_files(storage)}


@router.get("/test-runs/{run_id}/export.csv")
def export_csv(run_id: uuid.UUID, user: PlatformAdmin, db: DbSession) -> Response:
    """This run's metrics next to the baseline, for a spreadsheet."""
    run = _run(db, run_id)
    mine = testing_metrics.compute(db, runs.run_submissions(db, run.id))
    base = _metrics_for(db, "baseline")
    base_by_key = {m["key"]: m for s in base["sections"] for m in s["metrics"]}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["section", "metric", "unit", "this run", "out of", "baseline", "out of"])
    for s in mine["sections"]:
        for m in s["metrics"]:
            b = base_by_key.get(m["key"], {})
            w.writerow([s["title"], m["label"], m["unit"], m["value"], m["n"],
                        b.get("value"), b.get("n")])  # fmt: skip
    safe = "".join(c if c.isalnum() else "-" for c in run.name)[:60] or "test-run"
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={**PRIVATE, "Content-Disposition": f'attachment; filename="{safe}-metrics.csv"'},
    )
