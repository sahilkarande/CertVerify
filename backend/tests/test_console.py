from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import (
    AuditEvent,
    Document,
    DocumentPage,
    DocumentType,
    ExtractedField,
    Submission,
    SubmissionMessage,
    ValidationIssue,
)
from app.models.enums import (
    FieldSource,
    IssueSeverity,
    IssueStatus,
    SubmissionStatus,
)
from app.review.learning import corrections_report, export_corrections
from app.seeds.document_types import seed_document_types
from tests.auth_helpers import CSRF, make_user, sign_in
from tests.factories import make_institution, make_stored_file, make_submission

R = "/api/v1/review"


@pytest.fixture
def case(session: Session) -> dict:
    """A submitted application with one semester page, two values and a warning."""
    seed_document_types(session)
    sem = session.scalar(select(DocumentType).where(DocumentType.code == "semester_marksheet"))
    sub = make_submission(session)
    sub.status = SubmissionStatus.NEEDS_REVIEW
    sub.applicant.email = "asha@example.in"
    doc = Document(submission_id=sub.id, file_id=make_stored_file(session).id, slot_key="ssc")
    session.add(doc)
    session.flush()
    page = DocumentPage(document_id=doc.id, page_number=1, detected_type_id=sem.id)
    session.add(page)
    session.flush()
    sgpa = ExtractedField(
        document_id=doc.id,
        page_id=page.id,
        field_key="sgpa",
        machine_value="8.27",
        value="8.27",
        confidence=0.95,
    )
    name = ExtractedField(
        document_id=doc.id,
        page_id=page.id,
        field_key="candidate_name",
        machine_value="ASHA PATIL",
        value="ASHA PATIL",
        confidence=0.6,
    )
    warn = ValidationIssue(
        submission_id=sub.id,
        code="name_partial_match",
        severity=IssueSeverity.WARNING,
        message="Name partly matches.",
        details={"key": "name:x"},
    )
    session.add_all([sgpa, name, warn])
    session.commit()
    make_user(session, "v@cdac.test", institution_id=sub.applicant.institution_id)
    return {"sub": sub.id, "page": page.id, "sgpa": sgpa.id, "name": name.id, "warn": warn.id}


@pytest.fixture
def verifier(client: TestClient, case: dict) -> TestClient:
    sign_in(client, "v@cdac.test")
    return client


def detail(c: TestClient, case: dict) -> dict:
    return c.get(f"{R}/submissions/{case['sub']}").json()


# ---------- values ----------


def test_correct_and_confirm_values(verifier, case, session: Session) -> None:
    r = verifier.patch(
        f"{R}/fields/{case['sgpa']}", headers=CSRF, json={"value": "8.72", "expected_value": "8.27"}
    )
    assert r.status_code == 200 and r.json()["source"] == "human"
    f = session.get(ExtractedField, case["sgpa"])
    session.refresh(f)
    assert (f.machine_value, f.value, f.corrected_by_id is not None) == ("8.27", "8.72", True)
    # someone else already changed it: refused, not overwritten
    stale = verifier.patch(
        f"{R}/fields/{case['sgpa']}", headers=CSRF, json={"value": "9.00", "expected_value": "8.27"}
    )
    assert stale.status_code == 409
    verifier.patch(
        f"{R}/fields/{case['name']}",
        headers=CSRF,
        json={"value": "ASHA PATIL", "expected_value": "ASHA PATIL"},
    )
    actions = [e.action for e in session.scalars(select(AuditEvent))]
    assert "field.corrected" in actions and "field.confirmed" in actions
    assert "8.72" not in str([e.details for e in session.scalars(select(AuditEvent))])


def test_add_a_missed_value_only_from_the_schema(verifier, case, session: Session) -> None:
    page = detail(verifier, case)["documents"][0]["pages"][0]
    assert "cgpa" in [f["key"] for f in page["addable_fields"]]
    assert "sgpa" not in [f["key"] for f in page["addable_fields"]]  # already there
    r = verifier.post(
        f"{R}/pages/{case['page']}/fields", headers=CSRF, json={"key": "cgpa", "value": "8.41"}
    )
    assert r.status_code == 201
    added = session.scalar(select(ExtractedField).where(ExtractedField.field_key == "cgpa"))
    assert added.machine_value is None and added.source is FieldSource.HUMAN
    assert (
        verifier.post(
            f"{R}/pages/{case['page']}/fields", headers=CSRF, json={"key": "board", "value": "x"}
        ).status_code
        == 422
    )
    assert (
        verifier.post(
            f"{R}/pages/{case['page']}/fields", headers=CSRF, json={"key": "cgpa", "value": "8"}
        ).status_code
        == 409
    )


# ---------- issues & decisions ----------


def test_dismissing_needs_a_reason_and_approval_needs_no_open_warnings(
    verifier, case, session: Session
) -> None:
    sub = session.get(Submission, case["sub"])
    approve = {"decision": "approve", "expected_status": "needs_review"}
    r = verifier.post(f"{R}/submissions/{case['sub']}/decision", headers=CSRF, json=approve)
    assert r.status_code == 409 and "1 open" in r.json()["detail"]

    url = f"{R}/issues/{case['warn']}/status"
    assert verifier.post(url, headers=CSRF, json={"status": "dismissed"}).status_code == 422
    r = verifier.post(
        url,
        headers=CSRF,
        json={"status": "dismissed", "note": "Name changed after marriage; proof seen."},
    )
    assert r.status_code == 200
    issue = session.get(ValidationIssue, case["warn"])
    session.refresh(issue)
    assert issue.resolution_note.startswith("Name changed") and issue.resolved_by_id is not None

    r = verifier.post(f"{R}/submissions/{case['sub']}/decision", headers=CSRF, json=approve)
    assert r.status_code == 200 and r.json()["status"] == "verified"
    session.refresh(sub)
    assert sub.decided_by_id is not None and sub.decided_at is not None
    # deciding twice from a stale view is refused
    assert (
        verifier.post(
            f"{R}/submissions/{case['sub']}/decision", headers=CSRF, json=approve
        ).status_code
        == 409
    )


def test_reopening_an_issue_clears_its_resolution(verifier, case, session: Session) -> None:
    url = f"{R}/issues/{case['warn']}/status"
    verifier.post(url, headers=CSRF, json={"status": "resolved", "note": "fixed"})
    verifier.post(url, headers=CSRF, json={"status": "open"})
    issue = session.get(ValidationIssue, case["warn"])
    session.refresh(issue)
    assert (issue.status, issue.resolution_note, issue.resolved_by_id) == (
        IssueStatus.OPEN,
        None,
        None,
    )


def test_request_changes_emails_and_lets_the_student_resubmit(
    verifier, case, session: Session, test_settings: Settings
) -> None:
    body = {"decision": "request_changes", "expected_status": "needs_review"}
    assert (
        verifier.post(
            f"{R}/submissions/{case['sub']}/decision", headers=CSRF, json=body
        ).status_code
        == 422
    )  # a message is required
    body["note"] = "Please upload page 2 of your 12th marksheet."
    r = verifier.post(f"{R}/submissions/{case['sub']}/decision", headers=CSRF, json=body)
    assert r.json()["status"] == "changes_requested"
    [msg] = session.scalars(select(SubmissionMessage))
    assert msg.from_staff and "page 2" in msg.body
    mails = list(Path(test_settings.outbox_dir).glob("*.eml"))
    assert len(mails) == 1 and "page 2" in mails[0].read_text()
    assert detail(verifier, case)["messages"][0]["body"].startswith("Please upload")


def test_reject_needs_a_reason_and_reopen_brings_it_back(verifier, case, session) -> None:
    url = f"{R}/submissions/{case['sub']}/decision"
    assert (
        verifier.post(
            url, headers=CSRF, json={"decision": "reject", "expected_status": "needs_review"}
        ).status_code
        == 422
    )
    r = verifier.post(
        url,
        headers=CSRF,
        json={
            "decision": "reject",
            "expected_status": "needs_review",
            "note": "Documents belong to someone else.",
        },
    )
    assert r.json()["status"] == "rejected"
    r = verifier.post(
        url,
        headers=CSRF,
        json={
            "decision": "reopen",
            "expected_status": "rejected",
            "note": "Student sent the right files by post.",
        },
    )
    assert r.json()["status"] == "needs_review"


def test_other_institutions_cannot_act(client: TestClient, case, session: Session) -> None:
    other = make_institution(session, slug="other-college")
    make_user(session, "x@other.test", institution_id=other.id)
    sign_in(client, "x@other.test")
    assert (
        client.patch(
            f"{R}/fields/{case['sgpa']}",
            headers=CSRF,
            json={"value": "1", "expected_value": "8.27"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"{R}/issues/{case['warn']}/status", headers=CSRF, json={"status": "resolved"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"{R}/submissions/{case['sub']}/decision",
            headers=CSRF,
            json={"decision": "approve", "expected_status": "needs_review"},
        ).status_code
        == 404
    )


def test_students_cannot_use_console_actions(
    client: TestClient, case, test_settings: Settings
) -> None:
    from tests.test_student import sign_in_student

    sign_in_student(client, test_settings, "nosy@example.in")
    r = client.patch(
        f"{R}/fields/{case['sgpa']}", headers=CSRF, json={"value": "1", "expected_value": "8.27"}
    )
    assert r.status_code == 403
    r = client.post(
        f"{R}/submissions/{case['sub']}/decision",
        headers=CSRF,
        json={"decision": "approve", "expected_status": "needs_review"},
    )
    assert r.status_code == 403


# ---------- learning loop ----------


def test_corrections_become_training_data(verifier, case, session: Session, tmp_path) -> None:
    verifier.patch(
        f"{R}/fields/{case['sgpa']}", headers=CSRF, json={"value": "8.72", "expected_value": "8.27"}
    )
    verifier.patch(
        f"{R}/fields/{case['name']}",
        headers=CSRF,
        json={"value": "ASHA PATIL", "expected_value": "ASHA PATIL"},
    )
    verifier.post(
        f"{R}/pages/{case['page']}/fields", headers=CSRF, json={"key": "cgpa", "value": "8.41"}
    )
    report = corrections_report(session)
    assert report["sgpa"]["corrected"] == 1 and report["sgpa"]["high_conf"] == 1
    assert report["sgpa"]["high_conf_ok"] == 0  # a confident reading that was wrong
    assert report["candidate_name"]["confirmed"] == 1 and report["cgpa"]["added"] == 1
    out = tmp_path / "c.jsonl"
    assert export_corrections(session, out) == 3
    assert oct(out.stat().st_mode)[-3:] == "600"
    assert '"machine_value": "8.27", "value": "8.72"' in out.read_text()


# ---------- student side ----------


def test_student_sees_the_request_and_can_reply_and_resubmit(
    client: TestClient, session: Session, test_settings: Settings
) -> None:
    from tests.factories import make_requirement_set
    from tests.test_student import ANSWERS, sign_in_student

    seed_document_types(session)
    inst = make_institution(session)
    rs = make_requirement_set(session, inst)
    session.commit()
    S = "/api/v1/student"
    sign_in_student(client, test_settings, "stud@example.in")
    client.put(f"{S}/applications/{rs.id}", json=ANSWERS, headers=CSRF)
    sub = session.scalar(select(Submission))
    sub.status = SubmissionStatus.NEEDS_REVIEW
    session.commit()
    client.post("/api/v1/auth/logout", headers=CSRF)

    make_user(session, "v2@cdac.test", institution_id=inst.id)
    sign_in(client, "v2@cdac.test")
    client.post(
        f"{R}/submissions/{sub.id}/decision",
        headers=CSRF,
        json={
            "decision": "request_changes",
            "expected_status": "needs_review",
            "note": "Your 10th marksheet is cut off.",
        },
    )
    client.post("/api/v1/auth/logout", headers=CSRF)

    sign_in_student(client, test_settings, "stud@example.in")
    app = client.get(f"{S}/applications/{rs.id}").json()
    assert app["status"] == "changes_requested" and app["editable"] is True
    assert app["decision_note"] == "Your 10th marksheet is cut off."
    r = client.post(
        f"{S}/applications/{rs.id}/messages",
        headers=CSRF,
        json={"body": "Uploaded a full scan now."},
    )
    assert [m["from_staff"] for m in r.json()["messages"]] == [True, False]
    session.refresh(sub)
    assert sub.status is SubmissionStatus.CHANGES_REQUESTED


def test_decision_from_a_stale_view_is_refused(verifier, case, session: Session) -> None:
    # The verifier's screen still says "submitted", but it moved to "needs review" since.
    r = verifier.post(
        f"{R}/submissions/{case['sub']}/decision",
        headers=CSRF,
        json={"decision": "reject", "expected_status": "submitted", "note": "x"},
    )
    assert r.status_code == 409
    assert session.get(Submission, case["sub"]).status is SubmissionStatus.NEEDS_REVIEW


def test_times_are_sent_with_a_utc_offset(verifier, case) -> None:
    verifier.post(f"{R}/submissions/{case['sub']}/messages", headers=CSRF, json={"body": "Hello"})
    d = detail(verifier, case)
    assert d["messages"][0]["at"].endswith("+00:00")  # else browsers read it as local time
