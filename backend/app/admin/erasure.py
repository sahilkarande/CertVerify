"""Deleting student data on request: one application, or the whole person.

* ``delete_application``: one application (submission) with its documents, pages, readings,
  values, issues, messages and files. The person (name, email, account) stays.
* ``erase_applicant``: everything about the person: every application as above, the
  applicant record (name, email, phone, form number), their student login account,
  sessions and sign-in codes. A keyed hash of their form number is kept so that re-running
  the form import does not bring them back; nothing else about them remains.

What is never deleted: the audit trail (it holds ids and counts, never names or values),
and copies outside the live database (backups in data/backups, evaluation label files):
those must be handled by whoever runs the server.

Files are removed from storage only after the database change is committed (see
``Erasure.purge_files``): if the transaction failed, rows would otherwise point at missing
files. A file another applicant also uploaded is kept.
"""

import hashlib
import hmac
import uuid
from dataclasses import dataclass, field

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import (
    Applicant,
    Document,
    DocumentPage,
    ErasedApplicant,
    LoginCode,
    StoredFile,
    Submission,
    User,
)
from app.models.enums import UserRole
from app.services.audit import record_event
from app.storage import ObjectStorage


def erasure_token(key: bytes, institution_id: uuid.UUID, external_ref: str) -> str:
    """Keyed hash of a form number: lets the importer recognise an erased person without
    keeping the number itself (9-digit numbers are too few for a plain hash to hide)."""
    msg = f"{institution_id}:{external_ref.strip()}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def is_erased(db: Session, key: bytes, institution_id: uuid.UUID, external_ref: str) -> bool:
    token = erasure_token(key, institution_id, external_ref)
    return db.get(ErasedApplicant, token) is not None


@dataclass
class Erasure:
    submissions: int = 0
    documents: int = 0
    account_deleted: bool = False
    file_ids: set[uuid.UUID] = field(default_factory=set)
    _keys: list[str] = field(default_factory=list)

    def counts(self) -> dict:
        return {
            "submissions": self.submissions,
            "documents": self.documents,
            "account_deleted": self.account_deleted,
        }

    def collect_unused_files(self, db: Session) -> None:
        """After the rows are gone: delete the file records nobody uses any more and remember
        their storage keys (deleted from storage by ``purge_files`` after the commit)."""
        for fid in self.file_ids:
            used = db.scalar(select(Document.id).where(Document.file_id == fid).limit(1)) or (
                db.scalar(select(DocumentPage.id).where(DocumentPage.image_file_id == fid).limit(1))
            )
            f = db.get(StoredFile, fid)
            if f is not None and not used:
                self._keys.append(f.object_key)
                db.delete(f)
        db.flush()

    def purge_files(self, storage: ObjectStorage) -> int:
        """Call after commit. Returns how many stored files were removed."""
        for key in self._keys:
            storage.delete(key)
        n, self._keys = len(self._keys), []
        return n


def _delete_submission(db: Session, sub: Submission, out: Erasure) -> None:
    docs = db.scalars(select(Document).where(Document.submission_id == sub.id)).all()
    for d in docs:
        out.file_ids.add(d.file_id)
        out.file_ids.update(p.image_file_id for p in d.pages if p.image_file_id)
    out.documents += len(docs)
    out.submissions += 1
    # The database cascades: documents -> pages -> OCR, fields, jobs; issues, messages and
    # Drive import references belong to the submission.
    db.execute(delete(Submission).where(Submission.id == sub.id))


def delete_application(db: Session, sub: Submission, actor: User) -> Erasure:
    """Caller commits, then calls ``purge_files``."""
    out = Erasure()
    applicant = sub.applicant
    _delete_submission(db, sub, out)
    db.expire_all()
    out.collect_unused_files(db)
    from app.extract.pipeline import corroborate_ids

    corroborate_ids(db, applicant.id)  # their other applications' IDs lose this confirmation
    record_event(
        db,
        action="application.deleted",
        entity_type="submission",
        entity_id=sub.id,
        actor_user_id=actor.id,
        institution_id=applicant.institution_id,
        details={"applicant": str(applicant.id), **out.counts()},
    )
    return out


def erase_applicant(db: Session, applicant: Applicant, actor: User, key: bytes) -> Erasure:
    """Caller commits, then calls ``purge_files``."""
    out = Erasure()
    applicant_id, institution_id = applicant.id, applicant.institution_id
    for sub in db.scalars(select(Submission).where(Submission.applicant_id == applicant.id)).all():
        _delete_submission(db, sub, out)

    user = db.get(User, applicant.user_id) if applicant.user_id else None
    other = (
        db.scalar(
            select(func.count()).where(Applicant.user_id == user.id, Applicant.id != applicant.id)
        )
        if user
        else 0
    )
    emails = {e.strip().lower() for e in (applicant.email, user.email if user else None) if e}
    if applicant.external_ref:
        token = erasure_token(key, institution_id, applicant.external_ref)
        if db.get(ErasedApplicant, token) is None:
            db.add(ErasedApplicant(token=token, institution_id=institution_id))
    db.execute(delete(Applicant).where(Applicant.id == applicant_id))
    # Only a student account belonging to nobody else goes: never a staff account.
    if user is not None and user.role == UserRole.STUDENT and not other:
        db.execute(delete(User).where(User.id == user.id))  # sessions cascade
        out.account_deleted = True
    if emails and (out.account_deleted or user is None):
        db.execute(delete(LoginCode).where(LoginCode.email.in_(emails)))
    db.expire_all()
    out.collect_unused_files(db)
    record_event(
        db,
        action="applicant.erased",
        entity_type="applicant",
        entity_id=applicant_id,
        actor_user_id=actor.id,
        institution_id=institution_id,
        details=out.counts(),  # never the name or email
    )
    return out
