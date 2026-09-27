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

from darwin.db.engine import database_is_available

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
    started = request.app.state.started is True
    checks = [CheckResult(name="startup_complete", ok=started)]
    # Dependencies are only probed once startup has created them.
    if started:
        database_ok = database_is_available(request.app.state.engine)
        checks.append(
            CheckResult(
                name="database",
                ok=database_ok,
                # Deliberately vague: connection errors can contain hosts and usernames.
                detail=None if database_ok else "unavailable",
            )
        )
    is_ready = all(check.ok for check in checks)
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(status="ready" if is_ready else "not_ready", checks=checks)
