"""PostgresQueue semantics against darwin_test: enqueue, lease, ack, retry, dead, concurrency."""

import threading
import uuid
from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import Connection, Engine, delete, func, select, text
from sqlalchemy.orm import Session

from darwin.db.models import QueueMessage
from darwin.db.session import create_session_factory
from darwin.queue.base import OutgoingMessage, ReceivedMessage
from darwin.queue.postgres import PostgresQueue
from darwin.queue.status import queue_counts

pytestmark = pytest.mark.integration


def _message(body: dict[str, object] | None = None) -> OutgoingMessage:
    return OutgoingMessage(uuid.uuid4(), "test.message", body or {"n": 1})


def _row(connection: Connection, message_id: uuid.UUID) -> QueueMessage:
    with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
        row = session.scalars(
            select(QueueMessage).where(QueueMessage.message_id == message_id)
        ).one()
        session.expunge(row)
        return row


def _expire_leases(connection: Connection) -> None:
    """Simulate the passage of time: every pending message becomes visible now."""
    connection.execute(
        text(
            "UPDATE queue_message SET visible_at = now() - interval '1 second' "
            "WHERE status = 'pending'"
        )
    )


def _only_mine(received: ReceivedMessage | None, mine: OutgoingMessage) -> ReceivedMessage:
    assert received is not None and received.message_id == mine.message_id
    return received


# ---- enqueue -------------------------------------------------------------------------


def test_enqueue_is_durable_and_pending(test_queue: PostgresQueue, connection: Connection) -> None:
    message = _message({"k": "v"})

    assert test_queue.enqueue(message) is True

    row = _row(connection, message.message_id)
    assert (row.status, row.attempts, row.body) == ("pending", 0, {"k": "v"})
    assert row.receipt_handle is None and row.finished_at is None


def test_enqueue_same_message_id_twice_keeps_one_row_and_the_first_body(
    test_queue: PostgresQueue, connection: Connection
) -> None:
    first = _message({"version": "first"})
    second = OutgoingMessage(first.message_id, first.message_type, {"version": "second"})

    assert test_queue.enqueue(first) is True
    assert test_queue.enqueue(second) is False

    count = connection.scalar(
        select(func.count())
        .select_from(QueueMessage)
        .where(QueueMessage.message_id == first.message_id)
    )
    assert count == 1
    assert _row(connection, first.message_id).body == {"version": "first"}


def test_resubmitting_a_processed_message_is_still_a_duplicate(
    test_queue: PostgresQueue, connection: Connection
) -> None:
    message = _message()
    test_queue.enqueue(message)
    test_queue.ack(_only_mine(test_queue.receive(), message))

    assert test_queue.enqueue(message) is False
    assert _row(connection, message.message_id).status == "done"


def test_resubmitting_a_dead_message_requeues_it_with_a_fresh_budget(
    test_queue: PostgresQueue, connection: Connection
) -> None:
    message = _message()
    test_queue.enqueue(message)
    test_queue.dead_letter(_only_mine(test_queue.receive(), message), error="RuntimeError")

    assert test_queue.enqueue(message) is True

    row = _row(connection, message.message_id)
    assert (row.status, row.attempts, row.finished_at) == ("pending", 0, None)


# ---- lease / ack / retry / dead ----------------------------------------------------------


def test_receive_leases_the_message_and_hides_it(
    test_queue: PostgresQueue, connection: Connection
) -> None:
    message = _message()
    test_queue.enqueue(message)

    received = _only_mine(test_queue.receive(), message)

    assert received.attempt == 1
    row = _row(connection, message.message_id)
    assert row.receipt_handle == received.receipt_handle
    assert test_queue.receive() is None  # invisible while leased


def test_ack_marks_done(test_queue: PostgresQueue, connection: Connection) -> None:
    message = _message()
    test_queue.enqueue(message)

    assert test_queue.ack(_only_mine(test_queue.receive(), message)) is True

    row = _row(connection, message.message_id)
    assert row.status == "done" and row.finished_at is not None and row.receipt_handle is None
    _expire_leases(connection)
    assert test_queue.receive() is None  # never delivered again


def test_expired_lease_makes_the_message_visible_again(
    test_queue: PostgresQueue, connection: Connection
) -> None:
    message = _message()
    test_queue.enqueue(message)
    first = _only_mine(test_queue.receive(), message)

    _expire_leases(connection)  # the worker "crashed"; its lease ran out
    second = _only_mine(test_queue.receive(), message)

    assert second.attempt == 2
    assert second.receipt_handle != first.receipt_handle


def test_a_stale_delivery_cannot_settle_the_current_one(
    test_queue: PostgresQueue, connection: Connection
) -> None:
    message = _message()
    test_queue.enqueue(message)
    stale = _only_mine(test_queue.receive(), message)
    _expire_leases(connection)
    current = _only_mine(test_queue.receive(), message)

    assert test_queue.ack(stale) is False
    assert test_queue.retry(stale, delay=timedelta(0), error="x") is False
    assert test_queue.dead_letter(stale, error="x") is False
    assert test_queue.ack(current) is True


def test_retry_hides_the_message_until_its_delay_passes(
    test_queue: PostgresQueue, connection: Connection
) -> None:
    message = _message()
    test_queue.enqueue(message)
    received = _only_mine(test_queue.receive(), message)

    assert test_queue.retry(received, delay=timedelta(seconds=8), error="OperationalError") is True

    row = _row(connection, message.message_id)
    assert (row.status, row.attempts, row.last_error) == ("pending", 1, "OperationalError")
    assert row.receipt_handle is None
    assert test_queue.receive() is None  # still inside the retry delay
    _expire_leases(connection)
    assert _only_mine(test_queue.receive(), message).attempt == 2


def test_dead_letter_keeps_the_body_and_a_bounded_error(
    test_queue: PostgresQueue, connection: Connection
) -> None:
    message = _message({"kept": True})
    test_queue.enqueue(message)

    test_queue.dead_letter(_only_mine(test_queue.receive(), message), error="E" * 2000)

    row = _row(connection, message.message_id)
    assert row.status == "dead" and row.body == {"kept": True}
    assert row.last_error is not None and len(row.last_error) == 500
    _expire_leases(connection)
    assert test_queue.receive() is None


def test_queue_status_counts_without_reading_bodies(
    test_queue: PostgresQueue, connection: Connection
) -> None:
    messages = [_message() for _ in range(4)]
    for message in messages:
        test_queue.enqueue(message)
    a = test_queue.receive()
    b = test_queue.receive()
    c = test_queue.receive()
    assert a and b and c
    test_queue.ack(a)
    test_queue.dead_letter(b, error="x")
    # c stays leased; one message is still visible.

    with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
        counts = queue_counts(session)

    assert counts == {"pending": 1, "leased": 1, "delayed": 0, "done": 1, "dead": 1}


# ---- concurrency (real commits, separate connections) -------------------------------------


@pytest.fixture
def committed_queue(migrated_engine: Engine) -> PostgresQueue:
    return PostgresQueue(
        create_session_factory(migrated_engine), visibility_timeout=timedelta(seconds=30)
    )


@pytest.fixture
def cleanup(migrated_engine: Engine) -> Iterator[list[uuid.UUID]]:
    created: list[uuid.UUID] = []
    yield created
    with migrated_engine.begin() as conn:
        conn.execute(delete(QueueMessage).where(QueueMessage.message_id.in_(created)))


def _race(queue: PostgresQueue, workers: int) -> list[ReceivedMessage | None]:
    """`workers` threads call receive() at the same instant."""
    barrier = threading.Barrier(workers)
    results: list[ReceivedMessage | None] = []
    lock = threading.Lock()

    def claim() -> None:
        barrier.wait()
        received = queue.receive()
        with lock:
            results.append(received)

    threads = [threading.Thread(target=claim) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    return results


def _visible_backlog(migrated_engine: Engine) -> int:
    with migrated_engine.connect() as conn:
        return int(
            conn.scalar(
                text(
                    "SELECT count(*) FROM queue_message "
                    "WHERE status = 'pending' AND visible_at <= now()"
                )
            )
            or 0
        )


def test_one_message_is_leased_by_exactly_one_of_many_workers(
    committed_queue: PostgresQueue, migrated_engine: Engine, cleanup: list[uuid.UUID]
) -> None:
    assert _visible_backlog(migrated_engine) == 0, "darwin_test queue must start empty"
    message = _message()
    cleanup.append(message.message_id)
    committed_queue.enqueue(message)

    results = _race(committed_queue, workers=8)

    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert winners[0].message_id == message.message_id


def test_two_messages_are_leased_by_two_different_workers(
    committed_queue: PostgresQueue, migrated_engine: Engine, cleanup: list[uuid.UUID]
) -> None:
    assert _visible_backlog(migrated_engine) == 0, "darwin_test queue must start empty"
    messages = [_message(), _message()]
    for message in messages:
        cleanup.append(message.message_id)
        committed_queue.enqueue(message)

    results = _race(committed_queue, workers=2)

    received = [r for r in results if r is not None]
    assert sorted(str(r.message_id) for r in received) == sorted(
        str(m.message_id) for m in messages
    )


def test_a_row_being_claimed_is_skipped_not_waited_for(
    committed_queue: PostgresQueue, migrated_engine: Engine, cleanup: list[uuid.UUID]
) -> None:
    """SKIP LOCKED: while one transaction holds message A's row lock, a second
    receiver gets message B immediately instead of blocking on A."""
    assert _visible_backlog(migrated_engine) == 0, "darwin_test queue must start empty"
    first, second = _message(), _message()
    for message in (first, second):
        cleanup.append(message.message_id)
        committed_queue.enqueue(message)

    with migrated_engine.connect() as holder:
        holder.begin()
        locked: uuid.UUID = holder.execute(
            text(
                "SELECT message_id FROM queue_message WHERE status='pending' "
                "ORDER BY visible_at LIMIT 1 FOR UPDATE"
            )
        ).scalar_one()
        received = committed_queue.receive()  # would hang here without SKIP LOCKED
        holder.rollback()

    assert received is not None
    assert received.message_id != locked
