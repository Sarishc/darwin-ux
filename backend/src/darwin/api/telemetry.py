"""Telemetry ingestion endpoint. HTTP concerns only: parse, delegate, respond."""

from fastapi import APIRouter, status

from darwin.db.session import DbSession
from darwin.telemetry.schemas import IngestionResult, TelemetryEvent
from darwin.telemetry.service import ingest_event

router = APIRouter(prefix="/telemetry", tags=["telemetry"])


@router.post(
    "/events",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit one behavioural event",
    description=(
        "Accepts one privacy-safe interaction event. Idempotent on `event_id`: "
        "resending the same event returns 202 with `status: duplicate` and stores "
        "nothing new. Invalid events are rejected with 422 before anything is stored."
    ),
    response_description="The event was accepted (or had already been accepted).",
)
def submit_event(event: TelemetryEvent, session: DbSession) -> IngestionResult:
    return ingest_event(session, event)
