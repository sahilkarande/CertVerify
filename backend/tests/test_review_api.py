import io

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import load_storage_key
from app.main import create_app
from app.models import (
    AuditEvent,
    Document,
    DocumentPage,
    DocumentType,
    ExtractedField,
    PageOcr,
    ValidationIssue,
)
from app.models.enums import IssueSeverity, IssueStatus, OcrStatus
from app.seeds.document_types import seed_document_types
from app.services.files import store_file
from app.storage import EncryptedLocalStorage
from tests.factories import make_stored_file, make_submission

API = "/api/v1/review"


@pytest.fixture
def seeded(session: Session, test_settings: Settings) -> dict:
    """One applicant with a landscape page read at 90 degrees, one field and two issues."""
    storage = EncryptedLocalStorage(test_settings.storage_dir, load_storage_key(test_settings))
    seed_document_types(session)
    sem = session.scalar(select(DocumentType).where(DocumentType.code == "semester_marksheet"))
    sub = make_submission(session)
    sub.applicant.full_name = "Asha Patil"
    doc = Document(
        submission_id=sub.id, file_id=make_stored_file(session).id, slot_key="semester_marksheets"
    )
    session.add(doc)
    img = np.zeros((1000, 2000, 3), np.uint8)  # landscape scan of a portrait page
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, "PNG")
    stored = store_file(session, storage, buf.getvalue(), mime_type="image/png")
    page = DocumentPage(
        document_id=doc.id,
        page_number=1,
        image_file_id=stored.id,
        width_px=2000,
        height_px=1000,
        detected_type_id=sem.id,
        classification_confidence=0.9,
        semesters=[3],
    )
    session.add(page)
    session.flush()
    # Read upright (turned 90 degrees): the OCR image is 1000 wide x 2000 tall.
    session.add(
        PageOcr(
            page_id=page.id,
            engine="paddle",
            engine_version="3",
            pipeline_version=1,
            status=OcrStatus.OK,
            rotation=90,
            words=[],
        )
    )
    session.add(
        ExtractedField(
            document_id=doc.id,
            page_id=page.id,
            field_key="sgpa",
            machine_value="8.72",
            value="8.72",
            confidence=0.95,
            bbox={"x": 100, "y": 500, "w": 200, "h": 40, "rotation": 90, "engine": "paddle"},
        )
    )
    session.add(
        ValidationIssue(
            submission_id=sub.id,
            document_id=doc.id,
            code="name_partial_match",
            severity=IssueSeverity.WARNING,
            message="Name partly matches.",
            details={"key": "name:x", "rules_version": 1, "page": 1},
        )
    )
    session.add(
        ValidationIssue(
            submission_id=sub.id,
            code="duplicate_upload",
            severity=IssueSeverity.ERROR,
            status=IssueStatus.DISMISSED,
            message="Dismissed by a verifier.",
            details={"key": "dup"},
        )
    )
    session.commit()
    return {"sub": str(sub.id), "page": str(page.id)}


def test_list_shows_verdict_from_open_issues_only(client: TestClient, seeded: dict) -> None:
    resp = client.get(f"{API}/submissions")
    assert resp.status_code == 200 and resp.headers["cache-control"] == "private, no-store"
    [row] = resp.json()
    assert row["id"] == seeded["sub"] and row["name"] == "Asha Patil"
    assert row["issues"] == {"error": 0, "warning": 1, "info": 0}  # dismissed error not counted
    assert row["verdict"] == "review" and row["documents"] == 1


def test_detail_has_pages_fields_and_boxes_relative_to_the_upright_page(
    client: TestClient, seeded: dict, session: Session
) -> None:
    body = client.get(f"{API}/submissions/{seeded['sub']}").json()
    assert body["verdict"] == "review"
    assert [i["status"] for i in body["issues"]] == ["open", "dismissed"]  # open first
    assert "key" not in body["issues"][0]["details"]  # internal bookkeeping hidden
    [doc] = body["documents"]
    [page] = doc["pages"]
    assert page["type"] == "semester_marksheet" and page["ocr"]["rotation"] == 90
    [field] = page["fields"]
    assert field["box"] == {"x": 0.1, "y": 0.25, "w": 0.2, "h": 0.02}
    events = session.scalars(select(AuditEvent).where(AuditEvent.action == "submission.viewed"))
    assert len(events.all()) == 1


def test_page_image_is_upright_web_jpeg(client: TestClient, seeded: dict, session: Session) -> None:
    resp = client.get(f"{API}/pages/{seeded['page']}/image")
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/jpeg"
    img = Image.open(io.BytesIO(resp.content))
    assert img.size == (800, 1600)  # turned 90 degrees, long side capped at 1600
    assert session.scalar(select(AuditEvent).where(AuditEvent.action == "page_image.viewed"))


def test_unknown_ids_are_404(client: TestClient, database) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"{API}/submissions/{missing}").status_code == 404
    assert client.get(f"{API}/pages/{missing}/image").status_code == 404
    assert client.get(f"{API}/submissions/not-a-uuid").status_code == 422


def test_refused_in_production_until_sign_in_exists(test_settings: Settings) -> None:
    prod = test_settings.model_copy(update={"environment": "production"})
    with TestClient(create_app(prod)) as c:
        resp = c.get(f"{API}/submissions")
    assert resp.status_code == 403


def test_refused_from_other_machines(test_settings: Settings, database) -> None:
    with TestClient(create_app(test_settings), client=("192.168.1.20", 5000)) as c:
        assert c.get(f"{API}/submissions").status_code == 403
        assert c.get("/api/v1/health").status_code == 200  # other routes unaffected
