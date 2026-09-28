"""ASGI middleware: one SERVER span + two metrics per HTTP request, safely.

Chosen over opentelemetry-instrumentation-fastapi because we control exactly what
is captured: the method, the ROUTE TEMPLATE (never the raw URL — no query string,
no path values) and the status code. No headers, no bodies, no client address.
Health endpoints are skipped: probes would drown real traffic.

Incoming `traceparent` headers are ignored: the API is the entry point, and an
untrusted client must not be able to choose (or join) DarwinUX's traces.
"""

import time
from collections.abc import Callable, Sequence
from typing import Any

from opentelemetry.trace import SpanKind
from starlette.routing import compile_path

from .tracing import record, span

SKIPPED_PREFIXES = ("/api/v1/health",)
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"})


class TracingMiddleware:
    """`templates` returns the app's full route templates (e.g. from its OpenAPI paths):
    FastAPI nests included routers, so a matched route only knows its relative path."""

    def __init__(self, app: Any, templates: Callable[[], Sequence[str]] | None = None) -> None:
        self.app = app
        self._templates = templates
        self._compiled: list[tuple[Any, str]] | None = None

    def _route(self, path: str) -> str:
        if self._compiled is None:
            try:
                names = list(self._templates()) if self._templates else []
                self._compiled = [(compile_path(t)[0], t) for t in names]
            except Exception:  # noqa: BLE001
                self._compiled = []
        for regex, template in self._compiled:
            if regex.match(path):
                return template
        return "unmatched"  # never the raw path: it could carry ids or user input

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or str(scope.get("path", "")).startswith(SKIPPED_PREFIXES):
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method", "GET")).upper()
        method = method if method in _METHODS else "OTHER"
        status = {"code": 500}

        async def capture_status(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                status["code"] = int(message.get("status", 500))
            await send(message)

        started = time.perf_counter()
        with span("http.server", {"http.request.method": method}, kind=SpanKind.SERVER) as s:
            try:
                await self.app(scope, receive, capture_status)
            finally:
                route = self._route(str(scope.get("path", "")))
                code = status["code"]
                s.rename(f"{method} {route}")
                s.set(**{"http.route": route, "http.response.status_code": code})
                if code >= 500:
                    s.error(f"http_{code}")
                labels = {
                    "http.request.method": method,
                    "http.route": route,
                    "status_class": f"{code // 100}xx",
                }
                record("darwin.http.server.requests", 1, **labels)
                elapsed = (time.perf_counter() - started) * 1000
                record("darwin.http.server.duration", elapsed, **labels)
