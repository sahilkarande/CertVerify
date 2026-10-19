import hashlib
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.importers.cdac_form.importer import import_form
from app.importers.cdac_form.parser import parse_csv
from app.importers.gdrive.client import DriveFileError, DriveFileMeta
from app.importers.gdrive.downloader import download_pending
from app.models import Document, ImportFileRef, StoredFile
from app.models.enums import ImportFileStatus
from app.services.files import read_file
from app.storage import EncryptedLocalStorage
from tests.cdac_fixtures import make_row, write_csv


class FakeDrive:
    """In-memory Drive. Missing ids behave like 404; `broken` ids corrupt their payload."""

    def __init__(self, files: dict[str, bytes], broken: set[str] = frozenset()) -> None:
        self.files = files
        self.broken = broken
        self.downloads = 0

    def get_metadata(self, file_id: str) -> DriveFileMeta:
        if file_id not in self.files:
            raise DriveFileError("Drive API HTTP 404", permanent=True)
        data = self.files[file_id]
        return DriveFileMeta(
            id=file_id,
            name=f"{file_id}.pdf",
            mime_type="application/pdf",
            size=len(data),
            md5=hashlib.md5(data).hexdigest(),
        )

    def download(self, file_id: str) -> bytes:
        self.downloads += 1
        data = self.files[file_id]
        return data[::-1] if file_id in self.broken else data


@pytest.fixture
def imported(session: Session, tmp_path: Path) -> list[str]:
    import_form(session, parse_csv(write_csv(tmp_path / "f.csv", [make_row("100000001")])))
    session.commit()
    return list(session.scalars(select(ImportFileRef.external_id)))


def all_files(ids: list[str]) -> dict[str, bytes]:
    return {i: f"%PDF content of {i}".encode() for i in ids}


def test_downloads_all_and_creates_documents(
    session: Session, storage: EncryptedLocalStorage, imported: list[str]
) -> None:
    drive = FakeDrive(all_files(imported))
    report = download_pending(session, storage, drive)

    assert (report.fetched, report.failed) == (7, 0)
    docs = session.scalars(select(Document)).all()
    assert len(docs) == 7
    doc = next(d for d in docs if d.slot_key == "ssc")
    assert doc.source_ref == "gdrive:100000001-file-11"
    assert read_file(storage, doc.file) == b"%PDF content of 100000001-file-11"
    refs = session.scalars(select(ImportFileRef)).all()
    assert all(r.status is ImportFileStatus.FETCHED and r.document_id for r in refs)


def test_rerun_downloads_nothing_new(
    session: Session, storage: EncryptedLocalStorage, imported: list[str]
) -> None:
    drive = FakeDrive(all_files(imported))
    download_pending(session, storage, drive)
    report = download_pending(session, storage, drive)
    assert (report.fetched, report.failed, drive.downloads) == (0, 0, 7)


def test_limit_allows_resuming(
    session: Session, storage: EncryptedLocalStorage, imported: list[str]
) -> None:
    drive = FakeDrive(all_files(imported))
    assert download_pending(session, storage, drive, limit=3).fetched == 3
    assert download_pending(session, storage, drive).fetched == 4


def test_failures_recorded_and_retryable(
    session: Session, storage: EncryptedLocalStorage, imported: list[str]
) -> None:
    files = all_files(imported)
    missing = imported[0]
    corrupt = imported[1]
    del files[missing]
    drive = FakeDrive(files, broken={corrupt})

    report = download_pending(session, storage, drive)
    assert (report.fetched, report.failed) == (5, 2)
    by_id = {r.external_id: r for r in session.scalars(select(ImportFileRef))}
    assert by_id[missing].status is ImportFileStatus.FAILED
    assert "404" in by_id[missing].last_error
    assert "checksum mismatch" in by_id[corrupt].last_error
    # Nothing half-stored for the failures.
    assert len(session.scalars(select(StoredFile)).all()) == 5
    assert len([p for p in storage._root.rglob("*") if p.is_file()]) == 5

    # Plain re-run skips failed refs; --retry-failed picks them up once fixed.
    assert download_pending(session, storage, drive).fetched == 0
    drive.files[missing] = b"%PDF now shared"
    drive.broken = set()
    report = download_pending(session, storage, drive, retry_failed=True)
    assert (report.fetched, report.failed) == (2, 0)
    assert by_id[missing].attempts == 2


def test_oversized_file_rejected_before_download(
    session: Session, storage: EncryptedLocalStorage, imported: list[str]
) -> None:
    drive = FakeDrive(all_files(imported))
    report = download_pending(session, storage, drive, max_bytes=10)
    assert report.failed == 7
    assert drive.downloads == 0
