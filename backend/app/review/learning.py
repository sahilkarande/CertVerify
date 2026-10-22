"""Verifier corrections as training data, and accuracy measured on real use.

Every value a verifier confirmed or corrected is a labelled example: ``machine_value`` is
what the extractor read, ``value`` what is actually on the document. Values a verifier added
were missed entirely.
"""

import json
from collections import Counter
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DocumentPage, DocumentType, ExtractedField, PageOcr
from app.models.enums import FieldSource


def corrections_report(db: Session) -> dict[str, Counter]:
    """Per field: confirmed (machine was right), corrected (wrong), added (missed)."""
    out: dict[str, Counter] = {}
    for f in db.scalars(select(ExtractedField).where(ExtractedField.source == FieldSource.HUMAN)):
        c = out.setdefault(f.field_key, Counter())
        if f.machine_value is None:
            c["added"] += 1
        elif f.machine_value == f.value:
            c["confirmed"] += 1
        else:
            c["corrected"] += 1
        if f.machine_value is not None and (f.confidence or 0) >= 0.9:
            c["high_conf"] += 1
            c["high_conf_ok"] += f.machine_value == f.value
    return out


def export_corrections(db: Session, path: Path) -> int:
    """Write one JSON line per verified value (contains personal data: keep it private)."""
    codes = dict(db.execute(select(DocumentType.id, DocumentType.code)).all())
    rows = db.execute(
        select(ExtractedField, DocumentPage.detected_type_id, PageOcr.engine)
        .join(DocumentPage, DocumentPage.id == ExtractedField.page_id)
        .outerjoin(PageOcr, PageOcr.page_id == DocumentPage.id)
        .where(ExtractedField.source == FieldSource.HUMAN)
    ).all()
    seen = set()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for f, type_id, engine in rows:
            if f.id in seen:
                continue
            seen.add(f.id)
            fh.write(
                json.dumps(
                    {
                        "page_id": str(f.page_id),
                        "document_type": codes.get(type_id),
                        "field": f.field_key,
                        "machine_value": f.machine_value,
                        "value": f.value,
                        "confidence": f.confidence,
                        "bbox": f.bbox,
                        "ocr_engine": engine,
                    }
                )
                + "\n"
            )
    path.chmod(0o600)
    return len(seen)
