"""The readability layer: pages whose text was read poorly ask the student to type the key
values, which are kept only if they match the page; otherwise a clearer copy is asked for."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.extract import readability as R
from app.extract.pipeline import extract_page_fields
from app.models import (
    AuditEvent,
    Document,
    DocumentPage,
    DocumentType,
    ExtractedField,
    PageOcr,
    Submission,
    ValidationIssue,
)
from app.models.enums import FieldSource, OcrStatus, Readability
from app.validate.engine import validate_submission
from tests.auth_helpers import CSRF
from tests.factories import make_stored_file
from tests.test_student import ANSWERS, S, intake, sign_in_student  # noqa: F401  (fixture)

NAME = "Pooja Deepakrao Gadhe"
STRONG, WEAK = 97.0, 70.0


def page_lines(name_conf: float = WEAK, rest: float = WEAK) -> list[tuple[str, float]]:
    return [
        ("UNIVERSITY OF MUMBAI STATEMENT OF GRADES", STRONG),
        ("COURSE CODE GRADE POINTS CREDITS EARNED TOTAL", STRONG),
        ("NAME : FOOJA DEEPAIORAT GADHE", name_conf),  # 2 of 3 form-name words recognisable
        ("SEAT NO : 11601OO13", rest),
        ("SGPA : 8.45", rest),
        ("ENGINEERING MATHEMATICS 4 A 9", rest),
    ]


def add_page(
    db: Session, doc: Document, lines: list[tuple[str, float]], status=OcrStatus.OK
) -> DocumentPage:
    sem = db.scalar(select(DocumentType).where(DocumentType.code == "semester_marksheet"))
    p = DocumentPage(document_id=doc.id, page_number=1, detected_type_id=sem.id, semesters=[3])
    db.add(p)
    db.flush()
    words = [
        [w, conf, j * 120, i * 40, 100, 20, f"l{i}"]
        for i, (text, conf) in enumerate(lines)
        for j, w in enumerate(text.split())
    ]
    db.add(
        PageOcr(
            page_id=p.id,
            engine="paddle",
            engine_version="t",
            pipeline_version=1,
            status=status,
            text="\n".join(t for t, _ in lines),
            words=words,
        )
    )
    db.flush()
    extract_page_fields(db, p)
    return p


@pytest.fixture
def student(client: TestClient, test_settings: Settings, intake: str, session: Session):  # noqa: F811
    sign_in_student(client, test_settings)
    r = client.put(f"{S}/applications/{intake}", json={**ANSWERS, "full_name": NAME}, headers=CSRF)
    assert r.status_code == 200
    sub = session.scalar(select(Submission))
    doc = Document(submission_id=sub.id, file_id=make_stored_file(session).id, slot_key="ssc")
    session.add(doc)
    session.flush()
    return sub, doc


# ---------- matching what the student typed ----------


def test_names_tolerate_ocr_damage_but_not_another_person() -> None:
    lines = ["NAME : FOOJA DEEPAIORAT GADHE", "SEAT NO 116010013"]
    m = R.match_name(NAME, lines)
    assert m.ok and not m.exact and "FOOJA" in m.read_as
    assert R.match_name("POOJA DEEPAKRAO GADHE", ["NAME POOJA DEEPAKRAO GADHE"]).exact
    assert not R.match_name("Shweta Deepak Mohile", lines).ok
    assert not R.match_name("Abhishek Mohile", ["NAME : SHWETA MOHILE"]).ok
    # Every word looks alike, but not closely enough on average: a different person.
    assert not R.match_name("Asha Patil", ["NAME : ASHOK PATEL"]).ok


def test_ids_forgive_damaged_characters_never_a_different_digit() -> None:
    assert R.match_id("116010013", ["SEAT NO : 11601OO13"]).exact  # letter O for zero
    slip = R.match_id("116010013", ["SEAT NO : 11601A013"])  # one character damaged
    assert slip.ok and not slip.exact
    assert not R.match_id("116010014", ["SEAT NO : 116010013"]).ok  # a classmate's number
    assert not R.match_id("116010013", ["SEAT NO : 1160100"]).ok


def test_marks_must_match_exactly() -> None:
    assert R.match_number("536", ["TOTAL 750/ 536"]).ok
    assert not R.match_number("536", ["TOTAL 750/ 516"]).ok
    assert R.match_number("8.45", ["SGPA : 8.45"]).ok
    assert R.tidy_number("85.4%") == "85.4" and R.tidy_number("eighty") is None


# ---------- assessing pages ----------


def test_well_read_page_is_clear(session: Session, student) -> None:
    _, doc = student
    p = add_page(session, doc, page_lines(STRONG, STRONG))
    assert R.assess_page(session, p) is Readability.CLEAR
    assert p.readability_details["ask"] == []


def test_values_missing_from_a_well_read_page_are_not_asked(session: Session, student) -> None:
    _, doc = student
    lines = [(t, STRONG) for t, _ in page_lines() if not t.startswith("SEAT")]
    p = add_page(session, doc, lines)
    assert R.assess_page(session, p) is Readability.CLEAR  # e.g. a back side without a PRN
    assert p.readability_details["why"] == {"student_id": "missing"}


def test_poorly_read_page_asks_for_the_weak_values(session: Session, student) -> None:
    _, doc = student
    p = add_page(session, doc, page_lines())
    assert R.assess_page(session, p) is Readability.CONFIRM
    assert p.readability_details["ask"] == ["candidate_name", "seat_number", "sgpa"]
    assert R.CONFIRM_WEAK_SHARE <= p.readability_details["weak_share"] < R.RETAKE_WEAK_SHARE


def test_name_on_a_weak_line_is_asked_even_on_a_good_page(session: Session, student) -> None:
    _, doc = student
    p = add_page(session, doc, page_lines(name_conf=WEAK, rest=STRONG))
    assert R.assess_page(session, p) is Readability.CONFIRM
    assert p.readability_details["ask"] == ["candidate_name"]


def test_weak_values_something_independent_agrees_with_are_not_asked(
    session: Session, student
) -> None:
    _, doc = student
    lines = page_lines()
    lines[2] = ("NAME : FOOJA DEEPAKRAO GADHE", WEAK)  # every form-name word is there
    p = add_page(session, doc, lines)
    session.add(
        PageOcr(
            page_id=p.id,
            engine="tesseract",
            engine_version="t",
            pipeline_version=1,
            status=OcrStatus.LOW_CONFIDENCE,
            text="SGPA 8.45",
            words=[["SGPA", 60, 0, 0, 50, 20, "t0"], ["8.45", 60, 60, 0, 50, 20, "t0"]],
        )
    )
    session.flush()
    assert R.assess_page(session, p) is Readability.CONFIRM
    assert p.readability_details["ask"] == ["seat_number"]  # the second engine read 8.45 too


def test_a_doubted_name_is_not_reported_as_someone_elses(session: Session, student) -> None:
    sub, doc = student
    lines = page_lines()
    lines[2] = ("NAME : CANHE PONOJA DEEPAIORAT", WEAK)  # the real misreading of a faint print
    p = add_page(session, doc, lines)
    validate_submission(session, sub)
    codes = {i.code for i in session.scalars(select(ValidationIssue))}
    assert "name_partial_match" in codes  # without the readability check: looks like a mismatch
    R.assess_page(session, p)
    validate_submission(session, sub)
    codes = {i.code for i in session.scalars(select(ValidationIssue)) if i.status.value == "open"}
    assert "page_needs_confirmation" in codes and not codes & {
        "name_partial_match",
        "name_mismatch",
    }


def test_unreadable_page_asks_for_a_clearer_copy(session: Session, student) -> None:
    _, doc = student
    p = add_page(session, doc, [(t, 40.0) for t, _ in page_lines()])
    assert R.assess_page(session, p) is Readability.RETAKE


def test_pages_read_by_an_uncalibrated_engine_are_not_judged(session: Session, student) -> None:
    _, doc = student
    p = add_page(session, doc, page_lines())
    session.scalar(select(PageOcr).where(PageOcr.page_id == p.id)).engine = "tesseract"
    session.flush()
    assert R.assess_page(session, p) is None


# ---------- the student confirms ----------


def _app_page(r) -> dict:
    return next(p for item in r.json()["checklist"] for d in item["documents"] for p in d["pages"])


def test_matching_values_are_kept_as_the_students(
    client: TestClient, session: Session, student
) -> None:
    sub, doc = student
    p = add_page(session, doc, page_lines())
    R.assess_page(session, p)
    validate_submission(session, sub)
    session.commit()
    intake_id = sub.requirement_set_id

    r = client.get(f"{S}/applications/{intake_id}")
    item = next(i for i in r.json()["checklist"] if i["documents"])
    assert not any("name on this document" in m for m in item["problems"])  # misread, not different
    page_json = _app_page(r)
    assert [f["key"] for f in page_json["confirm"]["fields"]] == [
        "candidate_name",
        "seat_number",
        "sgpa",
    ]
    assert "FOOJA" not in str(page_json)  # the student is never shown what was read

    values = {"candidate_name": NAME, "seat_number": "116010013", "sgpa": "8.45"}
    r = client.post(f"/api/v1/student/pages/{p.id}/confirm", json={"values": values}, headers=CSRF)
    assert r.status_code == 200, r.text
    assert _app_page(r)["confirm"] is None
    session.expire_all()
    assert session.get(DocumentPage, p.id).readability is Readability.CONFIRMED
    rows = {
        f.field_key: f
        for f in session.scalars(select(ExtractedField).where(ExtractedField.page_id == p.id))
    }
    name = rows["candidate_name"]
    assert name.source is FieldSource.STUDENT and name.value == NAME.upper()
    assert "FOOJA" in name.machine_value  # what was read stays alongside
    assert rows["seat_number"].value == "116010013" and rows["sgpa"].value == "8.45"
    [issue] = session.scalars(
        select(ValidationIssue).where(ValidationIssue.code == "student_confirmed_values")
    )
    assert issue.details["check"] == ["candidate_name"]  # FOOJA != POOJA: verifier looks
    [event] = session.scalars(
        select(AuditEvent).where(AuditEvent.action == "page.values_confirmed")
    )
    assert "116010013" not in str(event.details)  # keys only, no values

    # Re-extraction (e.g. a new extractor version) keeps the student's values and state.
    extract_page_fields(session, session.get(DocumentPage, p.id))
    R.assess_page(session, session.get(DocumentPage, p.id))
    session.flush()
    kept = session.scalars(
        select(ExtractedField).where(
            ExtractedField.page_id == p.id, ExtractedField.field_key == "candidate_name"
        )
    ).all()
    assert [f.source for f in kept] == [FieldSource.STUDENT]  # no machine row beside it
    assert session.get(DocumentPage, p.id).readability is Readability.CONFIRMED


def test_values_that_do_not_match_twice_ask_for_a_clearer_copy(
    client: TestClient, session: Session, student
) -> None:
    sub, doc = student
    p = add_page(session, doc, page_lines())
    R.assess_page(session, p)
    session.commit()
    url = f"/api/v1/student/pages/{p.id}/confirm"
    wrong = {"candidate_name": NAME, "seat_number": "116010014", "sgpa": "9.45"}

    r = client.post(url, json={"values": wrong}, headers=CSRF)
    assert r.status_code == 200
    confirm = _app_page(r)["confirm"]
    assert confirm["attempts_left"] == 1 and confirm["mismatched"] == ["Seat number", "SGPA / SPI"]

    r = client.post(url, json={"values": wrong}, headers=CSRF)
    page_json = _app_page(r)
    assert page_json["confirm"] is None and page_json["text_unreadable"] is True
    item = next(i for i in r.json()["checklist"] if i["documents"])
    assert item["state"] == "attention"
    assert any("clearer scan or photo" in m for m in item["problems"])
    assert client.post(url, json={"values": wrong}, headers=CSRF).status_code == 409
    session.expire_all()
    assert (
        session.scalars(
            select(ExtractedField).where(ExtractedField.source == FieldSource.STUDENT)
        ).all()
        == []
    )


def test_confirm_needs_exactly_the_asked_values_and_own_pages(
    client: TestClient, session: Session, student, test_settings: Settings
) -> None:
    sub, doc = student
    p = add_page(session, doc, page_lines())
    R.assess_page(session, p)
    session.commit()
    url = f"/api/v1/student/pages/{p.id}/confirm"
    partial = {"candidate_name": NAME}
    assert client.post(url, json={"values": partial}, headers=CSRF).status_code == 422
    sneaky = {"candidate_name": NAME, "seat_number": "1", "sgpa": "8.45", "percentage": "99"}
    assert client.post(url, json={"values": sneaky}, headers=CSRF).status_code == 422
    not_printed = {"candidate_name": NAME, "seat_number": None, "sgpa": "8.45"}
    r = client.post(url, json={"values": not_printed}, headers=CSRF)
    assert r.status_code == 200 and _app_page(r)["confirm"] is None
    session.expire_all()
    assert session.get(DocumentPage, p.id).readability_details["not_printed"] == ["seat_number"]

    client.post("/api/v1/auth/logout", headers=CSRF)
    sign_in_student(client, test_settings, "other@example.in")
    assert client.post(url, json={"values": not_printed}, headers=CSRF).status_code == 404
