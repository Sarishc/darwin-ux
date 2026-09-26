from fastapi import FastAPI
from fastapi.testclient import TestClient

from darwin import __version__
from darwin.config import Settings
from darwin.main import create_app


def test_create_app_uses_settings_for_metadata(settings: Settings) -> None:
    app = create_app(settings)

    assert isinstance(app, FastAPI)
    assert app.title == "DarwinUX Test"
    assert app.version == __version__


def test_each_call_builds_an_independent_app() -> None:
    first = create_app(Settings(app_name="first"))
    second = create_app(Settings(app_name="second"))

    assert first is not second
    assert first.title == "first"
    assert second.title == "second"


def test_health_routes_are_versioned_under_api_v1(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert "/api/v1/health/live" in paths
    assert "/api/v1/health/ready" in paths
    # The unversioned path must not exist; clients depend on the /api/v1 contract.
    assert client.get("/health/live").status_code == 404
