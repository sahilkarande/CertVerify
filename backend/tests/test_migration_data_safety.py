"""Regression: SQLite batch migrations must not cascade-delete or null child rows.

A batch migration rebuilds a table (copy, drop, rename). With foreign keys enforced, the drop
fired ON DELETE CASCADE on page_ocr and SET NULL on import_file_refs.document_id.
"""

from pathlib import Path

from alembic import command
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.db.session import build_engine
from app.models import Document, DocumentPage, ImportFileRef, PageOcr
from app.models.enums import ImportFileStatus, OcrStatus
from tests.factories import make_document, make_stored_file, make_submission
from tests.test_migrations import alembic_config


def test_downgrade_and_upgrade_keep_child_rows(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "head")

    engine = build_engine(url)
    with sessionmaker(bind=engine)() as db:
        sub = make_submission(db)
        doc = make_document(db, sub, make_stored_file(db))
        page = DocumentPage(document_id=doc.id, page_number=1)
        db.add(page)
        db.flush()
        db.add(PageOcr(page_id=page.id, engine="t", engine_version="1", pipeline_version=1,
                       status=OcrStatus.OK))  # fmt: skip
        db.add(ImportFileRef(submission_id=sub.id, slot_key="ssc", source="gdrive",
                             external_id="abc", status=ImportFileStatus.FETCHED,
                             document_id=doc.id))  # fmt: skip
        db.commit()
    engine.dispose()

    # Downgrading past the extractor_version migration rebuilds document_pages in batch mode.
    command.downgrade(cfg, "c5f4bc176401")  # before the page extractor_version migration
    engine = build_engine(url)
    with engine.connect() as conn:  # the downgrade really happened (not silently rolled back)
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(document_pages)")}
    engine.dispose()
    assert "extractor_version" not in cols
    command.upgrade(cfg, "head")

    engine = build_engine(url)
    with sessionmaker(bind=engine)() as db:
        assert db.scalar(select(func.count()).select_from(PageOcr)) == 1
        assert db.scalar(select(func.count()).select_from(Document)) == 1
        ref = db.scalars(select(ImportFileRef)).one()
        assert ref.document_id is not None
    engine.dispose()
