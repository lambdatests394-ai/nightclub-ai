from fastapi.testclient import TestClient

from backend.app.main import app

client = TestClient(app)


def test_live_health_check() -> None:
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_health_check() -> None:
    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"application": "ok"}}
