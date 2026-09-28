"""Active-generation endpoint: read-only. There is NO promotion or rollback route.

GET /api/v1/generations/active?page=pricing_signup
  -> {"status": "active", generation, spec_version_id, spec_hash, spec}
  -> {"status": "none"}          (nothing stored / unavailable: render bundled Generation 0)

The stored spec is re-hashed before it is served; a mismatch answers `none`. The
response carries only what rendering and telemetry attribution need — no approvals,
reviewers, evidence or experiment data.
"""

import logging
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from darwin.api.experiments import Factory
from darwin.generations.active import active_spec
from darwin.mutations.apply import content_hash

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/generations", tags=["generations"])


class ActiveGenerationResponse(BaseModel):
    status: Literal["active", "none"]
    generation: int | None = None
    spec_version_id: uuid.UUID | None = None
    spec_hash: str | None = None
    spec: dict[str, Any] | None = None


@router.get(
    "/active",
    summary="The UI Spec generation a page currently serves",
    response_model_exclude_none=True,
)
def active_generation(
    factory: Factory,
    page: Annotated[str, Query(pattern=r"^[a-z][a-z0-9_]{0,63}$")],
) -> ActiveGenerationResponse:
    try:
        with factory() as session:
            spec = active_spec(session, page)
            if spec is None or spec.generation is None:
                return ActiveGenerationResponse(status="none")
            if content_hash(spec.spec) != spec.content_hash:
                logger.warning(
                    "active generation failed its hash check", extra={"context": {"page": page}}
                )
                return ActiveGenerationResponse(status="none")
            return ActiveGenerationResponse(
                status="active",
                generation=spec.generation,
                spec_version_id=spec.id,
                spec_hash=spec.content_hash,
                spec=dict(spec.spec),
            )
    except Exception as error:  # noqa: BLE001 — the page must keep working (bundled Generation 0)
        logger.error(
            "active generation lookup failed", extra={"context": {"error": type(error).__name__}}
        )
        return ActiveGenerationResponse(status="none")
