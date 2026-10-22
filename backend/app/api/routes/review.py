"""Read-only review API for the verifier dashboard.

Signed-in staff only (password + authenticator code). Verifiers and institution admins see
their own institution's applicants; platform admins see all. Another institution's records
answer 404, exactly like missing ones. Every opened applicant and page image is audited.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_db_session, get_storage, require_staff
from app.models import User
from app.models.enums import UserRole
from app.review import queries
from app.services.audit import record_event
from app.storage import ObjectStorage


def scope(user: User) -> uuid.UUID | None:
    """Institution whose records the user may see; None = all (platform admin only)."""
    if user.role == UserRole.PLATFORM_ADMIN:
        return None
    if user.institution_id is None:  # misconfigured account: never "everything"
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your account has no institution.")
    return user.institution_id


router = APIRouter(prefix="/review")
DbSession = Annotated[Session, Depends(get_db_session)]
Staff = Annotated[User, Depends(require_staff())]
PRIVATE = {"Cache-Control": "private, no-store"}


@router.get("/submissions")
def list_submissions(user: Staff, db: DbSession, response: Response) -> list[dict]:
    response.headers.update(PRIVATE)
    return queries.submission_list(db, scope(user))


@router.get("/submissions/{submission_id}")
def get_submission(
    submission_id: uuid.UUID, user: Staff, db: DbSession, response: Response
) -> dict:
    detail = queries.submission_detail(db, submission_id, scope(user))
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Submission not found")
    record_event(db, action="submission.viewed", entity_type="submission",
                 entity_id=submission_id, actor_user_id=user.id,
                 institution_id=user.institution_id)  # fmt: skip
    db.commit()
    response.headers.update(PRIVATE)
    return detail


@router.get("/pages/{page_id}/image")
def get_page_image(
    page_id: uuid.UUID,
    user: Staff,
    db: DbSession,
    storage: Annotated[ObjectStorage, Depends(get_storage)],
) -> Response:
    data = queries.page_image(db, storage, page_id, scope(user))
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Page image not found")
    record_event(db, action="page_image.viewed", entity_type="document_page", entity_id=page_id,
                 actor_user_id=user.id, institution_id=user.institution_id)  # fmt: skip
    db.commit()
    return Response(data, media_type="image/jpeg", headers=PRIVATE)
