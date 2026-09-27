"""Telemetry ingestion endpoint: the producer. Validate, enqueue, respond.

The route never touches user_event or behavior_signal. It only needs *a*
queue — it does not know or care that today's is PostgreSQL.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from darwin.queue.base import MessageQueue
from darwin.telemetry.schemas import IngestionResult, TelemetryEvent
from darwin.telemetry.service import enqueue_event

router = APIRouter(prefix="/telemetry", tags=["telemetry"])


def get_queue(request: Request) -> MessageQueue:
    queue: MessageQueue = request.app.state.queue
    return queue


Queue = Annotated[MessageQueue, Depends(get_queue)]


@router.post(
    "/events",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit one behavioural event",
    description=(
        "Accepts one privacy-safe interaction event **for asynchronous processing**: "
        "on 202 the event is durably queued; a worker stores it and updates behaviour "
        "signals shortly afterwards. Idempotent on `event_id`: resubmitting an event "
        "that was already accepted returns 202 with `status: duplicate` and queues "
        "nothing new. Invalid events are rejected with 422 before anything is queued."
    ),
    response_description="The event is queued (or had already been accepted).",
)
def submit_event(event: TelemetryEvent, queue: Queue) -> IngestionResult:
    return enqueue_event(queue, event)
