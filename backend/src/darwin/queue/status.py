"""Read-only queue summary for developers:  python -m darwin.queue.status  (make queue-status)

Shows counts and ages only. Never message bodies: they contain untrusted
telemetry payloads.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from darwin.config import Settings
from darwin.db.engine import create_db_engine
from darwin.db.models.queue_message import DEAD, DONE, PENDING, QueueMessage


def queue_counts(session: Session) -> dict[str, int]:
    """pending (visible now) / leased (in flight) / delayed (waiting to retry) / done / dead."""
    now = func.now()
    pending = QueueMessage.status == PENDING
    in_flight = QueueMessage.receipt_handle.is_not(None)
    row = session.execute(
        select(
            func.count().filter(pending, QueueMessage.visible_at <= now),
            func.count().filter(pending, QueueMessage.visible_at > now, in_flight),
            func.count().filter(pending, QueueMessage.visible_at > now, ~in_flight),
            func.count().filter(QueueMessage.status == DONE),
            func.count().filter(QueueMessage.status == DEAD),
        )
    ).one()
    return dict(zip(("pending", "leased", "delayed", "done", "dead"), row, strict=True))


def oldest_pending_age_seconds(session: Session) -> float | None:
    age = session.scalar(
        select(func.extract("epoch", func.now() - func.min(QueueMessage.created_at))).where(
            QueueMessage.status == PENDING
        )
    )
    return None if age is None else float(age)


def dead_letter_reasons(session: Session) -> list[tuple[str, int]]:
    """Grouped, sanitised last_error of dead messages (no bodies, no ids)."""
    rows = session.execute(
        select(func.coalesce(QueueMessage.last_error, "(none)"), func.count())
        .where(QueueMessage.status == DEAD)
        .group_by(QueueMessage.last_error)
        .order_by(func.count().desc())
        .limit(10)
    ).all()
    return [(str(reason), int(count)) for reason, count in rows]


def main() -> None:
    settings = Settings()
    engine = create_db_engine(str(settings.database_url))
    try:
        with Session(engine) as session:
            counts = queue_counts(session)
            age = oldest_pending_age_seconds(session)
            reasons = dead_letter_reasons(session)
    finally:
        engine.dispose()
    print("  ".join(f"{name}={count}" for name, count in counts.items()))
    print(f"oldest pending: {'-' if age is None else f'{age:.1f}s'}")
    for reason, count in reasons:
        print(f"dead: {count} x {reason}")


if __name__ == "__main__":
    main()
