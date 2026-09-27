import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from darwin.db.models import UserEvent

pytestmark = pytest.mark.integration


def _event(**overrides: object) -> UserEvent:
    values: dict[str, object] = {
        "event_id": uuid.uuid4(),
        "event_type": "click",
        "session_id": uuid.uuid4(),
        "occurred_at": datetime(2026, 9, 26, 12, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return UserEvent(**values)


def test_insert_and_query_round_trip(db_session: Session) -> None:
    event = _event(payload={"x": 10, "y": 20})
    db_session.add(event)
    db_session.commit()

    loaded = db_session.scalars(select(UserEvent).where(UserEvent.event_id == event.event_id)).one()

    assert loaded.id == event.id
    assert loaded.event_type == "click"
    assert loaded.payload == {"x": 10, "y": 20}


def test_database_fills_defaults(db_session: Session) -> None:
    event = _event()
    db_session.add(event)
    db_session.flush()
    db_session.refresh(event)

    assert event.payload == {}
    assert event.received_at.tzinfo is not None


def test_timestamps_round_trip_as_the_same_instant(db_session: Session) -> None:
    # 14:00 at UTC+2 is 12:00 UTC; PostgreSQL stores the instant, not the offset.
    occurred_at = datetime(2026, 9, 26, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    event = _event(occurred_at=occurred_at)
    db_session.add(event)
    db_session.flush()
    db_session.expire_all()

    loaded = db_session.get(UserEvent, event.id)

    assert loaded is not None
    assert loaded.occurred_at == datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    assert loaded.occurred_at.utcoffset() is not None


def test_received_at_is_the_server_clock(db_session: Session) -> None:
    event = _event(occurred_at=datetime(2020, 1, 1, tzinfo=UTC))  # old client timestamp
    db_session.add(event)
    db_session.flush()
    db_session.refresh(event)

    database_now = db_session.scalar(select(func.now()))
    assert database_now is not None
    assert event.received_at == database_now  # same transaction => same now()


def test_duplicate_event_id_is_rejected(db_session: Session) -> None:
    event_id = uuid.uuid4()
    db_session.add(_event(event_id=event_id))
    db_session.flush()

    db_session.add(_event(event_id=event_id))
    with pytest.raises(IntegrityError, match="uq_user_event_event_id"):
        db_session.flush()


def test_redelivered_event_is_an_idempotent_no_op(db_session: Session) -> None:
    # The pattern the telemetry worker will use: INSERT ... ON CONFLICT DO NOTHING.
    row = {
        "id": uuid.uuid4(),
        "event_id": uuid.uuid4(),
        "event_type": "click",
        "session_id": uuid.uuid4(),
        "occurred_at": datetime(2026, 9, 26, 12, 0, tzinfo=UTC),
    }
    statement = (
        insert(UserEvent)
        .on_conflict_do_nothing(index_elements=["event_id"])
        .returning(UserEvent.id)
    )

    first = db_session.execute(statement, row).all()
    second = db_session.execute(statement, {**row, "id": uuid.uuid4()}).all()

    assert len(first) == 1  # inserted
    assert second == []  # conflict on event_id: nothing inserted, no error
    count = db_session.scalar(
        select(func.count()).select_from(UserEvent).where(UserEvent.event_id == row["event_id"])
    )
    assert count == 1


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"event_type": ""}, "ck_user_event_event_type_not_empty"),
        ({"payload": ["not", "an", "object"]}, "ck_user_event_payload_is_object"),
    ],
)
def test_check_constraints_protect_the_data(
    db_session: Session, overrides: dict[str, object], constraint: str
) -> None:
    db_session.add(_event(**overrides))

    with pytest.raises(IntegrityError, match=constraint):
        db_session.flush()
