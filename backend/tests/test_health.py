"""Health endpoints without a database (the unit-test settings point at a closed port).

The "database available -> ready" case needs PostgreSQL and lives in
tests/integration/test_readiness.py.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from darwin.api.health import LivenessResponse, ReadinessResponse


def test_liveness_reports_alive(client: TestClient) -> None:
    response = client.get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}
    LivenessResponse.model_validate(response.json())


def test_app_starts_and_stays_alive_when_database_is_unreachable(client: TestClient) -> None:
    # Startup must not connect to (or create tables in) the database.
    assert client.get("/api/v1/health/live").status_code == 200


def test_readiness_is_503_when_database_is_unreachable(client: TestClient) -> None:
    response = client.get("/api/v1/health/ready")

    assert response.status_code == 503
    body = ReadinessResponse.model_validate(response.json())
    assert body.status == "not_ready"
    checks = {check.name: check for check in body.checks}
    assert checks["startup_complete"].ok is True
    assert checks["database"].ok is False
    # No connection details (host, user) leak into the public response.
    assert checks["database"].detail == "unavailable"


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


def test_readiness_reports_not_started_after_shutdown(app: FastAPI) -> None:
    with TestClient(app):
        pass

    # The lifespan's shutdown half has run; the app must no longer claim readiness.
    body = ReadinessResponse.model_validate(TestClient(app).get("/api/v1/health/ready").json())
    assert body.status == "not_ready"
    assert {check.name: check.ok for check in body.checks}["startup_complete"] is False


def test_health_endpoints_only_accept_get(client: TestClient) -> None:
    assert client.post("/api/v1/health/live").status_code == 405
