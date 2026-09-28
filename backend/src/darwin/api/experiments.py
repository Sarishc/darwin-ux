"""Variant assignment endpoint: read-only. It never creates, starts or changes experiments.

POST /api/v1/experiments/assignment  {session_id, page}
  -> {"status": "none"}                                   render bundled Generation 0
  -> {"status": "assigned", experiment_key, variant, spec_hash, spec}
  -> {"status": "fallback", experiment_key, reason}       render Generation 0, no exposure

POST (not GET) so the anonymous session id travels in the body, never in a URL.
Any server-side problem answers `fallback` or `none` — never a candidate that
was not proven. Creating/starting experiments has no HTTP route at all (CLI only).
"""

import logging
import uuid
from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from darwin.experiments.serving import resolve_variant
from darwin.experiments.vocabulary import FallbackReason, Variant

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/experiments", tags=["experiments"])


def get_session_factory(request: Request) -> Callable[[], Session]:
    factory: Callable[[], Session] = request.app.state.session_factory
    return factory


Factory = Annotated[Callable[[], Session], Depends(get_session_factory)]


class AssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    session_id: uuid.UUID = Field(description="The anonymous per-visit session id.")
    page: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$", examples=["pricing_signup"])


class AssignmentResponse(BaseModel):
    status: Literal["none", "assigned", "fallback"]
    experiment_key: str | None = None
    variant: Variant | None = None
    spec_hash: str | None = None
    spec: dict[str, Any] | None = None
    reason: FallbackReason | None = None


@router.post(
    "/assignment",
    summary="Which UI Spec should this anonymous session render?",
    response_model_exclude_none=True,
)
def assignment(body: AssignmentRequest, factory: Factory) -> AssignmentResponse:
    try:
        with factory() as session:
            served = resolve_variant(session, body.session_id, body.page)
    except Exception as error:  # noqa: BLE001 — serving must never break the page or fail open
        # Error type only: database errors can carry hosts and usernames.
        logger.error(
            "experiment assignment failed", extra={"context": {"error": type(error).__name__}}
        )
        return AssignmentResponse(status="none")
    return AssignmentResponse(
        status=served.status,
        experiment_key=served.experiment_key,
        variant=served.variant,
        spec_hash=served.spec_hash,
        spec=served.spec,
        reason=served.reason,
    )
