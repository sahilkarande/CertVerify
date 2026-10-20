from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.importers.cdac_form import schema
from app.importers.cdac_form.folder import (
    FolderImportError,
    apply_import,
    plan_mapping,
    read_mapping,
    respondent_from_filename,
    scan_folder,
    write_mapping,
)
from app.importers.cdac_form.importer import import_form
from app.importers.cdac_form.parser import parse_csv
from app.models import Document, ImportFileRef, StoredFile
from app.models.enums import ImportFileStatus
from app.services.files import read_file
from app.services.filetypes import sniff_mime
from app.services.names import normalize_name
from app.storage import EncryptedLocalStorage
from tests.cdac_fixtures import make_row, write_csv

JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-body"
PNG = b"\x89PNG\r\n\x1a\n" + b"png-body"
HEIC = b"\x00\x00\x00\x18ftypheic" + b"heic-body"
PADDED_PDF = b"%PDF-1.7 body %%EOF" + b"\x00" * 300

# Fictional students: (form number, form name, email, Google account name in file names)
STUDENTS = [
    ("100000001", "Asha Ramesh Patil", "asha.patil@example.com", "Asha Ramesh Patil"),  # exact
    ("100000002", "Rohan Sunil Deshmukh", "rohan.d@example.com", "rohan deshmukh"),  # all words
    ("100000003", "Tejas Vikas Bhosle", "tejasbhosale@example.com", "Tejas Bhosale"),  # similar
    ("100000004", "Kiran Mahesh Joshi", "kiranj@example.com", "kj gamer"),  # nickname
]


def folder_name(slot: schema.UploadSlot) -> str:
    # The real export replaces "/" with "-" in question titles; prefixes are unaffected.
    return f"{slot.header_prefix} (rest of question) (File responses)".replace("/", "-")


def make_export(root: Path, students=STUDENTS) -> Path:
    for slot in schema.UPLOAD_SLOTS:
        d = root / folder_name(slot)
        d.mkdir(parents=True)
        for form_no, _, _, account in students:
            if slot.key in ("photo", "signature"):
                content, ext = JPEG + form_no.encode(), "jpg"
            else:
                content, ext = b"%PDF-1.4 " + f"{slot.key}-{form_no}".encode(), "pdf"
            (d / f"my {slot.key} file - {account}.{ext}").write_bytes(content)
    return root


@pytest.fixture
def imported_form(session: Session, tmp_path: Path) -> None:
    rows = [make_row(no, c2=name, c7=email) for no, name, email, _ in STUDENTS]
    import_form(session, parse_csv(write_csv(tmp_path / "f.csv", rows)))
    session.commit()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("10 marksheet - Asha Patil.pdf", "Asha Patil"),
        ("10th - Abc Def (1).pdf", "Abc Def"),
        ("a - b - Last Part.jpeg", "Last Part"),
        ("no-separator.pdf", None),
    ],
)
def test_respondent_from_filename(name: str, expected: str | None) -> None:
    assert respondent_from_filename(name) == expected


@pytest.mark.parametrize(
    ("data", "mime"),
    [
        (b"%PDF-1.7...", "application/pdf"),
        (JPEG, "image/jpeg"),
        (PNG, "image/png"),
        (HEIC, "image/heic"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"hello", "application/octet-stream"),
    ],
)
def test_sniff_mime(data: bytes, mime: str) -> None:
    assert sniff_mime(data) == mime


def test_normalize_name() -> None:
    assert normalize_name("  KULKARNI  Aastha-Satishrao. ") == "kulkarni aastha satishrao"
    assert normalize_name("Zoë  O'Neil") == "zoe o neil"


def test_scan_groups_files_per_respondent(tmp_path: Path) -> None:
    scanned = scan_folder(make_export(tmp_path / "export"))
    assert len(scanned.files) == 4
    assert all(len(slots) == 7 for slots in scanned.files.values())
    assert scanned.warnings == []


def test_scan_rejects_unknown_or_missing_folders(tmp_path: Path) -> None:
    root = make_export(tmp_path / "export")
    (root / "Something unexpected").mkdir()
    with pytest.raises(FolderImportError, match="does not match"):
        scan_folder(root)


def test_plan_methods_and_confirmation(
    session: Session, tmp_path: Path, imported_form: None
) -> None:
    rows = {
        r.form_number: r for r in plan_mapping(session, scan_folder(make_export(tmp_path / "e")))
    }
    assert rows["100000001"].method == "exact"
    assert rows["100000002"].method == "all_words"
    assert rows["100000003"].method == "similar_and_email"
    assert rows["100000004"].method == "elimination"
    confirmed = {no: r.confirmed for no, r in rows.items()}
    assert confirmed == {
        "100000001": True,
        "100000002": True,
        "100000003": True,
        "100000004": False,
    }


def test_apply_refuses_unconfirmed_mapping(
    session: Session, storage: EncryptedLocalStorage, tmp_path: Path, imported_form: None
) -> None:
    scanned = scan_folder(make_export(tmp_path / "e"))
    with pytest.raises(FolderImportError, match="not confirmed"):
        apply_import(session, storage, scanned, plan_mapping(session, scanned))
    assert session.scalars(select(Document)).all() == []


def confirmed_mapping(session: Session, scanned, path: Path):
    rows = plan_mapping(session, scanned)
    for r in rows:
        r.confirmed = True
    write_mapping(path, rows)  # round-trip through the CSV the user edits
    return read_mapping(path)


def test_apply_imports_everything_and_links_drive_refs(
    session: Session, storage: EncryptedLocalStorage, tmp_path: Path, imported_form: None
) -> None:
    root = make_export(tmp_path / "e")
    # Real-world oddities: wrong extension, zero-padded PDF, HEIC photo.
    photo_dir = root / folder_name(schema.UPLOAD_SLOTS[0])
    (photo_dir / "my photo file - Asha Ramesh Patil.jpg").unlink()
    (photo_dir / "pic - Asha Ramesh Patil.png").write_bytes(JPEG)
    (photo_dir / "my photo file - rohan deshmukh.jpg").unlink()
    (photo_dir / "IMG - rohan deshmukh.heic").write_bytes(HEIC)
    sem_dir = root / folder_name(schema.UPLOAD_SLOTS[4])
    (sem_dir / "my semester_marksheets file - kj gamer.pdf").write_bytes(PADDED_PDF)

    scanned = scan_folder(root)
    report = apply_import(
        session, storage, scanned, confirmed_mapping(session, scanned, tmp_path / "m.csv")
    )

    assert (report.students, report.files_stored, report.drive_refs_linked) == (4, 28, 28)
    assert report.by_mime == {"application/pdf": 20, "image/jpeg": 7, "image/heic": 1}
    refs = session.scalars(select(ImportFileRef)).all()
    assert {r.status for r in refs} == {ImportFileStatus.FETCHED}

    padded = session.scalars(
        select(StoredFile).where(
            StoredFile.original_filename == "my semester_marksheets file - kj gamer.pdf"
        )
    ).one()
    assert read_file(storage, padded) == PADDED_PDF  # stored byte-for-byte as uploaded

    photo = session.scalars(
        select(StoredFile).where(StoredFile.original_filename == "pic - Asha Ramesh Patil.png")
    ).one()
    assert photo.mime_type == "image/jpeg"  # content wins over the .png extension


def test_apply_twice_is_idempotent(
    session: Session, storage: EncryptedLocalStorage, tmp_path: Path, imported_form: None
) -> None:
    scanned = scan_folder(make_export(tmp_path / "e"))
    mapping = confirmed_mapping(session, scanned, tmp_path / "m.csv")
    apply_import(session, storage, scanned, mapping)
    blobs_before = len([p for p in storage._root.rglob("*") if p.is_file()])

    report = apply_import(session, storage, scanned, mapping)
    assert (report.files_stored, report.files_already_present) == (0, 28)
    assert len(session.scalars(select(Document)).all()) == 28
    assert len([p for p in storage._root.rglob("*") if p.is_file()]) == blobs_before


def test_mapping_must_cover_every_respondent_once(
    session: Session, storage: EncryptedLocalStorage, tmp_path: Path, imported_form: None
) -> None:
    scanned = scan_folder(make_export(tmp_path / "e"))
    rows = confirmed_mapping(session, scanned, tmp_path / "m.csv")
    rows[1].form_number = rows[0].form_number  # two respondents -> same student
    with pytest.raises(FolderImportError, match="used 2 times"):
        apply_import(session, storage, scanned, rows)

    with pytest.raises(FolderImportError, match="not in the mapping"):
        apply_import(
            session, storage, scanned, confirmed_mapping(session, scanned, tmp_path / "x")[:3]
        )


def test_failed_student_leaves_no_partial_data(
    session: Session,
    storage: EncryptedLocalStorage,
    tmp_path: Path,
    imported_form: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanned = scan_folder(make_export(tmp_path / "e"))
    mapping = confirmed_mapping(session, scanned, tmp_path / "m.csv")
    import app.importers.cdac_form.folder as folder_mod

    real_link = folder_mod._link_drive_ref
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 10:  # fail partway through the 2nd student
            raise RuntimeError("disk full")
        return real_link(*args, **kwargs)

    monkeypatch.setattr(folder_mod, "_link_drive_ref", flaky)
    with pytest.raises(RuntimeError):
        apply_import(session, storage, scanned, mapping)

    docs = session.scalars(select(Document)).all()
    assert len(docs) == 7  # first student committed, second fully rolled back
    assert len([p for p in storage._root.rglob("*") if p.is_file()]) == 7

    monkeypatch.setattr(folder_mod, "_link_drive_ref", real_link)
    report = apply_import(session, storage, scanned, mapping)  # resume
    assert (report.files_stored, report.files_already_present) == (21, 7)


def test_rerun_relinks_refs_that_lost_their_document(
    session: Session, storage: EncryptedLocalStorage, tmp_path: Path, imported_form: None
) -> None:
    scanned = scan_folder(make_export(tmp_path / "e"))
    mapping = confirmed_mapping(session, scanned, tmp_path / "m.csv")
    apply_import(session, storage, scanned, mapping)
    for ref in session.scalars(select(ImportFileRef)):
        ref.document_id = None  # what the faulty SQLite migration did
    session.commit()

    report = apply_import(session, storage, scanned, mapping)
    assert report.drive_refs_relinked == 28
    refs = session.scalars(select(ImportFileRef)).all()
    assert all(r.document_id is not None for r in refs)
    assert apply_import(session, storage, scanned, mapping).drive_refs_relinked == 0
