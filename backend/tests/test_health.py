from fastapi import FastAPI
from fastapi.testclient import TestClient

from darwin.api.health import LivenessResponse, ReadinessResponse


def test_liveness_reports_alive(client: TestClient) -> None:
    response = client.get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}
    LivenessResponse.model_validate(response.json())


def test_readiness_reports_ready_after_startup(client: TestClient) -> None:
    response = client.get("/api/v1/health/ready")

    assert response.status_code == 200
    body = ReadinessResponse.model_validate(response.json())
    assert body.status == "ready"
    assert all(check.ok for check in body.checks)
    assert "startup_complete" in {check.name for check in body.checks}


def test_readiness_is_503_before_startup_but_liveness_is_200(app: FastAPI) -> None:
    # Without the `with` block, the lifespan never runs: the process can answer
    # HTTP (alive) but has not finished starting up (not ready).
    client = TestClient(app)

    live = client.get("/api/v1/health/live")
    ready = client.get("/api/v1/health/ready")

    assert live.status_code == 200
    assert ready.status_code == 503
    body = ReadinessResponse.model_validate(ready.json())
    assert body.status == "not_ready"
    assert [check.name for check in body.checks if not check.ok] == ["startup_complete"]


def test_readiness_is_503_again_after_shutdown(app: FastAPI) -> None:
    with TestClient(app) as client:
        assert client.get("/api/v1/health/ready").status_code == 200

    # The lifespan's shutdown half has run; the app must no longer claim readiness.
    assert TestClient(app).get("/api/v1/health/ready").status_code == 503


def test_health_endpoints_only_accept_get(client: TestClient) -> None:
    assert client.post("/api/v1/health/live").status_code == 405
