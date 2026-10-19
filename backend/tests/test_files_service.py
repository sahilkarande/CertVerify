import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditEvent, StoredFile
from app.services.files import find_by_hash, read_file, store_file
from app.storage import EncryptedLocalStorage


def test_store_and_read(session: Session, storage: EncryptedLocalStorage) -> None:
    stored = store_file(
        session, storage, b"marksheet bytes", mime_type="application/pdf", original_filename="a.pdf"
    )
    session.commit()

    assert read_file(storage, stored) == b"marksheet bytes"
    event = session.scalars(select(AuditEvent)).one()
    assert event.action == "file.stored"
    assert event.entity_id == str(stored.id)
    # Audit details must not carry file names or content.
    assert "a.pdf" not in str(event.details)


def test_exact_duplicates_found_by_hash(session: Session, storage: EncryptedLocalStorage) -> None:
    a = store_file(session, storage, b"same", mime_type="application/pdf")
    b = store_file(session, storage, b"same", mime_type="application/pdf")
    store_file(session, storage, b"different", mime_type="application/pdf")
    session.commit()
    assert {f.id for f in find_by_hash(session, a.sha256)} == {a.id, b.id}


def test_blob_removed_when_db_write_fails(
    session: Session, storage: EncryptedLocalStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(session, "flush", boom)
    with pytest.raises(RuntimeError):
        store_file(session, storage, b"orphan?", mime_type="application/pdf")
    monkeypatch.undo()
    session.rollback()

    assert session.scalars(select(StoredFile)).all() == []
    assert not [p for p in storage._root.rglob("*") if p.is_file()]
