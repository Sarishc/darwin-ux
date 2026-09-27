import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, QueuePool, text

from darwin.api.health import ReadinessResponse
from darwin.config import Settings
from darwin.db.session import DbSession
from darwin.main import create_app

pytestmark = pytest.mark.integration


def test_ready_and_alive_when_database_is_available(
    migrated_engine: Engine, integration_settings: Settings
) -> None:
    with TestClient(create_app(integration_settings)) as client:
        live = client.get("/api/v1/health/live")
        ready = client.get("/api/v1/health/ready")

    assert live.status_code == 200
    assert ready.status_code == 200
    body = ReadinessResponse.model_validate(ready.json())
    assert body.status == "ready"
    assert {check.name: check.ok for check in body.checks} == {
        "startup_complete": True,
        "database": True,
    }


def test_request_sessions_return_their_connections_to_the_pool(
    migrated_engine: Engine, integration_settings: Settings
) -> None:
    app = create_app(integration_settings)

    @app.get("/_test/query")
    def query(session: DbSession) -> int:
        return int(session.execute(text("SELECT 1")).scalar_one())

    with TestClient(app) as client:
        for _ in range(5):
            assert client.get("/_test/query").json() == 1
        pool = app.state.engine.pool
        assert isinstance(pool, QueuePool)  # one shared pool per app, not per request
        # Every request's session was closed: no connection is still checked out.
        assert pool.checkedout() == 0
