from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.session import Database
from app.main import create_app


class UnreachableDatabase(Database):
    def ping(self) -> bool:
        return False


def test_health_ok(client: TestClient) -> None:
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["environment"] == "test"


def test_health_reports_unavailable_database(test_settings: Settings) -> None:
    app = create_app(test_settings)
    app.state.db = UnreachableDatabase(test_settings.database_url)
    with TestClient(app) as c:
        resp = c.get("/api/v1/health")
    assert resp.status_code == 503
    assert resp.json()["status"] == "degraded"
    assert resp.json()["database"] == "unavailable"


def test_storage_dir_created_on_startup(client: TestClient, test_settings: Settings) -> None:
    assert test_settings.storage_dir.is_dir()


def test_openapi_lists_health_route(client: TestClient) -> None:
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    assert "/api/v1/health" in resp.json()["paths"]
