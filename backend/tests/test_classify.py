import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.classify import rules
from app.classify.evaluate import evaluate
from app.classify.pipeline import (
    CLASSIFIER_VERSION,
    classify_document,
    documents_needing_classification,
    type_id_map,
)
from app.classify.rules import classify_text, find_semesters, to_semester
from app.classify.text import best_ratio, compact, header_text
from app.models import Document, DocumentPage, PageOcr, RequirementItem
from app.models.enums import AcademicLevel, ClassificationMethod, OcrStatus
from app.seeds.document_types import seed_document_types
from tests.factories import make_stored_file, make_submission


def words_for(lines: list[str]) -> list:
    """OCR-style word list: one line per entry, top to bottom."""
    out = []
    for i, line in enumerate(lines):
        for j, w in enumerate(line.split()):
            out.append([w, 90, j * 50, i * 40, 40, 20, f"1.{i}.1"])
    return out


def classify_lines(lines: list[str]) -> rules.Classification:
    return classify_text("\n".join(lines), words_for(lines))


FILLER = [f"row {i} subject marks" for i in range(12)]  # pushes tables below the header


# ---------- helpers ----------


@pytest.mark.parametrize(
    ("token", "value"),
    [
        ("VIII", 8),
        ("VILL", 8),
        ("LV", 4),
        ("1V", 4),
        ("IL", 2),
        ("SIXTH", 6),
        ("3", 3),
        ("12", None),
    ],
)
def test_to_semester_tolerates_ocr_numerals(token: str, value: int | None) -> None:
    assert to_semester(token) == value


@pytest.mark.parametrize(
    ("header", "sems"),
    [
        ("STATEMENT OF GRADES SEM : VI ROLL NO", [6]),
        ("B.E.(SEM.-VILL) (CBCS)", [8]),
        ("BACHELOR OF TECHNOLOGY - IV SEM EXAMINATION", [4]),
        ("SEMESTER : THIRD", [3]),
        ("TERM : SEVENTH EXAMINATION", [7]),
        ("STATEMENT OF MARKS/GRADES FOR S.E. (2019 CREDIT PAT.)", [3, 4]),
        ("STATEMENT OF GRADES FOR B.E.(2019 COURSE)", [7, 8]),
        ("GRADE CARD", []),
    ],
)
def test_find_semesters(header: str, sems: list[int]) -> None:
    assert find_semesters(header) == sems


def test_compact_and_best_ratio_tolerate_ocr_damage() -> None:
    assert compact("Edu cation, 10 + 2!") == "EDUCATION10+2"
    damaged = compact("ONDARY SCHOO! CERTIFICATE EXAMINATION")
    assert best_ratio(damaged, "SECONDARYSCHOOLCERTIFICATEEXAMINATION") >= 0.8
    assert best_ratio("ABC", "SECONDARY") < 0.5


def test_header_text_ignores_lower_part() -> None:
    lines = ["TOP TITLE", *FILLER, "BOTTOM SUMMARY"]
    head = header_text(words_for(lines))
    assert "TOP TITLE" in head and "BOTTOM SUMMARY" not in head


# ---------- content rules ----------


def test_board_name_alone_does_not_make_a_12th_marksheet() -> None:
    c = classify_lines(
        ["MAHARASHTRA STATE BOARD OF SECONDARY AND HIGHER SECONDARY EDUCATION, PUNE",
         "SECONDARY SCHOOL CERTIFICATE EXAMINATION - STATEMENT OF MARKS", *FILLER]
    )  # fmt: skip
    assert (c.type_code, c.level) == (rules.SSC, AcademicLevel.SCHOOL)


def test_hsc_title_wins() -> None:
    c = classify_lines(
        ["MAHARASHTRA STATE BOARD OF SECONDARY AND HIGHER SECONDARY EDUCATION, PUNE",
         "HIGHER SECONDARY CERTIFICATE EXAMINATION - STATEMENT OF MARKS", *FILLER]
    )  # fmt: skip
    assert c.type_code == rules.HSC and not c.fuzzy


def test_ocr_damaged_ssc_title_matches_fuzzily_not_as_hsc() -> None:
    c = classify_lines(["SECONDARY SCHOO! CERTIFICATE EX4MINATION - STATEMENT", *FILLER])
    assert (c.type_code, c.fuzzy, c.confidence) == (rules.SSC, True, 0.7)


def test_10_plus_2_mentioned_on_a_degree_passing_certificate_is_not_hsc() -> None:
    c = classify_lines(
        ["SAVITRIBAI PHULE PUNE UNIVERSITY", "CERTIFICATE (PASSING CERTIFICATE)",
         "THIS IS TO CERTIFY THAT", *FILLER, "ENTRY POINT: 10+2(HSC)"]
    )  # fmt: skip
    assert c.type_code == rules.PROVISIONAL


def test_semester_marksheet_with_level_and_semester() -> None:
    c = classify_lines(
        ["UNIVERSITY OF MUMBAI GRADE CARD", "M.SC. (PART II)(SEM.IV)(CBCGS)",
         "COURSE CODE COURSE TITLE CREDITS", *FILLER, "SGPI 8.1"]
    )  # fmt: skip
    assert (c.type_code, c.semesters, c.level) == (rules.SEMESTER, [4], AcademicLevel.PG)


@pytest.mark.parametrize(
    ("lines", "code"),
    [
        (["MAHARASHTRA STATE BOARD OF TECHNICAL EDUCATION", "STATEMENT OF MARKS", "FIFTH SEMESTER"],
         rules.DIPLOMA),
        (["PROVISIONAL CERTIFICATE", "THIS IS TO CERTIFY THAT", "ELIGIBLE FOR THE SAID DEGREE"],
         rules.PROVISIONAL),
        (["I CERTIFY THAT", "RATHOD GAJENDRA", "PASSED THE B.E. DEGREE EXAMINATION", "6.49 CGPI"],
         rules.PROVISIONAL),
        (["UNIVERSITY OF MUMBAI", "CONFER THE DEGREE OF", "BACHELOR OF ENGINEERING"],
         rules.DEGREE),
        (["CONVOCATION NUMBER EN24-18254", "SPEED POST BARCODE"], rules.SUPPORTING),
        (["SOMETHING ILLEGIBLE"], None),
    ],
)  # fmt: skip
def test_types(lines: list[str], code: str | None) -> None:
    assert classify_lines(lines).type_code == code


def test_a_marksheet_saying_provisional_stays_a_marksheet() -> None:
    lines = ["STATEMENT OF GRADES SEMESTER : VI", "COURSE CODE COURSE TITLE CREDITS",
             "SGPA 8.1 CGPA 7.9 GRADE CARD", "RESULT IS PROVISIONAL"]  # fmt: skip
    assert classify_lines(lines).type_code == rules.SEMESTER


# ---------- pipeline ----------


@pytest.fixture
def type_ids(session: Session) -> dict:
    seed_document_types(session)
    session.commit()
    return type_id_map(session)


def _doc_with_pages(db: Session, page_lines: list[list[str]], slot: str, sub=None) -> Document:
    sub = sub or make_submission(db, ref=str(100000000 + len(page_lines) * 1000 + hash(slot) % 997))
    f = make_stored_file(db, sha=f"{slot:0<64}"[:64], key=None)
    doc = Document(submission_id=sub.id, file_id=f.id, slot_key=slot)
    db.add(doc)
    db.flush()
    for n, lines in enumerate(page_lines, start=1):
        page = DocumentPage(document_id=doc.id, page_number=n)
        db.add(page)
        db.flush()
        text = "\n".join(lines)
        db.add(PageOcr(page_id=page.id, engine="tesseract", engine_version="5", pipeline_version=1,
                       status=OcrStatus.OK, text=text, words=words_for(lines)))  # fmt: skip
    db.commit()
    return doc


SEM_PAGE = ["STATEMENT OF GRADES SEMESTER : III", "COURSE CODE CREDITS SGPA", *FILLER]


def test_document_pages_classified_with_continuation(session: Session, type_ids: dict) -> None:
    doc = _doc_with_pages(session, [SEM_PAGE, ["ILLEGIBLE BACK SIDE"]], "semester_marksheets")
    result = classify_document(session, doc, type_ids)
    session.commit()

    p1, p2 = session.scalars(
        select(DocumentPage)
        .where(DocumentPage.document_id == doc.id)
        .order_by(DocumentPage.page_number)
    ).all()
    assert p1.detected_type_id == type_ids[rules.SEMESTER] and p1.semesters == [3]
    assert p1.classification_method is ClassificationMethod.CONTENT_EXACT
    assert p1.classification_confidence == 0.9 and p1.classifier_version == CLASSIFIER_VERSION
    assert p2.detected_type_id == type_ids[rules.SEMESTER]
    assert p2.classification_method is ClassificationMethod.CONTINUATION
    assert p2.classification_confidence == 0.5
    assert p2.classification_evidence[0] == "continuation_of_page:1"
    assert doc.detected_type_id == type_ids[rules.SEMESTER]  # all pages agree
    assert result.methods == {"content_exact": 1, "continuation": 1}


def test_slot_hint_only_for_single_type_slots(session: Session, type_ids: dict) -> None:
    sub = make_submission(session, ref="200000001")
    item = session.scalars(
        select(RequirementItem).where(RequirementItem.requirement_set_id == sub.requirement_set_id)
    ).first()  # factory set: "ssc" accepts only ssc_marksheet; "hsc_or_diploma" accepts two
    assert item.key == "ssc"
    ssc_doc = _doc_with_pages(session, [["ILLEGIBLE"]], "ssc", sub=sub)
    either_doc = _doc_with_pages(session, [["ILLEGIBLE"]], "hsc_or_diploma", sub=sub)

    classify_document(session, ssc_doc, type_ids)
    classify_document(session, either_doc, type_ids)
    session.commit()
    ssc_page = ssc_doc.pages[0]
    assert ssc_page.detected_type_id == type_ids[rules.SSC]
    assert ssc_page.classification_method is ClassificationMethod.SLOT_HINT
    assert ssc_page.classification_confidence == 0.4
    assert either_doc.pages[0].detected_type_id is None  # ambiguous slot: stays unknown
    assert either_doc.pages[0].classification_method is None


def test_mixed_document_has_no_single_type(session: Session, type_ids: dict) -> None:
    prov = ["PROVISIONAL CERTIFICATE", "THIS IS TO CERTIFY THAT"]
    doc = _doc_with_pages(session, [SEM_PAGE, prov], "semester_marksheets")
    classify_document(session, doc, type_ids)
    assert doc.detected_type_id is None


def test_needing_classification_and_evaluation(
    session: Session, type_ids: dict, tmp_path: Path
) -> None:
    doc = _doc_with_pages(session, [SEM_PAGE], "semester_marksheets")
    assert documents_needing_classification(session) == [doc.id]
    classify_document(session, doc, type_ids)
    session.commit()
    assert documents_needing_classification(session) == []
    assert documents_needing_classification(session, force=True) == [doc.id]

    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps([
        {"page_id": str(doc.pages[0].id), "split": "A", "type": "semester", "sems": [3]}
    ]))  # fmt: skip
    report = evaluate(session, labels)
    assert report.by_split == {"A": [1, 1]} and report.semester == [1, 1]
    assert report.wrong_type == report.unknown == 0
