from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.importers.cdac_form.importer import import_form
from app.importers.cdac_form.parser import parse_csv
from app.models import Applicant, AuditEvent, ImportFileRef, RequirementSet, Submission
from app.models.enums import ImportFileStatus, SubmissionStatus
from tests.cdac_fixtures import make_row, write_csv


def count(db: Session, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


def test_import_creates_records(session: Session, tmp_path: Path) -> None:
    parsed = parse_csv(
        write_csv(tmp_path / "f.csv", [make_row("100000001"), make_row("100000002")])
    )
    report = import_form(session, parsed)
    session.commit()

    assert (report.applicants_created, report.file_refs_created) == (2, 14)
    rs = session.scalars(select(RequirementSet)).one()
    assert [i.key for i in rs.items] == [
        "photo",
        "signature",
        "ssc",
        "hsc_or_diploma",
        "semester_marksheets",
        "provisional_or_latest_sem",
        "degree_or_fallback",
    ]
    assert rs.items[3].accepted_type_codes == ["hsc_marksheet", "diploma_marksheet"]

    sub = session.scalars(select(Submission)).first()
    assert sub.status is SubmissionStatus.SUBMITTED
    assert sub.attributes["degree_status"] == "has_degree"
    assert sub.attributes["claimed_aggregate_percent"] == 75.0
    assert {r.status for r in session.scalars(select(ImportFileRef))} == {ImportFileStatus.PENDING}
    assert session.scalars(select(AuditEvent)).one().action == "import.form"


def test_reimport_is_idempotent_and_updates(session: Session, tmp_path: Path) -> None:
    import_form(session, parse_csv(write_csv(tmp_path / "a.csv", [make_row("100000001")])))
    session.commit()

    changed = make_row("100000001", c2="Corrected Name", c21="76.5")
    report = import_form(session, parse_csv(write_csv(tmp_path / "b.csv", [changed])))
    session.commit()

    assert (report.applicants_created, report.applicants_updated) == (0, 1)
    assert (report.file_refs_created, report.file_refs_existing) == (0, 7)
    assert count(session, Applicant) == 1
    assert count(session, Submission) == 1
    assert count(session, ImportFileRef) == 7
    assert count(session, RequirementSet) == 1
    assert session.scalars(select(Applicant)).one().full_name == "Corrected Name"
    attrs = session.scalars(select(Submission)).one().attributes
    assert attrs["claimed_aggregate_percent"] == 76.5


def test_same_drive_file_in_two_students_is_flagged(session: Session, tmp_path: Path) -> None:
    shared = "https://drive.google.com/open?id=SHAREDFILE123"
    rows = [make_row("100000001", c11=shared), make_row("100000002", c11=shared)]
    report = import_form(session, parse_csv(write_csv(tmp_path / "f.csv", rows)))
    assert report.file_refs_created == 13
    assert any("already linked to another submission" in w for w in report.warnings)
