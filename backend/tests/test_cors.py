"""CORS: the browser demo's origin may call the API; nothing else may."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from darwin.config import DEFAULT_CORS_ORIGINS, Settings
from darwin.main import create_app

from .conftest import UNREACHABLE_DATABASE_URL

URL = "/api/v1/telemetry/events"
ALLOWED = "http://localhost:3000"


def _preflight(client: TestClient, origin: str) -> dict[str, str]:
    response = client.options(
        URL,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    return {k.lower(): v for k, v in response.headers.items()}


def test_allowed_origin_passes_preflight(client: TestClient) -> None:
    headers = _preflight(client, ALLOWED)

    assert headers["access-control-allow-origin"] == ALLOWED
    assert "POST" in headers["access-control-allow-methods"]
    assert "content-type" in headers["access-control-allow-headers"].lower()
    assert "access-control-allow-credentials" not in headers  # no cookies, ever


@pytest.mark.parametrize(
    "origin",
    ["https://evil.example", "http://localhost:3001", "http://localhost:3000.evil.example", "null"],
)
def test_other_origins_are_not_allowed(client: TestClient, origin: str) -> None:
    headers = _preflight(client, origin)

    assert "access-control-allow-origin" not in headers


def test_simple_request_from_allowed_origin_gets_the_header(client: TestClient) -> None:
    response = client.get("/api/v1/health/live", headers={"Origin": ALLOWED})

    assert response.headers["access-control-allow-origin"] == ALLOWED
    assert response.headers.get("access-control-allow-origin") != "*"


def test_defaults_are_the_local_frontend_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DARWIN_CORS_ORIGINS", raising=False)

    assert Settings().cors_origins == DEFAULT_CORS_ORIGINS


def test_origins_are_read_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARWIN_CORS_ORIGINS", "https://demo.darwinux.dev, http://localhost:4000")

    assert Settings().cors_origins == ["https://demo.darwinux.dev", "http://localhost:4000"]


@pytest.mark.parametrize(
    "value",
    ["*", "https://*.example.com", "localhost:3000", "http://localhost:3000/", "ftp://x.example"],
)
def test_wildcards_and_malformed_origins_fail_fast(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("DARWIN_CORS_ORIGINS", value)

    with pytest.raises(ValidationError):
        Settings()


def test_configured_origin_replaces_the_defaults() -> None:
    app = create_app(
        Settings(database_url=UNREACHABLE_DATABASE_URL, cors_origins=["https://demo.example"])
    )
    with TestClient(app) as client:
        assert "access-control-allow-origin" not in _preflight(client, ALLOWED)
        assert _preflight(client, "https://demo.example")["access-control-allow-origin"] == (
            "https://demo.example"
        )
