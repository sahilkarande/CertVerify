import email
import re
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import utcnow
from app.db.session import Database
from app.models import (
    Applicant,
    AuditEvent,
    Document,
    LoginCode,
    RequirementItem,
    StoredFile,
    Submission,
    User,
    ValidationIssue,
)
from app.models.enums import (
    ApplicantSource,
    IssueSeverity,
    SubmissionStatus,
    UserRole,
)
from app.seeds.document_types import seed_document_types
from tests.auth_helpers import CSRF, make_user
from tests.factories import make_institution, make_requirement_set
from tests.test_jobs import jpeg

S = "/api/v1/student"
ANSWERS = {
    "full_name": "Asha  Patil",
    "phone": "9800000000",
    "degree_status": "has_degree",
    "claimed_aggregate_percent": 72.5,
}


@pytest.fixture
def intake(session: Session) -> str:
    seed_document_types(session)
    inst = make_institution(session)
    rs = make_requirement_set(session, inst)
    rs.items.append(
        RequirementItem(
            key="degree_or_fallback",
            label="Degree certificate",
            position=2,
            accepted_type_codes=["degree_certificate", "provisional_degree_certificate"],
            condition={
                "attribute": "degree_status",
                "accepted_by_value": {"has_degree": ["degree_certificate"]},
            },
        )
    )
    session.commit()
    return str(rs.id)


def last_code(settings: Settings, to: str) -> str:
    to = to.strip().lower()
    files = sorted(Path(settings.outbox_dir).glob("*.eml"))
    for f in reversed(files):
        msg = email.message_from_bytes(f.read_bytes())
        if msg["To"] == to:
            return re.search(r"\b(\d{6})\b", msg["Subject"]).group(1)
    raise AssertionError("no email")


def sign_in_student(client: TestClient, settings: Settings, addr: str = "asha@example.in"):
    assert (
        client.post(f"{S}/auth/request-code", json={"email": addr}, headers=CSRF).status_code == 202
    )
    code = last_code(settings, addr)
    r = client.post(f"{S}/auth/verify-code", json={"email": addr, "code": code}, headers=CSRF)
    assert r.status_code == 200, r.text
    return r.json()


# ---------- sign-in by email code ----------


def test_code_sign_in_creates_a_student_account(client, test_settings, intake, session) -> None:
    me = sign_in_student(client, test_settings, "Asha@Example.in ")
    assert me["role"] == "student" and me["mfa_passed"] is True
    user = session.scalar(select(User).where(User.email == "asha@example.in"))
    assert user.role is UserRole.STUDENT and user.password_hash is None
    [row] = session.scalars(select(LoginCode))
    code = last_code(test_settings, "asha@example.in")
    assert code not in row.code_hash and row.used_at is not None  # hashed, single use
    assert client.get("/api/v1/review/submissions").status_code == 403  # signed in, not staff


def test_wrong_codes_are_limited_and_codes_expire(client, test_settings, intake, session) -> None:
    client.post(f"{S}/auth/request-code", json={"email": "a@x.in"}, headers=CSRF)
    good = last_code(test_settings, "a@x.in")
    bad = f"{(int(good) + 1) % 10**6:06d}"
    for _ in range(5):
        r = client.post(
            f"{S}/auth/verify-code", json={"email": "a@x.in", "code": bad}, headers=CSRF
        )
        assert r.status_code == 400
    r = client.post(f"{S}/auth/verify-code", json={"email": "a@x.in", "code": good}, headers=CSRF)
    assert r.status_code == 400  # burnt after 5 wrong guesses

    client.post(f"{S}/auth/request-code", json={"email": "a@x.in"}, headers=CSRF)
    good = last_code(test_settings, "a@x.in")
    row = session.scalars(select(LoginCode).order_by(LoginCode.created_at.desc())).first()
    row.expires_at = utcnow() - timedelta(seconds=1)
    session.commit()
    r = client.post(f"{S}/auth/verify-code", json={"email": "a@x.in", "code": good}, headers=CSRF)
    assert r.status_code == 400


def test_only_the_newest_code_works_and_requests_are_capped(client, test_settings, intake) -> None:
    client.post(f"{S}/auth/request-code", json={"email": "b@x.in"}, headers=CSRF)
    first = last_code(test_settings, "b@x.in")
    client.post(f"{S}/auth/request-code", json={"email": "b@x.in"}, headers=CSRF)
    second = last_code(test_settings, "b@x.in")
    ok = client.post(f"{S}/auth/verify-code", json={"email": "b@x.in", "code": first}, headers=CSRF)
    assert first == second or ok.status_code == 400
    client.post(f"{S}/auth/request-code", json={"email": "b@x.in"}, headers=CSRF)
    sent = len(list(Path(test_settings.outbox_dir).glob("*.eml")))
    r = client.post(f"{S}/auth/request-code", json={"email": "b@x.in"}, headers=CSRF)
    assert r.status_code == 202  # same answer...
    assert len(list(Path(test_settings.outbox_dir).glob("*.eml"))) == sent  # ...but no 4th mail


def test_staff_emails_cannot_use_codes(client, test_settings, intake, session) -> None:
    make_user(session, "staff@cdac.test", institution_id=None)
    r = client.post(f"{S}/auth/request-code", json={"email": "staff@cdac.test"}, headers=CSRF)
    assert r.status_code == 202  # no hint either way
    assert not list(Path(test_settings.outbox_dir).glob("*.eml"))


def test_invalid_email_rejected(client, intake) -> None:
    r = client.post(f"{S}/auth/request-code", json={"email": "not-an-email"}, headers=CSRF)
    assert r.status_code == 422


# ---------- application ----------


@pytest.fixture
def student(client: TestClient, test_settings: Settings, intake: str) -> TestClient:
    sign_in_student(client, test_settings)
    return client


def test_start_application_and_checklist_follows_answers(student, intake, session) -> None:
    assert student.get(f"{S}/applications/{intake}").status_code == 404
    r = student.put(f"{S}/applications/{intake}", json=ANSWERS, headers=CSRF)
    assert r.status_code == 200, r.text
    app = r.json()
    assert app["status"] == "draft" and app["answers"]["full_name"] == "Asha Patil"
    states = {c["key"]: c["state"] for c in app["checklist"]}
    assert states == {
        "ssc": "missing",
        "hsc_or_diploma": "missing",
        "degree_or_fallback": "missing",
    }
    degree = next(c for c in app["checklist"] if c["key"] == "degree_or_fallback")
    assert degree["expected"] == ["Degree certificate"]  # because of "has_degree"
    assert degree["problems"] == ["This document is missing. Please upload it."]
    assert app["ready_to_submit"] is False
    applicant = session.scalar(select(Applicant))
    assert applicant.source is ApplicantSource.PORTAL and applicant.email == "asha@example.in"
    bad = {**ANSWERS, "degree_status": "made_up"}
    assert student.put(f"{S}/applications/{intake}", json=bad, headers=CSRF).status_code == 422


def test_imported_applicant_is_linked_by_email(client, test_settings, intake, session) -> None:
    sub_rs = session.scalar(select(Submission.requirement_set_id).limit(1))
    assert sub_rs is None
    from app.models import Institution, RequirementSet

    rs = session.get(RequirementSet, uuid.UUID(intake))
    a = Applicant(
        institution_id=rs.institution_id,
        full_name="Imported Person",
        email="Imported@Example.in",
        source=ApplicantSource.FORM_IMPORT,
    )
    session.add(a)
    session.flush()
    session.add(
        Submission(
            applicant_id=a.id,
            requirement_set_id=rs.id,
            status=SubmissionStatus.SUBMITTED,
            attributes={"degree_status": "has_degree"},
        )
    )
    session.commit()
    assert Institution  # imported for the type check above
    sign_in_student(client, test_settings, "imported@example.in")
    app = client.get(f"{S}/applications/{intake}").json()
    assert app["answers"]["full_name"] == "Imported Person" and app["status"] == "submitted"


def test_upload_delete_and_submit(
    student, intake, session, database: Database, test_settings
) -> None:
    from app.core.security import load_storage_key
    from app.jobs.pipeline import Engines
    from app.jobs.worker import Worker
    from app.storage import EncryptedLocalStorage
    from tests.test_jobs import ScriptedOcr

    student.put(f"{S}/applications/{intake}", json=ANSWERS, headers=CSRF)
    up = lambda slot: student.post(  # noqa: E731
        f"{S}/applications/{intake}/documents",
        headers=CSRF,
        data={"slot": slot},
        files={"file": ("scan.jpg", jpeg(), "image/jpeg")},
    )
    first = up("ssc").json()["document_id"]
    app = student.get(f"{S}/applications/{intake}").json()
    ssc = next(c for c in app["checklist"] if c["key"] == "ssc")
    assert ssc["state"] == "processing" and ssc["documents"][0]["id"] == first

    storage = EncryptedLocalStorage(test_settings.storage_dir, load_storage_key(test_settings))
    worker = Worker(database, storage, Engines(rotation=ScriptedOcr("tesseract")), "w")
    worker.run_once()  # the quick check
    worker.run_once()  # the read
    status = student.get(f"{S}/documents/{first}/status").json()
    assert status["job"]["finished"] is True
    app = student.get(f"{S}/applications/{intake}").json()
    page_id = app["checklist"][0]["documents"][0]["pages"][0]["id"]
    assert student.get(f"{S}/pages/{page_id}/image").headers["content-type"] == "image/jpeg"

    # a wrong upload can be removed, file and page images included
    files_before = session.scalar(select(StoredFile.id).limit(1)) is not None
    assert student.delete(f"{S}/documents/{first}", headers=CSRF).status_code == 204
    session.expire_all()
    assert session.get(Document, uuid.UUID(first)) is None and files_before
    assert session.scalars(select(StoredFile)).all() == []
    assert session.scalar(select(AuditEvent).where(AuditEvent.action == "document.removed"))

    r = student.post(f"{S}/applications/{intake}/submit", headers=CSRF)
    assert r.status_code == 409 and "10th" in r.json()["detail"]
    for slot in ("ssc", "hsc_or_diploma", "degree_or_fallback"):
        up(slot)
    r = student.post(f"{S}/applications/{intake}/submit", headers=CSRF)
    assert r.status_code == 200 and r.json()["status"] in ("submitted", "needs_review")


def test_students_only_see_their_own_things(client, test_settings, intake, session) -> None:
    sign_in_student(client, test_settings, "one@example.in")
    client.put(f"{S}/applications/{intake}", json=ANSWERS, headers=CSRF)
    doc = client.post(
        f"{S}/applications/{intake}/documents",
        headers=CSRF,
        data={"slot": "ssc"},
        files={"file": ("a.jpg", jpeg(), "image/jpeg")},
    ).json()["document_id"]
    client.post("/api/v1/auth/logout", headers=CSRF)
    sign_in_student(client, test_settings, "two@example.in")
    assert client.get(f"{S}/documents/{doc}/status").status_code == 404
    assert client.delete(f"{S}/documents/{doc}", headers=CSRF).status_code == 404
    assert client.get(f"{S}/applications/{intake}").status_code == 404  # not someone else's


def test_students_are_not_told_about_fraud_checks(student, intake, session) -> None:
    student.put(f"{S}/applications/{intake}", json=ANSWERS, headers=CSRF)
    sub = session.scalar(select(Submission))
    for code in (
        "file_shared_with_other_applicant",
        "claimed_percentage_mismatch",
        "identifier_shared_with_other_applicant",
    ):
        session.add(
            ValidationIssue(
                submission_id=sub.id,
                code=code,
                severity=IssueSeverity.ERROR,
                message="internal",
                details={"key": code},
            )
        )
    session.commit()
    body = str(student.get(f"{S}/applications/{intake}").json())
    assert (
        "internal" not in body
        and "shared" not in body
        and "claimed" not in body.replace("claimed_aggregate_percent", "")
    )


def test_decided_applications_are_locked(student, intake, session) -> None:
    student.put(f"{S}/applications/{intake}", json=ANSWERS, headers=CSRF)
    sub = session.scalar(select(Submission))
    sub.status = SubmissionStatus.VERIFIED
    session.commit()
    assert student.put(f"{S}/applications/{intake}", json=ANSWERS, headers=CSRF).status_code == 409
    r = student.post(
        f"{S}/applications/{intake}/documents",
        headers=CSRF,
        data={"slot": "ssc"},
        files={"file": ("a.jpg", jpeg(), "image/jpeg")},
    )
    assert r.status_code == 409


def test_staff_cannot_use_student_endpoints(client, intake, session) -> None:
    from tests.auth_helpers import sign_in

    make_user(session, "v@x.test", institution_id=None)
    sign_in(client, "v@x.test")
    assert client.get(f"{S}/applications").status_code == 403
