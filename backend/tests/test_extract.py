import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.extract.evaluate import evaluate
from app.extract.fields import extract_page, known_name, match_form_name
from app.extract.layout import Layout
from app.extract.pipeline import (
    EXTRACTOR_VERSION,
    extract_page_fields,
    extraction_report,
    pages_needing_extraction,
)
from app.extract.values import (
    clean_name,
    fix_digits,
    parse_gpa,
    parse_ids,
    parse_percentage,
    parse_result,
    parse_total,
)
from app.models import Document, DocumentPage, ExtractedField, PageOcr
from app.models.enums import FieldSource, OcrStatus
from app.seeds.document_types import seed_document_types
from tests.factories import make_stored_file, make_submission

# ---------- values ----------


def test_fix_digits_repairs_number_like_tokens_only() -> None:
    assert fix_digits("294D") == ("2940", True)
    assert fix_digits("8.6O") == ("8.60", True)
    assert fix_digits("PASS") == ("PASS", False)
    assert fix_digits("SOBHABAI") == ("SOBHABAI", False)


@pytest.mark.parametrize(
    ("text", "value", "repaired"),
    [
        (": 8.72", "8.72", False),
        ("8,2", "8.2", False),
        ("SGPA 10", "10", False),
        ("8.0O", "8", True),
        ("872", "8.72", True),  # decimal point lost on dot-matrix print
    ],
)
def test_parse_gpa(text: str, value: str, repaired: bool) -> None:
    assert parse_gpa(text) == (value, repaired)


@pytest.mark.parametrize("text", ["", "TOTAL CREDITS 44", "12.5", "0.00"])
def test_parse_gpa_rejects_non_gpa(text: str) -> None:
    assert parse_gpa(text) is None


def test_parse_percentage() -> None:
    assert parse_percentage(":64.78%") == ("64.78", False)
    assert parse_percentage("80 %") == ("80", False)
    assert parse_percentage("12") is None  # below any passing percentage


@pytest.mark.parametrize(
    ("text", "obtained", "maximum"),
    [
        ("(Out Of 500):400", "400", "500"),
        (": 478/675", "478", "675"),
        ("294D/4500", "2940", "4500"),
        ("900 757 32", "757", "900"),  # maximum, obtained, credits
        ("MAX/MIN/OBT: 1500/375/ 1107", "1107", "1500"),  # 375 is the passing minimum
        ("650 399 THREE HUNDRED AND", "399", "650"),
        ("455", "455", None),
    ],
)
def test_parse_total(text: str, obtained: str, maximum: str | None) -> None:
    assert parse_total(text)[0] == (obtained, maximum)


def test_parse_result_and_ids_and_names() -> None:
    assert parse_result("Remark : Successful") == ("PASS", False)
    assert parse_result("PASS IN SECOND DIVISION") == ("PASS", False)
    assert parse_result("FAILS") == ("FAIL", False)
    assert parse_result("RESULT") is None
    assert [i for i, _ in parse_ids("NO.S190944271 CENTRE")] == ["S190944271"]
    assert parse_ids("SEM 2 2023") == []
    assert clean_name("Shri/Smt PATIL ASHA MOTHER'S NAME SUNITA") == "PATIL ASHA"
    assert clean_name("MEENA M PILLAI") == "MEENA M PILLAI"


# ---------- layout + finders on synthetic pages ----------


def page(*boxes) -> list:
    """Boxes as (text, x, y[, w]) -> stored OCR words, one line key per box."""
    words = []
    for n, (text, x, y, *w) in enumerate(boxes):
        width = w[0] if w else 12 * len(text)
        words.append([text, 95.0, x, y, width, 20, f"p.{n}"])
    words.append(["FOOTER", 95.0, 0, 1000, 60, 20, "p.end"])  # gives the page its height
    return words


def test_layout_rows_and_neighbours() -> None:
    lay = Layout(page(("SGPA", 100, 200), ("8.72", 300, 204), ("CGPA", 100, 300)))
    sgpa = next(b for b in lay.boxes if b.text == "SGPA")
    assert [b.text for b in lay.right_of(sgpa)] == ["8.72"]
    assert [b.text for b in lay.below(sgpa)] == ["CGPA"]
    assert [lay.row_text(r) for r in lay.rows][:2] == ["SGPA 8.72", "CGPA"]


def test_value_in_same_box_right_and_below() -> None:
    got = extract_page(page(
        ("SGPA: 8.72", 100, 300),
        ("CGPA", 400, 300, 60), ("8.41", 520, 300),
        ("Percentage", 100, 500, 120), ("RESULT", 400, 500, 80),
        ("80.50", 110, 530), ("PASS", 405, 530),
    ))  # fmt: skip
    assert (got["sgpa"].value, got["sgpa"].evidence) == ("8.72", ["same_box"])
    assert (got["cgpa"].value, got["cgpa"].evidence) == ("8.41", ["right"])
    assert (got["percentage"].value, got["percentage"].evidence) == ("80.5", ["below"])
    assert got["result"].value == "PASS"


def test_latest_semester_wins_and_header_values_are_distrusted() -> None:
    got = extract_page(page(
        ("NAAC Accredited with CGPA 3.09", 100, 20, 400),
        ("SGPI: 6.17", 100, 700), ("SGPI: 6.55", 400, 700),
        ("SGPI: 7.95", 100, 730), ("SGPI: 8.00", 400, 730),
        ("CGPI: 7.09", 100, 800),
    ))  # fmt: skip
    assert got["sgpa"].value == "8"  # last row, rightmost
    assert got["cgpa"].value == "7.09"


def test_value_in_the_page_header_gets_low_confidence() -> None:
    got = extract_page(page(("NAAC Accredited with CGPA 3.09", 100, 20, 400)))
    assert got["cgpa"].confidence <= 0.6  # about the institution, not the student


def test_value_before_label_in_a_sentence() -> None:
    got = extract_page(page(("MAY 2023 WITH 8.91 CGPI.", 100, 600, 400)))
    assert (got["cgpa"].value, got["cgpa"].evidence) == ("8.91", ["before_label"])


def test_total_confirmed_by_percentage() -> None:
    got = extract_page(page(
        ("Percentage", 100, 600, 120), ("61.38", 300, 600),
        ("Total Marks", 600, 615, 130), ("650", 800, 600), ("399", 900, 600),
    ))  # fmt: skip
    assert got["total_marks_obtained"].value == "399"
    assert got["total_marks_max"].value == "650"
    assert got["total_marks_obtained"].confidence == got["percentage"].confidence == 0.95
    assert "matches_percentage" in got["total_marks_obtained"].evidence


def test_total_credits_is_not_total_marks() -> None:
    got = extract_page(page(("Total Credits Acquired 128", 100, 600, 300)))
    assert "total_marks_obtained" not in got


def test_identifiers_with_split_labels() -> None:
    got = extract_page(page(
        ("SEAT", 100, 300, 50), ("NO.S190944271", 160, 300),
        ("PERM.REG. NO:72260254F", 600, 300, 250),
    ))  # fmt: skip
    assert got["seat_number"].value == "S190944271"
    assert got["student_id"].value == "72260254F"


def test_form_name_found_despite_ocr_damage() -> None:
    lay = Layout(page(("NAME", 100, 300, 50), ("IGOSAVI AKSHAYABALKRISHNA SUJATA", 200, 300)))
    c = known_name(lay, "Akshaya Gosavi")
    assert c.value == "GOSAVI AKSHAYA BALKRISHNA SUJATA"
    assert c.confidence == 0.95
    assert match_form_name("GADHE FOOJA", "Pooja Gadhe") == ("GADHE POOJA", 2, 2)


def test_partial_match_keeps_the_printed_spelling() -> None:
    # "ASHA" is close to the form's "AASTHA", but the other form words are absent: this may
    # be someone else's document, so the name must stay exactly as printed.
    got = extract_page(
        page(("NAME", 100, 300, 50), ("TESTER ASHA RAMESH", 200, 300)),
        applicant_name="Aastha Satishrao Kulkarni",
    )
    assert got["candidate_name"].value == "TESTER ASHA RAMESH"


def test_name_differing_from_the_form_is_kept_for_review() -> None:
    got = extract_page(page(("Name of Student", 100, 300, 160), ("RAHUL VERMA", 300, 300)),
                       applicant_name="Asha Patil")  # fmt: skip
    assert got["candidate_name"].value == "RAHUL VERMA"
    assert got["candidate_name"].confidence <= 0.6


def test_mother_name_label_variants() -> None:
    assert extract_page(page(("MOTHER : BABITA", 100, 300)))["mother_name"].value == "BABITA"
    got = extract_page(page(("CANDIDATE'S MOTHER'S NAME", 100, 300, 300), ("Asmita", 500, 300)))
    assert got["mother_name"].value == "ASMITA"
    got = extract_page(page(("S/0 SHASHIKANT (Father) and SONALI (Mother),", 100, 300, 500)))
    assert got["mother_name"].value == "SONALI"


def test_table_row_is_not_a_name() -> None:
    got = extract_page(page(
        ("NAME OF THE CANDIDATE", 100, 300, 250), ("14945 ENGINEERING MATHEMATICS", 100, 330, 300),
    ))  # fmt: skip
    assert "candidate_name" not in got


# ---------- pipeline with database ----------

SEM_WORDS = page(
    ("NAME", 50, 100, 50), ("PATIL ASHA RAMESH", 150, 100),
    ("SEAT NO: B190244471", 50, 140, 200), ("PRN: 72145244C", 400, 140, 150),
    ("SGPA: 8.72", 50, 600), ("CGPA: 8.41", 400, 600),
)  # fmt: skip


@pytest.fixture
def typed_page(session: Session):
    seed_document_types(session)
    session.flush()
    from app.models import DocumentType

    sem = session.scalar(select(DocumentType).where(DocumentType.code == "semester_marksheet"))
    sub = make_submission(session)
    sub.applicant.full_name = "Asha Patil"
    doc = Document(submission_id=sub.id, file_id=make_stored_file(session).id, slot_key="sem")
    session.add(doc)
    session.flush()
    p = DocumentPage(document_id=doc.id, page_number=1, detected_type_id=sem.id,
                     classification_confidence=0.9, semesters=[3])  # fmt: skip
    session.add(p)
    session.flush()
    session.add(PageOcr(page_id=p.id, engine="paddle", engine_version="3", pipeline_version=1,
                        status=OcrStatus.OK, rotation=90, text="", words=SEM_WORDS))  # fmt: skip
    session.commit()
    return p


def _fields(db: Session, p: DocumentPage) -> dict:
    rows = db.scalars(select(ExtractedField).where(ExtractedField.page_id == p.id))
    return {r.field_key: r for r in rows}


def test_extract_stores_schema_fields_per_page(session: Session, typed_page) -> None:
    assert pages_needing_extraction(session) == [typed_page.id]
    result = extract_page_fields(session, typed_page)
    session.commit()
    f = _fields(session, typed_page)
    assert {k: v.value for k, v in f.items()} == {
        "candidate_name": "PATIL ASHA RAMESH",
        "seat_number": "B190244471",
        "student_id": "72145244C",
        "sgpa": "8.72",
        "cgpa": "8.41",
        "semester": "3",
    }
    assert f["sgpa"].machine_value == "8.72" and f["sgpa"].source is FieldSource.MACHINE
    assert f["sgpa"].bbox["rotation"] == 90 and f["sgpa"].bbox["engine"] == "paddle"
    assert set(result.missing_required) == {"university", "program", "exam_session"}
    assert typed_page.extractor_version == EXTRACTOR_VERSION
    assert pages_needing_extraction(session) == []
    report = extraction_report(session)["semester_marksheet"]
    assert report["pages"] == 1 and report["sgpa"] == 1


def test_rerun_replaces_machine_fields_and_keeps_corrections(session: Session, typed_page) -> None:
    extract_page_fields(session, typed_page)
    session.commit()
    sgpa = _fields(session, typed_page)["sgpa"]
    sgpa.value, sgpa.source = "8.27", FieldSource.HUMAN  # a reviewer's correction
    session.commit()

    extract_page_fields(session, typed_page)
    session.commit()
    f = _fields(session, typed_page)
    assert (f["sgpa"].value, f["sgpa"].machine_value) == ("8.27", "8.72")
    assert len(session.scalars(select(ExtractedField)).all()) == 6  # no duplicates


def test_unclassified_page_gets_no_fields(session: Session, typed_page) -> None:
    typed_page.detected_type_id = None
    session.commit()
    assert extract_page_fields(session, typed_page).stored == []
    assert pages_needing_extraction(session) == []


def test_evaluation_reports_counts_only(session: Session, typed_page, tmp_path: Path) -> None:
    labels = tmp_path / "fields.json"
    item = {"page_id": str(typed_page.id), "name": "PATIL ASHA", "sgpa": "8.72", "cgpa": "9.99",
            "ids": ["B190244471"]}  # fmt: skip
    labels.write_text(json.dumps([item]))
    report = evaluate(session, labels)
    text = "\n".join(report.lines())
    assert "PATIL" not in text and "8.72" not in text
    c = report.counts
    assert (c["name ok"], c["sgpa ok"], c["cgpa wrong"], c["ids ok"]) == (1, 1, 1, 1)
