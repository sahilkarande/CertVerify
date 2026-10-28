"""Deleting student data on request: one application, or the whole person."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.admin.erasure import erasure_token
from app.core.config import Settings
from app.core.security import load_storage_key
from app.db.base import utcnow
from app.importers.cdac_form.importer import import_form
from app.importers.cdac_form.parser import parse_csv
from app.models import (
    Applicant,
    AuditEvent,
    Document,
    DocumentPage,
    ErasedApplicant,
    ExtractedField,
    LoginCode,
    StoredFile,
    Submission,
    SubmissionMessage,
    User,
    UserSession,
    ValidationIssue,
)
from app.models.enums import ApplicantSource, FieldSource, IssueSeverity, UserRole
from app.seeds.document_types import seed_document_types
from app.services.files import store_file
from app.storage import EncryptedLocalStorage
from tests.auth_helpers import CSRF, make_user, sign_in
from tests.cdac_fixtures import KEY, make_row, write_csv
from tests.factories import make_institution, make_requirement_set

A = "/api/v1/admin"
NAME = "Asha Ramesh Patil"


@pytest.fixture
def storage(test_settings: Settings) -> EncryptedLocalStorage:
    # The app's own storage, so files the API deletes are the ones this test stored.
    return EncryptedLocalStorage(test_settings.storage_dir, load_storage_key(test_settings))


def _student(db: Session, storage, inst, rs, ref: str, name: str, email: str, files: list[bytes]):
    user = User(email=email, full_name=name, role=UserRole.STUDENT, institution_id=None)
    db.add(user)
    db.flush()
    applicant = Applicant(
        institution_id=inst.id,
        user_id=user.id,
        external_ref=ref,
        full_name=name,
        email=email,
        phone="9800000000",
        source=ApplicantSource.FORM_IMPORT,
    )
    db.add(applicant)
    db.flush()
    sub = Submission(applicant_id=applicant.id, requirement_set_id=rs.id)
    db.add(sub)
    db.flush()
    for n, data in enumerate(files):
        f = store_file(db, storage, data, mime_type="image/jpeg")
        image = store_file(db, storage, data + b"-page", mime_type="image/jpeg")
        doc = Document(submission_id=sub.id, file_id=f.id, slot_key=f"slot{n}")
        db.add(doc)
        db.flush()
        page = DocumentPage(document_id=doc.id, page_number=1, image_file_id=image.id)
        db.add(page)
        db.flush()
        db.add(ExtractedField(document_id=doc.id, page_id=page.id, field_key="candidate_name",
                              value=name.upper(), source=FieldSource.MACHINE))  # fmt: skip
    db.add(SubmissionMessage(submission_id=sub.id, from_staff=False, body=f"Hi, this is {name}"))
    db.add(ValidationIssue(submission_id=sub.id, code="x", severity=IssueSeverity.INFO,
                           message="m", details={}))  # fmt: skip
    now = utcnow()
    db.add(UserSession(user_id=user.id, token_hash=ref * 2, created_at=now, last_seen_at=now,
                       expires_at=now + timedelta(hours=1), mfa_passed=True))  # fmt: skip
    db.add(LoginCode(email=email, code_hash="h" * 64, created_at=now,
                     expires_at=now + timedelta(minutes=10)))  # fmt: skip
    db.flush()
    return applicant, sub


@pytest.fixture
def world(session: Session, storage):
    seed_document_types(session)
    inst = make_institution(session)
    rs = make_requirement_set(session, inst)
    asha, asha_sub = _student(session, storage, inst, rs, "100000001", NAME, "asha@x.in",
                              [b"asha-ssc", b"shared-file"])  # fmt: skip
    ravi, _ = _student(session, storage, inst, rs, "100000002", "Ravi Kumar", "ravi@x.in",
                       [b"shared-file"])  # fmt: skip  # the same file uploaded by both
    # The same file uploaded by both: the import keeps one stored copy for both documents.
    [shared] = [d for d in asha_sub.documents if d.slot_key == "slot1"]
    ravi_doc = session.scalar(
        select(Document).join(Submission).where(Submission.applicant_id == ravi.id)
    )
    ravi_doc.file_id = shared.file_id
    make_user(session, "admin@c.test", UserRole.INSTITUTION_ADMIN, inst.id)
    session.commit()
    return {"inst": inst, "asha": asha, "sub": asha_sub, "ravi": ravi, "rs": rs}


def _count(db: Session, model, *where) -> int:
    return db.scalar(select(func.count()).select_from(model).where(*where))


def _keys(db: Session) -> set[str]:
    return set(db.scalars(select(StoredFile.object_key)))


def test_the_name_must_be_typed_to_confirm(client: TestClient, session: Session, world) -> None:
    sign_in(client, "admin@c.test")
    for wrong in ("", "Asha", "Ravi Kumar"):
        r = client.post(f"{A}/applicants/{world['asha'].id}/erase", json={"confirm": wrong},
                        headers=CSRF)  # fmt: skip
        assert r.status_code == 422
    assert session.get(Applicant, world["asha"].id) is not None


def test_deleting_an_application_keeps_the_person(
    client: TestClient, session: Session, storage, world
) -> None:
    sign_in(client, "admin@c.test")
    asha_id, user_id, sub_id = world["asha"].id, world["asha"].user_id, world["sub"].id
    keys_before = _keys(session)
    r = client.post(f"{A}/submissions/{sub_id}/delete", json={"confirm": " asha ramesh PATIL "},
                    headers=CSRF)  # fmt: skip
    assert r.status_code == 200, r.text
    assert r.json() == {"submissions": 1, "documents": 2, "account_deleted": False,
                        "files_removed": 3}  # fmt: skip  # 2 page images + Asha's own file
    session.expire_all()
    assert session.get(Submission, sub_id) is None
    assert _count(session, SubmissionMessage, SubmissionMessage.submission_id == sub_id) == 0
    assert _count(session, ValidationIssue, ValidationIssue.submission_id == sub_id) == 0
    assert session.get(Applicant, asha_id).email == "asha@x.in"  # the person stays
    assert session.get(User, user_id) is not None
    gone = keys_before - _keys(session)
    assert len(gone) == 3
    for key in gone:
        assert not storage.exists(key)  # removed from storage, not just the database
    shared = session.scalar(
        select(StoredFile).join(Document, Document.file_id == StoredFile.id)
        .where(Document.submission_id != sub_id)
    )  # fmt: skip
    assert storage.exists(shared.object_key)  # Ravi still has the shared file


def test_erasing_removes_everything_about_the_person(
    client: TestClient, session: Session, storage, world
) -> None:
    sign_in(client, "admin@c.test")
    asha_id, user_id, ravi_id = world["asha"].id, world["asha"].user_id, world["ravi"].id
    r = client.post(f"{A}/applicants/{asha_id}/erase", json={"confirm": NAME}, headers=CSRF)
    assert r.status_code == 200, r.text
    assert r.json()["account_deleted"] is True and r.json()["submissions"] == 1
    session.expire_all()
    assert session.get(Applicant, asha_id) is None
    assert session.get(User, user_id) is None
    assert _count(session, UserSession, UserSession.user_id == user_id) == 0
    assert _count(session, LoginCode, LoginCode.email == "asha@x.in") == 0
    assert _count(session, Applicant, Applicant.email == "asha@x.in") == 0
    assert _count(session, ExtractedField, ExtractedField.value == NAME.upper()) == 0
    assert _count(session, SubmissionMessage, SubmissionMessage.body.contains("Asha")) == 0
    # Ravi is untouched, shared file included.
    ravi = session.get(Applicant, ravi_id)
    assert ravi is not None and _count(session, LoginCode, LoginCode.email == "ravi@x.in") == 1
    for f in session.scalars(select(StoredFile)):
        assert storage.exists(f.object_key)
    # The trail says who erased what, never who it was.
    [event] = session.scalars(select(AuditEvent).where(AuditEvent.action == "applicant.erased"))
    text = str(event.details).lower()
    assert "asha" not in text and "100000001" not in text and event.actor_user_id is not None


def test_an_erased_person_is_not_re_imported(
    client: TestClient, session: Session, world, tmp_path, test_settings
) -> None:
    from app.auth.totp import derive_key

    inst_id = world["inst"].id
    sign_in(client, "admin@c.test")
    client.post(f"{A}/applicants/{world['asha'].id}/erase", json={"confirm": NAME}, headers=CSRF)
    key = derive_key(load_storage_key(test_settings), "erasure")
    [marker] = session.scalars(select(ErasedApplicant))
    assert marker.token == erasure_token(key, inst_id, "100000001")
    assert "100000001" not in marker.token  # only a keyed hash is kept

    rows = [make_row("100000001"), make_row("100000003")]
    report = import_form(session, parse_csv(write_csv(tmp_path / "f.csv", rows)), erasure_key=key)
    assert report.skipped_erased == 1 and report.applicants_created == 1
    assert _count(session, Applicant, Applicant.external_ref == "100000001") == 0
    # A different key (another installation) does not match: the hash is keyed.
    assert erasure_token(KEY, inst_id, "100000001") != marker.token


def test_only_admins_of_that_institution_may_delete(
    client: TestClient, session: Session, world
) -> None:
    other = make_institution(session, slug="other-college")
    make_user(session, "other-admin@c.test", UserRole.INSTITUTION_ADMIN, other.id)
    make_user(session, "verifier@c.test", UserRole.VERIFIER, world["inst"].id)
    make_user(session, "platform@c.test", UserRole.PLATFORM_ADMIN)
    session.commit()
    url = f"{A}/applicants/{world['asha'].id}/erase"
    body = {"confirm": NAME}

    sign_in(client, "verifier@c.test")
    assert client.post(url, json=body, headers=CSRF).status_code == 403
    client.post("/api/v1/auth/logout", headers=CSRF)
    sign_in(client, "other-admin@c.test")
    assert client.post(url, json=body, headers=CSRF).status_code == 404  # not theirs
    sub_url = f"{A}/submissions/{world['sub'].id}/delete"
    assert client.post(sub_url, json=body, headers=CSRF).status_code == 404
    client.post("/api/v1/auth/logout", headers=CSRF)
    sign_in(client, "platform@c.test")
    assert client.post(url, json=body, headers=CSRF).status_code == 200


def test_a_staff_account_is_never_deleted(client: TestClient, session: Session, world) -> None:
    staff = make_user(session, "staff-applicant@c.test", UserRole.VERIFIER, world["inst"].id)
    world["asha"].user_id = staff.id  # e.g. a staff member who also applied
    session.commit()
    sign_in(client, "admin@c.test")
    r = client.post(f"{A}/applicants/{world['asha'].id}/erase", json={"confirm": NAME},
                    headers=CSRF)  # fmt: skip
    assert r.status_code == 200 and r.json()["account_deleted"] is False
    session.expire_all()
    assert session.get(User, staff.id) is not None
