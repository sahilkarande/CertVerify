import io
from datetime import timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import load_storage_key
from app.db.base import utcnow
from app.db.session import Database
from app.jobs.pipeline import Engines
from app.jobs.queue import (
    CHECK_DOCUMENT,
    CHECK_PRIORITY,
    LEASE,
    READ_DOCUMENT,
    UPLOAD_PRIORITY,
    claim,
    enqueue_document,
    enqueue_upload,
    fail,
    finish,
    latest_job,
    progress,
    try_claim,
)
from app.jobs.worker import Worker
from app.models import (
    AuditEvent,
    Document,
    DocumentType,
    ExtractedField,
    Job,
    PageOcr,
    ValidationIssue,
)
from app.models.enums import DocumentStatus, JobStage, JobStatus, Readability, UserRole
from app.ocr.engine import OcrResult, Word
from app.seeds.document_types import seed_document_types
from app.storage import EncryptedLocalStorage
from tests.auth_helpers import CSRF, make_user, sign_in
from tests.factories import make_institution, make_stored_file, make_submission

LINES = [
    "STATEMENT OF GRADES SEMESTER : III",
    "NAME : PATIL ASHA RAMESH",
    "SEAT NO : B190244471",
    *[f"row {i} subject marks credits" for i in range(12)],
    "SGPA : 8.72",
]


class ScriptedOcr:
    """Stands in for Tesseract/PaddleOCR: always 'reads' the same semester marksheet."""

    def __init__(self, name: str, osd=(0, 9.0), weak_lines=()) -> None:
        self.name, self.version, self.reads = name, "test", 0
        self.osd = osd  # orientation answer (None: cannot tell)
        self.weak_lines = set(weak_lines)  # line numbers read with low confidence

    def prepare(self, rgb):
        return rgb

    def orientation(self, image):
        return self.osd

    def read(self, image) -> OcrResult:
        self.reads += 1
        words = [
            Word(w, 70 if i in self.weak_lines else 95, j * 60, i * 40, 50, 20, f"l{i}")
            for i, line in enumerate(LINES)
            for j, w in enumerate(line.split())
        ]
        return OcrResult(words)


class Exploding(ScriptedOcr):
    def read(self, image):
        raise RuntimeError("engine died")


def test_an_ocr_engine_crash_marks_the_page_and_processing_continues(
    session: Session, database: Database, storage
) -> None:
    doc = _doc(session, storage)
    job = enqueue_document(session, doc.id)
    session.commit()
    Worker(database, storage, Engines(rotation=Exploding("tesseract")), "w").run_once()
    session.expire_all()
    assert session.get(Job, job.id).status is JobStatus.DONE
    assert [r.status.value for r in session.scalars(select(PageOcr))] == ["failed"]


def jpeg(w: int = 1240, h: int = 1754) -> bytes:
    rng = np.random.default_rng(0)
    img = np.full((h, w, 3), 245, np.uint8)
    for y in range(150, h - 150, 40):  # lines of "text" so the page is not blank
        img[y : y + 12, 120 : w - 120] = rng.integers(0, 60, (12, w - 240, 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, "JPEG", quality=90)
    return buf.getvalue()


@pytest.fixture
def storage(test_settings: Settings) -> EncryptedLocalStorage:
    return EncryptedLocalStorage(test_settings.storage_dir, load_storage_key(test_settings))


def _doc(db: Session, storage, data: bytes | None = None, slot="semester_marksheets") -> Document:
    from app.services.files import store_file

    seed_document_types(db)
    sub = make_submission(db)
    sub.applicant.full_name = "Asha Ramesh Patil"
    f = store_file(db, storage, data or jpeg(), mime_type="image/jpeg")
    doc = Document(submission_id=sub.id, file_id=f.id, slot_key=slot)
    db.add(doc)
    db.commit()
    return doc


# ---------- queue ----------


def test_enqueue_reuses_a_waiting_job(session: Session) -> None:
    doc = Document(submission_id=make_submission(session).id, file_id=make_stored_file(session).id)
    session.add(doc)
    session.flush()
    a = enqueue_document(session, doc.id)
    b = enqueue_document(session, doc.id, priority=UPLOAD_PRIORITY)
    assert a is b and b.priority == UPLOAD_PRIORITY
    assert len(session.scalars(select(Job)).all()) == 1


def _jobs(session: Session, n: int) -> list[Job]:
    sub = make_submission(session)
    jobs = []
    for i in range(n):
        f = make_stored_file(session, sha=f"{i:064d}", key=f"jobs-test-{i}")
        d = Document(submission_id=sub.id, file_id=f.id)
        session.add(d)
        session.flush()
        jobs.append(enqueue_document(session, d.id))
    session.commit()
    return jobs


def test_claim_order_due_time_and_exclusivity(session: Session) -> None:
    low, urgent, later = _jobs(session, 3)
    urgent.priority = UPLOAD_PRIORITY
    later.run_after = utcnow() + timedelta(minutes=5)
    session.commit()
    assert claim(session, "w1").id == urgent.id
    assert claim(session, "w2").id == low.id
    assert claim(session, "w3") is None  # the remaining one is not due yet
    session.refresh(urgent)
    assert (urgent.status, urgent.locked_by, urgent.attempts) == (JobStatus.RUNNING, "w1", 1)


def test_losing_a_claim_race_changes_nothing(session: Session) -> None:
    [job] = _jobs(session, 1)
    assert try_claim(session, job.id, "fast-worker") is True
    assert try_claim(session, job.id, "slow-worker") is False  # saw it queued, too late
    session.refresh(job)
    assert (job.locked_by, job.attempts) == ("fast-worker", 1)


def test_expired_lease_is_taken_over(session: Session) -> None:
    [job] = _jobs(session, 1)
    claim(session, "crashed-worker")
    assert claim(session, "w2") is None  # lease still valid
    job.lease_until = utcnow() - timedelta(seconds=1)
    session.commit()
    taken = claim(session, "w2")
    assert taken.id == job.id and taken.locked_by == "w2" and taken.attempts == 2


def test_progress_renews_the_lease(session: Session) -> None:
    [job] = _jobs(session, 1)
    claim(session, "w")
    job.lease_until = utcnow()
    progress(session, job, JobStage.READING)
    assert job.lease_until > utcnow() + LEASE - timedelta(seconds=5)


def test_failures_retry_with_backoff_then_give_up(session: Session) -> None:
    [job] = _jobs(session, 1)
    for attempt in (1, 2):
        claim(session, "w")
        fail(session, job, RuntimeError("x"))
        assert (job.status, job.attempts, job.last_error) == (
            JobStatus.QUEUED,
            attempt,
            "RuntimeError",
        )
        assert job.run_after > utcnow()
        job.run_after = utcnow()
        session.commit()
    claim(session, "w")
    fail(session, job, RuntimeError("x"))
    assert job.status is JobStatus.FAILED and job.finished_at is not None
    assert claim(session, "w") is None


def test_finish(session: Session) -> None:
    [job] = _jobs(session, 1)
    claim(session, "w")
    finish(session, job)
    assert (job.status, job.stage, job.locked_by) == (JobStatus.DONE, JobStage.DONE, None)


# ---------- worker + pipeline ----------


def _worker(database: Database, storage, reader=None, tesseract=None, **kw) -> Worker:
    engines = Engines(rotation=tesseract or ScriptedOcr("tesseract"), reader=reader)
    return Worker(database, storage, engines, "test-worker", **kw)


def test_worker_runs_every_step_for_a_document(
    session: Session, database: Database, storage
) -> None:
    doc = _doc(session, storage)
    job = enqueue_document(session, doc.id)
    session.commit()
    reader, tesseract = ScriptedOcr("paddle"), ScriptedOcr("tesseract")
    assert _worker(database, storage, reader, tesseract).run_once() is True

    session.expire_all()
    job = session.get(Job, job.id)
    assert (job.status, job.stage) == (JobStatus.DONE, JobStage.DONE)
    doc = session.get(Document, doc.id)
    assert doc.status is DocumentStatus.PROCESSED and doc.page_count == 1
    engines = set(session.scalars(select(PageOcr.engine)))
    # Orientation was certain and the page read clearly: one read, by the main engine only.
    assert engines == {"paddle"} and reader.reads == 1 and tesseract.reads == 0
    sem = session.scalar(select(DocumentType).where(DocumentType.code == "semester_marksheet"))
    assert doc.pages[0].detected_type_id == sem.id
    fields = {f.field_key: f.value for f in session.scalars(select(ExtractedField))}
    assert fields["sgpa"] == "8.72" and fields["candidate_name"] == "PATIL ASHA RAMESH"
    assert session.scalars(select(ValidationIssue)).all()  # e.g. other documents missing
    assert _worker(database, storage).run_once() is False  # queue empty


def test_unsure_orientation_falls_back_to_the_rotation_search(
    session: Session, database: Database, storage
) -> None:
    doc = _doc(session, storage)
    enqueue_document(session, doc.id)
    session.commit()
    reader, tesseract = ScriptedOcr("paddle"), ScriptedOcr("tesseract", osd=(90, 0.5))
    _worker(database, storage, reader, tesseract).run_once()
    rows = {r.engine: r for r in session.scalars(select(PageOcr))}
    assert tesseract.reads >= 1 and reader.reads == 1  # the search decided; paddle read once
    assert rows["tesseract"].rotation == rows["paddle"].rotation == 0  # not OSD's unsure 90


def test_a_failed_main_reading_falls_back_to_tesseract(
    session: Session, database: Database, storage
) -> None:
    doc = _doc(session, storage)
    enqueue_document(session, doc.id)
    session.commit()
    _worker(database, storage, Exploding("paddle")).run_once()
    rows = {r.engine: r.status.value for r in session.scalars(select(PageOcr))}
    assert rows == {"paddle": "failed", "tesseract": "ok"}
    fields = {f.field_key for f in session.scalars(select(ExtractedField))}
    assert "sgpa" in fields  # the page was still read


def test_doubted_values_get_a_second_opinion(session: Session, database: Database, storage) -> None:
    doc = _doc(session, storage)
    enqueue_document(session, doc.id)
    session.commit()
    seat, sgpa = 2, len(LINES) - 1
    reader, tesseract = ScriptedOcr("paddle", weak_lines=(seat, sgpa)), ScriptedOcr("tesseract")
    _worker(database, storage, reader, tesseract).run_once()
    assert tesseract.reads == 1  # only because values on weak lines were in doubt
    page = session.get(Document, doc.id).pages[0]
    assert page.readability_details["why"] == {}  # Tesseract read both exactly: nothing asked
    assert page.readability is Readability.CLEAR


def test_upload_checks_run_before_any_waiting_reads(
    session: Session, database: Database, storage
) -> None:
    from app.services.files import store_file

    backlog = _doc(session, storage)
    enqueue_document(session, backlog.id, priority=UPLOAD_PRIORITY, kind=READ_DOCUMENT)
    f = store_file(session, storage, jpeg(w=1200), mime_type="image/jpeg")
    new = Document(submission_id=backlog.submission_id, file_id=f.id, slot_key="ssc")
    session.add(new)
    session.flush()
    check = enqueue_upload(session, new.id)
    session.commit()
    checks_only = _worker(database, storage, kinds=(CHECK_DOCUMENT,))
    assert checks_only.run_once() is True
    session.expire_all()
    assert session.get(Job, check.id).status is JobStatus.DONE
    assert session.get(Document, new.id).page_count == 1  # pages and quality known already
    assert checks_only.run_once() is False  # reads are not its job, however long they wait


def test_a_new_upload_is_checked_before_older_reads(session: Session, storage) -> None:
    doc = _doc(session, storage)
    older = enqueue_document(session, doc.id, priority=UPLOAD_PRIORITY, kind=READ_DOCUMENT)
    older.created_at = utcnow() - timedelta(hours=1)
    newer = enqueue_upload(session, doc.id)
    session.commit()
    assert claim(session, "w").id == newer.id


def test_unreadable_upload_finishes_with_an_issue_not_retries(
    session: Session, database: Database, storage
) -> None:
    doc = _doc(session, storage, data=b"%PDF-1.4 truncated nonsense")
    job = enqueue_document(session, doc.id)
    session.commit()
    _worker(database, storage).run_once()
    session.expire_all()
    assert session.get(Job, job.id).status is JobStatus.DONE  # the uploader must fix it
    assert session.get(Document, doc.id).status is DocumentStatus.FAILED
    codes = set(session.scalars(select(ValidationIssue.code)))
    assert "document_unreadable" in codes


def test_crash_is_recorded_and_retried(
    session: Session, database: Database, storage, monkeypatch
) -> None:
    import app.jobs.pipeline as pipeline

    def boom(*a, **k):
        raise RuntimeError("bug in a step")

    monkeypatch.setattr(pipeline, "classify_document", boom)
    doc = _doc(session, storage)
    job = enqueue_document(session, doc.id)
    session.commit()
    worker = _worker(database, storage)
    assert worker.run_once() is True  # the worker survives
    session.expire_all()
    job = session.get(Job, job.id)
    assert (job.status, job.last_error, job.attempts) == (JobStatus.QUEUED, "RuntimeError", 1)


# ---------- upload API ----------

API = "/api/v1/review"


@pytest.fixture
def signed_in(client: TestClient, session: Session, storage) -> dict:
    doc = _doc(session, storage)  # a submission to upload into
    sub_id = doc.submission_id
    inst = session.get(Document, doc.id).submission.applicant.institution_id
    make_user(session, "v@cdac.test", institution_id=inst)
    sign_in(client, "v@cdac.test")
    return {"sub": str(sub_id)}


def _upload(client: TestClient, sub: str, data: bytes, slot: str = "ssc", headers=CSRF):
    return client.post(
        f"{API}/submissions/{sub}/documents",
        headers=headers,
        data={"slot": slot},
        files={"file": ("my scan.jpg", data, "image/jpeg")},
    )


def test_upload_queues_processing_and_reports_status(
    client: TestClient, signed_in: dict, session: Session, database: Database, storage
) -> None:
    r = _upload(client, signed_in["sub"], jpeg())
    assert r.status_code == 202, r.text
    doc_id = r.json()["document_id"]
    status = client.get(f"{API}/documents/{doc_id}/status").json()
    assert status["job"]["stage"] == "waiting" and status["document_status"] == "uploaded"
    job = latest_job(session, Document.id.type.python_type(doc_id))
    assert (job.kind, job.priority) == (CHECK_DOCUMENT, CHECK_PRIORITY)
    event = session.scalar(select(AuditEvent).where(AuditEvent.action == "document.uploaded"))
    assert event.actor_user_id is not None and "my scan" not in str(event.details)

    _worker(database, storage).run_once()  # the quick check: pages and their quality
    status = client.get(f"{API}/documents/{doc_id}/status").json()
    assert status["job"]["finished"] is False and [p["number"] for p in status["pages"]] == [1]
    read = latest_job(session, Document.id.type.python_type(doc_id))
    assert (read.kind, read.priority) == (READ_DOCUMENT, UPLOAD_PRIORITY)
    _worker(database, storage).run_once()  # the read
    status = client.get(f"{API}/documents/{doc_id}/status").json()
    assert status["job"]["finished"] is True and status["job"]["stage"] == "done"


def test_upload_rejections(client: TestClient, signed_in: dict, monkeypatch) -> None:
    sub = signed_in["sub"]
    assert _upload(client, sub, b"MZ\x90\x00 not a document").status_code == 415
    assert _upload(client, sub, b"").status_code == 422
    assert _upload(client, sub, jpeg(), slot="no_such_slot").status_code == 422
    assert _upload(client, sub, jpeg(), headers={}).status_code == 403  # CSRF header missing
    import app.portal.uploads as uploads

    monkeypatch.setattr(uploads, "MAX_UPLOAD_BYTES", 1000)
    assert _upload(client, sub, jpeg()).status_code == 413


def test_upload_needs_staff_and_the_right_institution(
    client: TestClient, session: Session, storage
) -> None:
    doc = _doc(session, storage)
    sub = str(doc.submission_id)
    assert _upload(client, sub, jpeg()).status_code == 401
    other = make_institution(session, slug="other")
    make_user(session, "x@other.test", institution_id=other.id)
    sign_in(client, "x@other.test")
    assert _upload(client, sub, jpeg()).status_code == 404
    assert client.get(f"{API}/documents/{doc.id}/status").status_code == 404


def test_platform_admin_can_upload_anywhere(client: TestClient, session: Session, storage):
    doc = _doc(session, storage)
    make_user(session, "root@platform.test", role=UserRole.PLATFORM_ADMIN)
    sign_in(client, "root@platform.test")
    assert _upload(client, str(doc.submission_id), jpeg()).status_code == 202
