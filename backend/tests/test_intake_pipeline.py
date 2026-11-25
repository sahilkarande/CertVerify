import io

import cv2
import numpy as np
import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.intake import pipeline
from app.intake.pipeline import PROCESSING_VERSION, process_document
from app.models import Document, DocumentPage, StoredFile
from app.models.enums import DocumentStatus, QualityVerdict
from app.seeds.document_types import seed_document_types
from app.services.files import read_file, store_file
from app.storage import EncryptedLocalStorage
from tests.factories import make_submission
from tests.intake_fixtures import pad_into, text_page, to_jpeg, to_pdf


def blob_count(storage: EncryptedLocalStorage) -> int:
    return len([p for p in storage._root.rglob("*") if p.is_file()])


def add_document(db: Session, storage, data: bytes, slot: str = "ssc", sub=None) -> Document:
    sub = sub or make_submission(db)
    f = store_file(db, storage, data, mime_type="application/octet-stream")
    doc = Document(submission_id=sub.id, file_id=f.id, slot_key=slot)
    db.add(doc)
    db.commit()
    return doc


def test_multi_page_pdf_is_processed(session: Session, storage: EncryptedLocalStorage) -> None:
    pages = [Image.fromarray(text_page()), Image.fromarray(pad_into(text_page(), 250))]
    buf = io.BytesIO()
    pages[0].save(buf, "PDF", resolution=150.0, save_all=True, append_images=pages[1:])
    padded = buf.getvalue() + b"\x00" * 4096  # like the phone-app files in the seed data
    doc = add_document(session, storage, padded)

    result = process_document(session, storage, doc)

    assert result.status is DocumentStatus.PROCESSED and result.pages == 2
    session.refresh(doc)
    assert doc.status is DocumentStatus.PROCESSED
    assert doc.page_count == 2 and doc.processing_version == PROCESSING_VERSION
    assert doc.processed_at is not None and doc.processing_error is None
    p1, p2 = doc.pages
    assert (p1.page_number, p2.page_number) == (1, 2)
    assert p1.quality_verdict is QualityVerdict.OK
    assert p2.crop_box is not None  # white margin trimmed
    img = cv2.imdecode(np.frombuffer(read_file(storage, p1.image_file), np.uint8), cv2.IMREAD_COLOR)
    assert img.shape[:2] == (p1.height_px, p1.width_px)


def test_up_to_date_documents_are_skipped(session: Session, storage) -> None:
    doc = add_document(session, storage, to_pdf(text_page()))
    assert process_document(session, storage, doc) is not None
    assert process_document(session, storage, doc) is None


def test_force_reprocess_replaces_page_images(session: Session, storage) -> None:
    doc = add_document(session, storage, to_pdf(text_page()))
    process_document(session, storage, doc)
    old_image_id = doc.pages[0].image_file_id
    blobs = blob_count(storage)

    process_document(session, storage, doc, force=True)
    session.refresh(doc)
    assert len(doc.pages) == 1 and doc.pages[0].image_file_id != old_image_id
    assert session.get(StoredFile, old_image_id) is None
    assert blob_count(storage) == blobs  # old page image deleted, new one written


def test_unreadable_upload_marks_document_failed(session: Session, storage) -> None:
    doc = add_document(session, storage, b"%PDF-1.4\n1 0 obj << broken")
    result = process_document(session, storage, doc)
    session.refresh(doc)
    assert result.status is DocumentStatus.FAILED and result.error_code == "pdf_unreadable"
    assert (doc.status, doc.processing_error, doc.pages) == (
        DocumentStatus.FAILED,
        "pdf_unreadable",
        [],
    )
    assert process_document(session, storage, doc) is None  # not retried endlessly


def test_unexpected_error_rolls_back_everything(
    session: Session, storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = add_document(session, storage, to_pdf(text_page()))
    process_document(session, storage, doc)
    before_pages = [(p.id, p.image_file_id) for p in doc.pages]
    blobs = blob_count(storage)

    def boom(*a, **k):
        raise RuntimeError("out of memory")

    monkeypatch.setattr(pipeline, "assess_document", boom)
    with pytest.raises(RuntimeError):
        process_document(session, storage, doc, force=True)

    session.expire_all()
    doc = session.get(Document, doc.id)
    assert [(p.id, p.image_file_id) for p in doc.pages] == before_pages  # old result intact
    assert blob_count(storage) == blobs  # including its image blobs
    assert read_file(storage, doc.pages[0].image_file)  # still readable


def test_identity_slot_uses_identity_checks(session: Session, storage) -> None:
    from app.models import RequirementItem

    seed_document_types(session)
    sub = make_submission(session)
    item = session.scalars(
        select(RequirementItem).where(RequirementItem.requirement_set_id == sub.requirement_set_id)
    ).first()
    item.key, item.accepted_type_codes = "photo", ["passport_photo"]
    session.commit()

    # A plain photo would fail text-quality checks; as an identity image it only needs size.
    photo = cv2.GaussianBlur(text_page(400, 520), (0, 0), 8)
    doc = add_document(session, storage, to_jpeg(photo), slot="photo", sub=sub)
    process_document(session, storage, doc)
    page = session.scalars(select(DocumentPage).where(DocumentPage.document_id == doc.id)).one()
    assert page.quality_verdict is QualityVerdict.OK
    assert page.crop_box is None and page.deskew_angle == 0.0
