"""Testing Lab: batches of test students processed like real uploads, kept apart from real
applicants, measured against the baseline, and deleted as a whole."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import load_storage_key
from app.db.session import Database
from app.jobs.pipeline import Engines
from app.jobs.queue import claim, enqueue_upload
from app.jobs.worker import Worker
from app.models import (
    Applicant,
    Document,
    ExtractedField,
    Job,
    RequirementItem,
    StoredFile,
    Submission,
    TestRun,
    ValidationIssue,
)
from app.models.enums import ApplicantSource, UserRole
from app.seeds.document_types import seed_document_types
from app.services.files import store_file
from app.storage import EncryptedLocalStorage
from tests.auth_helpers import CSRF, make_user, sign_in
from tests.factories import make_institution, make_requirement_set
from tests.test_jobs import ScriptedOcr, jpeg

A = "/api/v1/admin/test-runs"


@pytest.fixture
def storage(test_settings: Settings) -> EncryptedLocalStorage:
    return EncryptedLocalStorage(test_settings.storage_dir, load_storage_key(test_settings))


@pytest.fixture
def lab(client: TestClient, session: Session):
    seed_document_types(session)
    inst = make_institution(session)
    rs = make_requirement_set(session, inst)
    for pos, (key, types) in enumerate(
        [("photo", ["passport_photo"]), ("signature", ["signature"]),
         ("degree_or_fallback", ["degree_certificate", "semester_marksheet"]),
         ("semester_marksheets", ["semester_marksheet"])], start=5,
    ):  # fmt: skip
        rs.items.append(
            RequirementItem(key=key, label=key, position=pos, accepted_type_codes=types)
        )
    make_user(session, "platform@c.test", UserRole.PLATFORM_ADMIN)
    session.commit()
    sign_in(client, "platform@c.test")
    return {"inst": inst, "rs": rs}


def _worker(database: Database, storage) -> Worker:
    return Worker(database, storage, Engines(rotation=ScriptedOcr("tesseract")), "w")


def _new_run_with_student(client: TestClient, name: str = "Pune batch") -> tuple[str, str]:
    run = client.post(A, json={"name": name}, headers=CSRF)
    assert run.status_code == 201, run.text
    run_id = run.json()["id"]
    sub = client.post(f"{A}/{run_id}/students", json={"name": "Asha Ramesh Patil"}, headers=CSRF)
    assert sub.status_code == 201, sub.text
    return run_id, sub.json()["id"]


def _upload(client, run_id: str, sub_id: str, data: bytes, slot: str = "ssc", name="scan.jpg"):
    r = client.post(
        f"{A}/{run_id}/students/{sub_id}/documents",
        headers=CSRF,
        data={"slot": slot},
        files={"file": (name, data, "image/jpeg")},
    )
    assert r.status_code == 202, r.text
    return r.json()["document_id"]


def _metric(m: dict, key: str) -> dict:
    return next(x for s in m["sections"] for x in s["metrics"] if x["key"] == key)


def test_a_run_processes_documents_and_reports_metrics(
    client: TestClient, session: Session, database: Database, storage, lab
) -> None:
    run_id, sub_id = _new_run_with_student(client)
    _upload(client, run_id, sub_id, jpeg())
    worker = _worker(database, storage)
    while worker.run_once():
        pass
    r = client.get(f"{A}/{run_id}")
    assert r.status_code == 200
    body = r.json()
    [student] = body["students"]
    assert student["name"] == "Asha Ramesh Patil" and student["pages"] == 1
    assert student["processing"] == 0
    m = body["metrics"]
    assert _metric(m, "students")["value"] == 1 and _metric(m, "pages")["value"] == 1
    assert _metric(m, "recognised")["value"] == 100.0
    assert _metric(m, "found_name")["value"] == 100.0
    assert m["document_types"] == {"Degree semester marksheet / grade report": 1}
    a = session.scalar(select(Applicant).where(Applicant.full_name == "Asha Ramesh Patil"))
    assert a.source is ApplicantSource.TEST and a.email is None and a.user_id is None


def test_test_students_stay_out_of_the_real_world(
    client: TestClient, session: Session, database: Database, storage, lab
) -> None:
    # A real applicant with a document...
    real = Applicant(institution_id=lab["inst"].id, full_name="Real Person",
                     source=ApplicantSource.FORM_IMPORT, external_ref="1")  # fmt: skip
    session.add(real)
    session.flush()
    real_sub = Submission(applicant_id=real.id, requirement_set_id=lab["rs"].id)
    session.add(real_sub)
    session.flush()
    f = store_file(session, storage, jpeg(), mime_type="image/jpeg")
    session.add(Document(submission_id=real_sub.id, file_id=f.id, slot_key="ssc"))
    session.commit()
    # ...and a test copy of the very same file.
    run_id, sub_id = _new_run_with_student(client)
    _upload(client, run_id, sub_id, jpeg())
    worker = _worker(database, storage)
    while worker.run_once():
        pass

    listed = client.get("/api/v1/review/submissions").json()
    assert [s["name"] for s in listed] == ["Real Person"]  # not in the verifier queue
    session.expire_all()
    shared = session.scalars(
        select(ValidationIssue).where(ValidationIssue.code == "file_shared_with_other_applicant")
    ).all()
    assert shared == []  # the test copy does not make the real student look suspicious
    baseline = client.get(f"{A}/metrics/baseline").json()
    assert _metric(baseline, "students")["value"] == 1  # the baseline is real students only


def test_real_uploads_go_before_test_uploads(
    client: TestClient, session: Session, storage, lab
) -> None:
    run_id, sub_id = _new_run_with_student(client)
    _upload(client, run_id, sub_id, jpeg())  # the test upload comes first...
    real = Applicant(institution_id=lab["inst"].id, full_name="Real", source=ApplicantSource.PORTAL)
    session.add(real)
    session.flush()
    sub = Submission(applicant_id=real.id, requirement_set_id=lab["rs"].id)
    session.add(sub)
    session.flush()
    f = store_file(session, storage, jpeg(w=1200), mime_type="image/jpeg")
    doc = Document(submission_id=sub.id, file_id=f.id, slot_key="ssc")
    session.add(doc)
    session.flush()
    real_job = enqueue_upload(session, doc.id)
    session.commit()
    assert claim(session, "w").id == real_job.id  # ...but the real one is checked first


def test_accuracy_comes_from_reviews(
    client: TestClient, session: Session, database: Database, storage, lab
) -> None:
    run_id, sub_id = _new_run_with_student(client)
    _upload(client, run_id, sub_id, jpeg())
    worker = _worker(database, storage)
    while worker.run_once():
        pass
    fields = {
        f.field_key: f
        for f in session.scalars(select(ExtractedField).where(ExtractedField.value.is_not(None)))
    }
    review = "/api/v1/review"
    name, sgpa = fields["candidate_name"], fields["sgpa"]
    # Confirm the name as read, correct the SGPA, and change the page's type.
    r = client.patch(f"{review}/fields/{name.id}", json={"value": name.value,
                     "expected_value": name.value}, headers=CSRF)  # fmt: skip
    assert r.status_code == 200, r.text
    r = client.patch(f"{review}/fields/{sgpa.id}", json={"value": "8.27",
                     "expected_value": sgpa.value}, headers=CSRF)  # fmt: skip
    assert r.status_code == 200, r.text
    r = client.post(f"{review}/pages/{name.page_id}/type", json={"type": "ssc_marksheet"},
                    headers=CSRF)  # fmt: skip
    assert r.status_code == 200, r.text

    m = client.get(f"{A}/{run_id}").json()["metrics"]
    assert _metric(m, "acc_candidate_name")["value"] == 100.0
    assert _metric(m, "acc_sgpa")["value"] == 0.0
    assert (_metric(m, "acc_all")["value"], _metric(m, "acc_all")["n"]) == (50.0, 2)
    assert (_metric(m, "acc_type")["value"], _metric(m, "acc_type")["n"]) == (0.0, 1)
    assert _metric(m, "reviewed_pages")["value"] == 1
    [student] = client.get(f"{A}/{run_id}").json()["students"]
    assert student["reviewed_pages"] == 1


def test_deleting_a_run_removes_everything(
    client: TestClient, session: Session, database: Database, storage, lab
) -> None:
    run_id, sub_id = _new_run_with_student(client)
    _upload(client, run_id, sub_id, jpeg())
    worker = _worker(database, storage)
    while worker.run_once():
        pass
    keys = list(session.scalars(select(StoredFile.object_key)))
    assert keys
    url = f"{A}/{run_id}/delete"
    assert client.post(url, json={"confirm": "pune batch"}, headers=CSRF).status_code == 422
    r = client.post(url, json={"confirm": "Pune batch"}, headers=CSRF)
    assert r.status_code == 200, r.text
    assert r.json()["submissions"] == 1 and r.json()["files_removed"] == len(keys)
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(TestRun)) == 0
    assert session.scalar(select(func.count()).select_from(Applicant)) == 0
    assert session.scalar(select(func.count()).select_from(Job)) == 0
    assert not any(storage.exists(k) for k in keys)


def test_only_platform_admins_use_the_lab(client: TestClient, session: Session, lab) -> None:
    run_id, _ = _new_run_with_student(client)
    make_user(session, "inst-admin@c.test", UserRole.INSTITUTION_ADMIN, lab["inst"].id)
    make_user(session, "verifier@c.test", UserRole.VERIFIER, lab["inst"].id)
    session.commit()
    for email in ("inst-admin@c.test", "verifier@c.test"):
        client.post("/api/v1/auth/logout", headers=CSRF)
        sign_in(client, email)
        assert client.get(A).status_code == 403
        assert client.get(f"{A}/{run_id}").status_code == 403
        assert client.post(A, json={"name": "x"}, headers=CSRF).status_code == 403


def test_metrics_export_as_csv(client: TestClient, lab) -> None:
    run_id, _ = _new_run_with_student(client)
    r = client.get(f"{A}/{run_id}/export.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.text.splitlines()
    assert lines[0] == "section,metric,unit,this run,out of,baseline,out of"
    assert any(line.startswith("Processing,Students,count,1,") for line in lines)


def test_failed_files_are_shown_and_can_be_retried(
    client: TestClient, session: Session, lab
) -> None:
    from app.models.enums import JobStatus

    run_id, sub_id = _new_run_with_student(client)
    _upload(client, run_id, sub_id, jpeg())
    job = session.scalar(select(Job))
    job.status, job.last_error = JobStatus.FAILED, "LookupError"  # e.g. a worker on old code
    session.commit()
    [student] = client.get(f"{A}/{run_id}").json()["students"]
    assert (student["failed"], student["processing"]) == (1, 0)

    r = client.post(f"{A}/{run_id}/retry", headers=CSRF)
    assert r.status_code == 200 and r.json() == {"queued": 1}
    [student] = client.get(f"{A}/{run_id}").json()["students"]
    assert (student["failed"], student["processing"]) == (0, 1)
    session.expire_all()
    retry = session.scalars(select(Job).order_by(Job.created_at.desc())).first()
    assert retry.kind == "process_document" and retry.priority == 200  # complete, still last


def test_bulk_uploads_are_sorted_into_the_checklist(
    client: TestClient, session: Session, database: Database, storage, lab
) -> None:
    run_id, sub_id = _new_run_with_student(client)
    marks = _upload(client, run_id, sub_id, jpeg(), slot="auto", name="all marks.jpg")
    photo = _upload(client, run_id, sub_id, jpeg(w=600), slot="auto", name="Passport Photo.jpg")
    sign = _upload(client, run_id, sub_id, jpeg(w=700), slot="auto", name="sign - asha.jpg")
    worker = _worker(database, storage)
    while worker.run_once():
        pass
    session.expire_all()
    slot = {d: session.get(Document, uuid.UUID(d)).slot_key for d in (marks, photo, sign)}
    # Read as a semester marksheet: its own item, not the degree item's fallback.
    assert slot[marks] == "semester_marksheets"
    assert (slot[photo], slot[sign]) == ("photo", "signature")  # by file name, not read


def test_unrecognised_bulk_files_stay_unsorted(
    client: TestClient, session: Session, database: Database, storage, lab, monkeypatch
) -> None:
    import app.jobs.pipeline as pipeline

    monkeypatch.setattr(pipeline, "classify_document", lambda *a, **k: None)  # nothing known
    run_id, sub_id = _new_run_with_student(client)
    doc = _upload(client, run_id, sub_id, jpeg(), slot="auto")
    worker = _worker(database, storage)
    while worker.run_once():
        pass
    session.expire_all()
    assert session.get(Document, uuid.UUID(doc)).slot_key == "auto"


def test_only_test_students_may_upload_unsorted(client: TestClient, session: Session, lab):
    from fastapi import HTTPException

    from app.portal.uploads import add_document

    real = Applicant(institution_id=lab["inst"].id, full_name="Real", source=ApplicantSource.PORTAL)
    session.add(real)
    session.flush()
    sub = Submission(applicant_id=real.id, requirement_set_id=lab["rs"].id)
    session.add(sub)
    session.flush()
    with pytest.raises(HTTPException) as e:
        add_document(session, None, sub, "auto", jpeg(), "x.jpg", uuid.uuid4())
    assert e.value.status_code == 422
