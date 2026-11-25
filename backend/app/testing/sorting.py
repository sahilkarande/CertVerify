"""Bulk uploads (Testing Lab): files dropped without choosing a checklist item go into the
``auto`` slot and are sorted once recognised, so a tester can drop a student's whole folder.

* Photo and signature cannot be recognised from text: the file name decides ("photo",
  "passport", "sign"...), at upload.
* Everything else is read first; then the document moves to the checklist item whose main
  type it is (a semester marksheet to "all semester marksheets", not to the degree item that
  also accepts one as a fallback). A file nothing recognised stays "unsorted" for the
  reviewer to look at.
"""

import re
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Document, DocumentType, RequirementItem

AUTO_SLOT = "auto"

_PHOTO = re.compile(r"photo|passport|\bpic\b|selfie|face", re.I)
_SIGN = re.compile(r"sign", re.I)


def slot_from_filename(filename: str | None, item_keys: set[str]) -> str | None:
    """Identity images, which are not read, by their file name."""
    name = filename or ""
    if "signature" in item_keys and _SIGN.search(name):
        return "signature"
    if "photo" in item_keys and _PHOTO.search(name):
        return "photo"
    return None


def assign_slot(db: Session, doc: Document) -> str | None:
    """Move an ``auto`` document to the item for what it was recognised as. Returns the new
    slot (None: unrecognised, left unsorted). Caller flushes/commits."""
    if doc.slot_key != AUTO_SLOT:
        return None
    codes = dict(db.execute(select(DocumentType.id, DocumentType.code)).all())
    found = Counter(codes[p.detected_type_id] for p in doc.pages if p.detected_type_id)
    found.pop("supporting_page", None)
    if not found:
        return None
    code = found.most_common(1)[0][0]
    items = db.scalars(
        select(RequirementItem)
        .where(RequirementItem.requirement_set_id == doc.submission.requirement_set_id)
        .order_by(RequirementItem.position)
    ).all()
    accepting = [i for i in items if code in (i.accepted_type_codes or [])]
    if not accepting:
        return None
    # The item whose main (first) accepted type this is; else the first that accepts it.
    main = next((i for i in accepting if (i.accepted_type_codes or [None])[0] == code), None)
    doc.slot_key = (main or accepting[0]).key
    return doc.slot_key
