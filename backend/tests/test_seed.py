from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DocumentType
from app.models.enums import DocumentCategory
from app.seeds.document_types import DOCUMENT_TYPES, seed_document_types


def test_seed_is_idempotent(session: Session) -> None:
    assert seed_document_types(session) == (len(DOCUMENT_TYPES), 0)
    assert seed_document_types(session) == (0, 0)
    assert len(session.scalars(select(DocumentType)).all()) == len(DOCUMENT_TYPES)


def test_seed_updates_changed_definitions(session: Session) -> None:
    seed_document_types(session)
    row = session.scalars(select(DocumentType).where(DocumentType.code == "ssc_marksheet")).one()
    row.name = "outdated"
    session.flush()
    assert seed_document_types(session) == (0, 1)
    assert row.name != "outdated"


def test_registry_covers_seed_dataset_types(session: Session) -> None:
    seed_document_types(session)
    types = {t.code: t for t in session.scalars(select(DocumentType))}
    assert {
        "ssc_marksheet",
        "hsc_marksheet",
        "diploma_marksheet",
        "semester_marksheet",
        "provisional_degree_certificate",
        "degree_certificate",
        "passport_photo",
        "signature",
    } <= set(types)
    assert types["signature"].category is DocumentCategory.IDENTITY_IMAGE
    sem_keys = {f["key"] for f in types["semester_marksheet"].field_schema}
    assert {"semester", "sgpa", "cgpa", "student_id"} <= sem_keys


def test_field_keys_unique_within_each_type() -> None:
    for spec in DOCUMENT_TYPES:
        keys = [f["key"] for f in spec["field_schema"]]
        assert len(keys) == len(set(keys)), spec["code"]
