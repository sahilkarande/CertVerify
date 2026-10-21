"""Read-only review API for the verifier dashboard.

There is no sign-in yet (Phase 9), so these routes answer only requests from this machine,
and never in production. Opening a submission or a page image is recorded in the audit log.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_app_settings, get_db_session, get_storage
from app.core.config import Settings
from app.review import queries
from app.services.audit import record_event
from app.storage import ObjectStorage

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def local_only(request: Request, settings: Annotated[Settings, Depends(get_app_settings)]) -> None:
    host = request.client.host if request.client else None
    if settings.environment == "production" or host not in LOCAL_HOSTS:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "The review dashboard is only available on this machine."
        )


router = APIRouter(prefix="/review", dependencies=[Depends(local_only)])
DbSession = Annotated[Session, Depends(get_db_session)]
PRIVATE = {"Cache-Control": "private, no-store"}


@router.get("/submissions")
def list_submissions(db: DbSession, response: Response) -> list[dict]:
    response.headers.update(PRIVATE)
    return queries.submission_list(db)


@router.get("/submissions/{submission_id}")
def get_submission(submission_id: uuid.UUID, db: DbSession, response: Response) -> dict:
    detail = queries.submission_detail(db, submission_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Submission not found")
    record_event(db, action="submission.viewed", entity_type="submission",
                 entity_id=submission_id)  # fmt: skip
    db.commit()
    response.headers.update(PRIVATE)
    return detail


@router.get("/pages/{page_id}/image")
def get_page_image(
    page_id: uuid.UUID, db: DbSession, storage: Annotated[ObjectStorage, Depends(get_storage)]
) -> Response:
    data = queries.page_image(db, storage, page_id)
    if data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Page image not found")
    record_event(db, action="page_image.viewed", entity_type="document_page", entity_id=page_id)
    db.commit()
    return Response(data, media_type="image/jpeg", headers=PRIVATE)
