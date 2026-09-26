"""Health endpoints: liveness and readiness.

Liveness  — "Is the process alive and able to answer HTTP?"
            Checks nothing external. If this fails, restart the process.

Readiness — "Can this instance do its real work right now?"
            Checks application state and (later) dependencies such as the
            database. If this fails, stop sending it traffic — but do not
            restart it; it may just be starting up or waiting on a dependency.
"""

from typing import Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel

router = APIRouter(prefix="/health", tags=["health"])


class LivenessResponse(BaseModel):
    status: Literal["alive"]


class CheckResult(BaseModel):
    """Outcome of one readiness check (e.g. "database" in a later step)."""

    name: str
    ok: bool
    detail: str | None = None


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: list[CheckResult]


@router.get("/live")
def live() -> LivenessResponse:
    return LivenessResponse(status="alive")


@router.get(
    "/ready",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
def ready(request: Request, response: Response) -> ReadinessResponse:
    checks = [
        CheckResult(name="startup_complete", ok=request.app.state.started is True),
        # Dependency checks (database, queue, …) are appended here when those
        # dependencies exist. The response contract does not change.
    ]
    is_ready = all(check.ok for check in checks)
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(status="ready" if is_ready else "not_ready", checks=checks)
