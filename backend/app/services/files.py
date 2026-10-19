"""Storing and reading document files through the encrypted storage backend."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import StoredFile
from app.services.audit import record_event
from app.storage import ObjectStorage


def store_file(
    db: Session,
    storage: ObjectStorage,
    data: bytes,
    *,
    mime_type: str,
    original_filename: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    institution_id: uuid.UUID | None = None,
) -> StoredFile:
    """Encrypt and store `data`, then add its metadata row to the session.

    The blob is written before the DB flush; if the transaction later fails the blob is
    removed so storage and database stay consistent.
    """
    obj = storage.put(data)
    try:
        stored = StoredFile(
            object_key=obj.key,
            sha256=obj.sha256,
            size_bytes=obj.size_bytes,
            mime_type=mime_type,
            original_filename=original_filename,
        )
        db.add(stored)
        db.flush()
        record_event(
            db,
            action="file.stored",
            entity_type="stored_file",
            entity_id=stored.id,
            actor_user_id=actor_user_id,
            institution_id=institution_id,
            details={"size_bytes": obj.size_bytes, "mime_type": mime_type},
        )
        db.flush()
    except Exception:
        storage.delete(obj.key)
        raise
    return stored


def read_file(storage: ObjectStorage, stored: StoredFile) -> bytes:
    return storage.get(stored.object_key)


def find_by_hash(db: Session, sha256: str) -> list[StoredFile]:
    """All stored files with identical content (exact-duplicate detection)."""
    return list(db.scalars(select(StoredFile).where(StoredFile.sha256 == sha256)))
