"""Adding and editing document types and requirement checklists — data, not code.

A new type needs: a name, the values to read (chosen from what the extractor can find, or
entered by verifiers), and title phrases that identify it. Built-in types keep their field
lists (maintained by the seed); admins can still add phrases to them.
"""

import re

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import DocumentPage, DocumentType, RequirementItem, RequirementSet, User
from app.models.enums import DocumentCategory
from app.services.audit import record_event

# Values the extractor can find on any document type (others are entered by verifiers).
EXTRACTABLE = {
    "candidate_name": ("Candidate name", "string"),
    "mother_name": ("Mother's name", "string"),
    "seat_number": ("Seat number", "string"),
    "student_id": ("PRN / enrolment number", "string"),
    "sgpa": ("SGPA / SPI", "number"),
    "cgpa": ("CGPA / CPI", "number"),
    "percentage": ("Percentage", "number"),
    "total_marks_obtained": ("Total marks obtained", "number"),
    "result": ("Result", "string"),
}
SLUG = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
EDITABLE_CATEGORIES = {DocumentCategory.ACADEMIC_RECORD, DocumentCategory.SUPPORTING}


def _bad(msg: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, msg)


def _clean_phrases(phrases: list[str]) -> list[str]:
    out = []
    for p in phrases:
        p = " ".join(p.split())[:120]
        if len(re.sub(r"[^A-Za-z]", "", p)) < 6:
            raise _bad(f"Title phrase {p!r} is too short to identify a document reliably.")
        out.append(p)
    return out[:20]


def _fields(fields: list[dict]) -> list[dict]:
    out, seen = [], set()
    for f in fields:
        key = str(f.get("key", "")).strip()
        if not SLUG.match(key) or key in seen:
            raise _bad(f"Invalid or repeated value key {key!r}.")
        seen.add(key)
        label, ftype = EXTRACTABLE.get(key, (str(f.get("label") or key).strip()[:80], "string"))
        out.append(
            {
                "key": key,
                "label": f.get("label") or label,
                "type": f.get("type") if f.get("type") in ("string", "number") else ftype,
                "required": bool(f.get("required", False)),
            }
        )
    return out


def types_json(db: Session) -> list[dict]:
    pages = dict(
        db.execute(
            select(DocumentPage.detected_type_id, func.count()).group_by(
                DocumentPage.detected_type_id
            )
        ).all()
    )
    rows = db.scalars(select(DocumentType).order_by(DocumentType.builtin.desc(), DocumentType.name))
    return [
        {
            "id": str(t.id),
            "code": t.code,
            "name": t.name,
            "category": t.category.value,
            "builtin": t.builtin,
            "active": t.is_active,
            "fields": t.field_schema,
            "title_phrases": (t.recognition or {}).get("title_phrases", []),
            "pages": pages.get(t.id, 0),
        }
        for t in rows
    ]


def create_type(
    db: Session,
    user: User,
    code: str,
    name: str,
    category: str,
    fields: list[dict],
    phrases: list[str],
) -> DocumentType:
    if not SLUG.match(code):
        raise _bad("Code: lower-case letters, digits and _ (3-63 characters), e.g. transcript.")
    if db.scalar(select(DocumentType).where(DocumentType.code == code)):
        raise HTTPException(status.HTTP_409_CONFLICT, "A document type with this code exists.")
    cat = DocumentCategory(category)
    if cat not in EDITABLE_CATEGORIES:
        raise _bad("Choose academic record or supporting document.")
    t = DocumentType(
        code=code,
        name=" ".join(name.split())[:255] or code,
        category=cat,
        field_schema=_fields(fields),
        recognition={"title_phrases": _clean_phrases(phrases)},
        builtin=False,
    )
    db.add(t)
    db.flush()
    record_event(
        db,
        action="document_type.created",
        entity_type="document_type",
        entity_id=t.id,
        actor_user_id=user.id,
        details={"code": code},
    )
    return t


def update_type(
    db: Session,
    user: User,
    t: DocumentType,
    *,
    name: str | None,
    phrases: list[str] | None,
    fields: list[dict] | None,
    active: bool | None,
) -> DocumentType:
    if name is not None:
        t.name = " ".join(name.split())[:255] or t.name
    if phrases is not None:
        t.recognition = {**(t.recognition or {}), "title_phrases": _clean_phrases(phrases)}
    if fields is not None:
        if t.builtin:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "The values read from built-in types are maintained with the platform.",
            )
        t.field_schema = _fields(fields)
    if active is not None:
        if t.builtin and not active:
            raise HTTPException(status.HTTP_409_CONFLICT, "Built-in types cannot be switched off.")
        t.is_active = active
    record_event(
        db,
        action="document_type.updated",
        entity_type="document_type",
        entity_id=t.id,
        actor_user_id=user.id,
        details={"code": t.code},
    )
    db.flush()
    return t


# ---------- requirement checklists ----------


def requirement_sets_json(db: Session, institution_id) -> list[dict]:
    q = select(RequirementSet).order_by(RequirementSet.name)
    if institution_id is not None:
        q = q.where(RequirementSet.institution_id == institution_id)
    return [
        {
            "id": str(rs.id),
            "code": rs.code,
            "name": rs.name,
            "active": rs.is_active,
            "items": [
                {
                    "id": str(i.id),
                    "key": i.key,
                    "label": i.label,
                    "required": i.is_required,
                    "accepted_types": i.accepted_type_codes,
                    "condition": i.condition,
                }
                for i in rs.items
            ],
        }
        for rs in db.scalars(q)
    ]


def _check_types(db: Session, codes: list[str]) -> list[str]:
    known = set(db.scalars(select(DocumentType.code).where(DocumentType.is_active.is_(True))))
    unknown = [c for c in codes if c not in known]
    if unknown or not codes:
        raise _bad("Accepted types must be existing, active document types.")
    return list(dict.fromkeys(codes))


def update_item(
    db: Session,
    user: User,
    item: RequirementItem,
    *,
    label: str | None,
    required: bool | None,
    accepted: list[str] | None,
) -> RequirementItem:
    if label is not None:
        item.label = " ".join(label.split())[:255] or item.label
    if required is not None:
        item.is_required = required
    if accepted is not None:
        item.accepted_type_codes = _check_types(db, accepted)
    record_event(
        db,
        action="requirement_item.updated",
        entity_type="requirement_item",
        entity_id=item.id,
        actor_user_id=user.id,
        institution_id=item.requirement_set.institution_id,
    )
    db.flush()
    return item


def add_item(
    db: Session,
    user: User,
    rs: RequirementSet,
    key: str,
    label: str,
    accepted: list[str],
    required: bool,
) -> RequirementItem:
    if not SLUG.match(key) or any(i.key == key for i in rs.items):
        raise _bad("Key: a new lower-case name, e.g. transcript.")
    item = RequirementItem(
        key=key,
        label=" ".join(label.split())[:255] or key,
        position=len(rs.items),
        accepted_type_codes=_check_types(db, accepted),
        is_required=required,
    )
    rs.items.append(item)
    db.flush()
    record_event(
        db,
        action="requirement_item.added",
        entity_type="requirement_item",
        entity_id=item.id,
        actor_user_id=user.id,
        institution_id=rs.institution_id,
    )
    return item
