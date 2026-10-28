"""Administration API. Document types: platform admins. Checklists, and deleting student
data on request: institution admins (their own institution) and platform admins."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.admin import erasure, registry
from app.api.deps import get_db_session, get_storage, require_staff
from app.api.routes.review import _check_scope as check_scope
from app.api.routes.review import _sub as review_sub
from app.models import Applicant, DocumentType, RequirementItem, RequirementSet, User
from app.models.enums import UserRole
from app.services.names import normalize_name
from app.storage import ObjectStorage

router = APIRouter(prefix="/admin")
DbSession = Annotated[Session, Depends(get_db_session)]
Storage = Annotated[ObjectStorage, Depends(get_storage)]
PlatformAdmin = Annotated[User, Depends(require_staff(UserRole.PLATFORM_ADMIN))]
Admin = Annotated[User, Depends(require_staff(UserRole.INSTITUTION_ADMIN, UserRole.PLATFORM_ADMIN))]


class FieldSpec(BaseModel):
    key: str = Field(max_length=63)
    label: str | None = Field(default=None, max_length=80)
    type: str | None = None
    required: bool = False


class NewType(BaseModel):
    code: str = Field(max_length=63)
    name: str = Field(max_length=255)
    category: str = "academic_record"
    fields: list[FieldSpec] = []
    title_phrases: list[str] = []


class TypeChange(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    title_phrases: list[str] | None = None
    fields: list[FieldSpec] | None = None
    active: bool | None = None


class ItemChange(BaseModel):
    label: str | None = Field(default=None, max_length=255)
    required: bool | None = None
    accepted_types: list[str] | None = None


class NewItem(BaseModel):
    key: str = Field(max_length=63)
    label: str = Field(max_length=255)
    accepted_types: list[str]
    required: bool = True


@router.get("/document-types")
def list_types(user: Admin, db: DbSession) -> dict:
    return {
        "types": registry.types_json(db),
        "extractable": {k: label for k, (label, _) in registry.EXTRACTABLE.items()},
    }


@router.post("/document-types", status_code=status.HTTP_201_CREATED)
def create_type(body: NewType, user: PlatformAdmin, db: DbSession) -> dict:
    t = registry.create_type(
        db,
        user,
        body.code,
        body.name,
        body.category,
        [f.model_dump() for f in body.fields],
        body.title_phrases,
    )
    db.commit()
    return {"id": str(t.id), "code": t.code}


@router.patch("/document-types/{type_id}")
def update_type(type_id: uuid.UUID, body: TypeChange, user: PlatformAdmin, db: DbSession) -> dict:
    t = db.get(DocumentType, type_id)
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    registry.update_type(
        db,
        user,
        t,
        name=body.name,
        phrases=body.title_phrases,
        fields=[f.model_dump() for f in body.fields] if body.fields is not None else None,
        active=body.active,
    )
    db.commit()
    return {"id": str(t.id)}


def _scope(user: User):
    if user.role == UserRole.PLATFORM_ADMIN:
        return None
    if user.institution_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your account has no institution.")
    return user.institution_id


@router.get("/requirement-sets")
def requirement_sets(user: Admin, db: DbSession) -> dict:
    return {"requirement_sets": registry.requirement_sets_json(db, _scope(user))}


def _own_set(db: Session, user: User, set_id: uuid.UUID) -> RequirementSet:
    rs = db.get(RequirementSet, set_id)
    inst = _scope(user)
    if rs is None or (inst is not None and rs.institution_id != inst):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return rs


@router.patch("/requirement-items/{item_id}")
def update_item(item_id: uuid.UUID, body: ItemChange, user: Admin, db: DbSession) -> dict:
    item = db.get(RequirementItem, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    _own_set(db, user, item.requirement_set_id)
    registry.update_item(
        db, user, item, label=body.label, required=body.required, accepted=body.accepted_types
    )
    db.commit()
    return {"id": str(item.id)}


@router.post("/requirement-sets/{set_id}/items", status_code=status.HTTP_201_CREATED)
def add_item(set_id: uuid.UUID, body: NewItem, user: Admin, db: DbSession) -> dict:
    rs = _own_set(db, user, set_id)
    item = registry.add_item(db, user, rs, body.key, body.label, body.accepted_types, body.required)
    db.commit()
    return {"id": str(item.id)}


# ---------- deleting student data on request ----------


class ConfirmDelete(BaseModel):
    confirm: str = Field(max_length=255)  # the student's full name, typed by the admin


def _confirmed(applicant: Applicant, typed: str) -> None:
    """Irreversible: the admin types the student's full name, so the wrong record (or a
    mis-click) cannot be deleted."""
    if not typed.strip() or normalize_name(typed) != normalize_name(applicant.full_name or ""):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Type the student's full name exactly as shown to confirm.",
        )


@router.post("/submissions/{submission_id}/delete")
def delete_application(
    submission_id: uuid.UUID, body: ConfirmDelete, user: Admin, db: DbSession, storage: Storage
) -> dict:
    """One application with its documents and files; the person and account stay."""
    sub = review_sub(db, user, submission_id)
    _confirmed(sub.applicant, body.confirm)
    out = erasure.delete_application(db, sub, user)
    db.commit()
    return {**out.counts(), "files_removed": out.purge_files(storage)}


@router.post("/applicants/{applicant_id}/erase")
def erase_applicant(
    applicant_id: uuid.UUID,
    body: ConfirmDelete,
    user: Admin,
    db: DbSession,
    storage: Storage,
    request: Request,
) -> dict:
    """Everything about the person: applications, files, name, email, phone, login."""
    applicant = db.get(Applicant, applicant_id)
    if applicant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    check_scope(user, applicant.institution_id)
    _confirmed(applicant, body.confirm)
    out = erasure.erase_applicant(db, applicant, user, request.app.state.erasure_key)
    db.commit()
    return {**out.counts(), "files_removed": out.purge_files(storage)}
