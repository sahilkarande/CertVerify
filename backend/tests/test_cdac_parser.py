from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.importers.cdac_form.parser import FormFormatError, extract_drive_ids, parse_csv
from tests.cdac_fixtures import HEADER, make_row, write_csv


def test_parses_and_normalises_a_row(tmp_path: Path) -> None:
    result = parse_csv(write_csv(tmp_path / "f.csv", [make_row("100000001")]))
    assert result.warnings == []
    row = result.rows[0]
    assert row.form_number == "100000001"
    assert row.full_name == "Test Student Example"  # whitespace collapsed
    assert row.email == "test.student@example.com"  # trimmed + lower-cased
    assert row.phone == "9000000001"
    assert row.degree_status == "has_degree"
    assert row.claimed_aggregate_percent == 75.0
    assert row.hostel_opted is True
    assert row.submitted_at == datetime(2026, 2, 13, 18, 38, 25, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert row.submitted_at.tzinfo is UTC  # 18:38 IST == 13:08 UTC
    assert row.submitted_at.hour == 13 and row.submitted_at.minute == 8
    assert set(row.drive_files) == {
        "photo",
        "signature",
        "ssc",
        "hsc_or_diploma",
        "semester_marksheets",
        "provisional_or_latest_sem",
        "degree_or_fallback",
    }
    assert row.drive_files["ssc"] == ["100000001-file-11"]


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("https://drive.google.com/open?id=1zOkOABZO45UY9Y1vrWEM", ["1zOkOABZO45UY9Y1vrWEM"]),
        ("https://drive.google.com/file/d/1AbCdEfGhIjK_-x/view", ["1AbCdEfGhIjK_-x"]),
        (
            "https://drive.google.com/open?id=aaaaaaaaaaaa, https://drive.google.com/open?id=bbbbbbbbbbbb",
            ["aaaaaaaaaaaa", "bbbbbbbbbbbb"],
        ),
        ("", []),
        ("not a link", []),
    ],
)
def test_extract_drive_ids(cell: str, expected: list[str]) -> None:
    assert extract_drive_ids(cell) == expected


def test_data_quality_warnings_have_no_personal_data(tmp_path: Path) -> None:
    rows = [
        make_row("100000001", c5="900000001", c21="8.1"),  # 9-digit phone, CGPA-like
        make_row("100000002", c13="Something new", c7="broken-email"),
        make_row("100000003", c21="abc"),
    ]
    result = parse_csv(write_csv(tmp_path / "f.csv", rows))
    text = "\n".join(result.warnings)
    assert "row 2 (form 100000001): mobile number has 9 digits" in text
    assert "aggregate looks like a CGPA" in text
    assert "row 3 (form 100000002): unknown degree option" in text
    assert "email looks invalid" in text
    assert "aggregate percentage is not a number" in text
    assert "Test" not in text and "9000000" not in text and "broken-email" not in text
    assert len(result.rows) == 3  # warnings never drop a row


def test_duplicate_and_missing_form_numbers_skipped(tmp_path: Path) -> None:
    rows = [make_row("100000001"), make_row("100000001"), make_row("", c2="X")]
    result = parse_csv(write_csv(tmp_path / "f.csv", rows))
    assert [r.form_number for r in result.rows] == ["100000001"]
    assert any("duplicate form number" in w for w in result.warnings)
    assert any("missing form number" in w for w in result.warnings)


def test_empty_upload_slot_is_simply_absent(tmp_path: Path) -> None:
    result = parse_csv(write_csv(tmp_path / "f.csv", [make_row("100000001", c20="")]))
    assert "degree_or_fallback" not in result.rows[0].drive_files
    assert result.warnings == []


def test_missing_column_is_a_hard_error(tmp_path: Path) -> None:
    header = [h for h in HEADER if not h.startswith("Email ID")]
    with pytest.raises(FormFormatError, match="Email ID"):
        parse_csv(write_csv(tmp_path / "f.csv", [], header=header))


def test_real_export_parses_cleanly() -> None:
    """Runs only where the real (git-ignored) export exists. Asserts counts only."""
    path = Path(__file__).resolve().parents[2] / "data_feb_26.csv"
    if not path.exists():
        pytest.skip("real export not present")
    result = parse_csv(path)
    assert len(result.rows) == 82
    assert sum(len(ids) for r in result.rows for ids in r.drive_files.values()) == 574
    assert all(r.degree_status for r in result.rows)
