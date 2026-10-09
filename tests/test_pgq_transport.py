"""The ``pgq`` Kombu transport against a real Postgres (.github#5).

Runs against the test database: ``celery_queue`` comes from the models
(``create_all``). Covers the queue contract the worker relies on: FIFO,
no double delivery under ``SKIP LOCKED``, ack = delete, requeue, lease
expiry, LISTEN/NOTIFY wake-up, fallback poll, idempotent sends and parked
messages.
"""
from __future__ import annotations

import threading
import time
import uuid
from queue import Empty

import psycopg
import pytest
from kombu import Connection, Consumer, Exchange, Producer, Queue
from kombu.transport import TRANSPORT_ALIASES

from app.pgq import HEADER_AFTER_TASK, libpq_url
from tests.conftest import _TEST_DB_URL

TRANSPORT_ALIASES.setdefault("pgq", "app.pgq:Transport")

BROKER_URL = "pgq+" + libpq_url(_TEST_DB_URL)
QUEUE = "pgq-test"
exchange = Exchange(QUEUE, type="direct")
queue = Queue(QUEUE, exchange, routing_key=QUEUE)

pytestmark = pytest.mark.integration


def _connection(**options) -> Connection:
    return Connection(BROKER_URL, transport_options={"polling_interval": 5.0, **options})


def _publish(conn: Connection, body: dict, *, task_id: str | None = None, headers: dict | None = None) -> None:
    hdrs = dict(headers or {})
    if task_id:
        hdrs["id"] = task_id
    Producer(conn.channel(), exchange=exchange, routing_key=QUEUE).publish(body, headers=hdrs, declare=[queue])


def _rows() -> list[tuple]:
    with psycopg.connect(libpq_url(_TEST_DB_URL)) as c:
        return c.execute(
            "SELECT id, task_id, after_task, visible_after <= now(), claimed_by, delivery_count"
            " FROM celery_queue WHERE queue = %s ORDER BY id",
            (QUEUE,),
        ).fetchall()


def test_put_get_fifo_and_ack_deletes():
    with _connection() as conn:
        for i in range(3):
            _publish(conn, {"n": i})
        channel = conn.channel()
        got = [channel.basic_get(QUEUE) for _ in range(3)]
        assert [m.payload["n"] for m in got] == [0, 1, 2]
        assert channel.basic_get(QUEUE) is None  # all claimed

        got[0].ack()
        remaining = _rows()
        assert len(remaining) == 2
        assert all(row[4] == channel.consumer_id for row in remaining)


def test_two_consumers_never_get_the_same_message():
    with _connection() as producer:
        for i in range(40):
            _publish(producer, {"n": i})

    seen: list[int] = []
    lock = threading.Lock()

    def consume():
        with _connection() as conn:
            channel = conn.channel()
            while True:
                message = channel.basic_get(QUEUE)
                if message is None:
                    return
                with lock:
                    seen.append(message.payload["n"])
                # Unacked messages go back to the queue when a channel
                # closes (as in AMQP); a consumer that is still looping
                # would then get them a second time.
                message.ack()

    threads = [threading.Thread(target=consume) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(seen) == list(range(40))  # each delivered exactly once
    assert _rows() == []


def test_reject_with_requeue_makes_the_row_visible_again():
    with _connection() as conn:
        _publish(conn, {"n": 1})
        channel = conn.channel()
        message = channel.basic_get(QUEUE)
        assert channel.basic_get(QUEUE) is None
        message.reject(requeue=True)
        again = channel.basic_get(QUEUE)
        assert again is not None and again.payload == {"n": 1}
        assert again.delivery_info.get("redelivered") is True
        assert len(_rows()) == 1  # requeued in place, not copied


def test_reject_without_requeue_deletes():
    with _connection() as conn:
        _publish(conn, {"n": 1})
        channel = conn.channel()
        channel.basic_get(QUEUE).reject(requeue=False)
        assert _rows() == []


def test_expired_lease_makes_the_message_visible_to_another_consumer():
    with _connection(visibility_timeout=1) as first, _connection(visibility_timeout=1) as second:
        _publish(first, {"n": 1})
        held = first.channel().basic_get(QUEUE)
        assert held is not None
        other = second.channel()
        assert other.basic_get(QUEUE) is None
        time.sleep(1.5)
        again = other.basic_get(QUEUE)
        assert again is not None and again.payload == {"n": 1}
        assert _rows()[0][5] == 2  # delivery_count
        # The first consumer lost the row: its ack must not delete the
        # message the second consumer now holds.
        held.ack()
        assert len(_rows()) == 1


def test_lease_renewal_keeps_a_claimed_message_hidden():
    received = []
    with (
        _connection(visibility_timeout=1.2) as conn,
        Consumer(conn, queues=[queue], callbacks=[lambda _body, msg: received.append(msg)], accept=["json"]),
    ):
        _publish(conn, {"n": 1})
        conn.drain_events(timeout=5)
        assert received
        # The renewal thread runs every visibility_timeout / 3.
        time.sleep(3)
        with _connection() as other:
            assert other.channel().basic_get(QUEUE) is None


def test_notify_wakes_a_waiting_consumer_quickly():
    received: list[float] = []
    with (
        _connection() as conn,
        Consumer(conn, queues=[queue], callbacks=[lambda _body, _msg: received.append(time.monotonic())]),
    ):
        waiter = threading.Thread(target=lambda: conn.drain_events(timeout=10), daemon=True)
        waiter.start()
        time.sleep(1.0)  # consumer is waiting, LISTEN is set up
        with _connection() as producer:
            _publish(producer, {"n": 1})  # opens the connection
            sent = time.monotonic()
            received.clear()
            waiter.join(timeout=10)
    # Measured from the end of the publish; the fallback poll is 5 s.
    assert received, "not delivered"
    latency = received[0] - sent
    print(f"wake-up latency {latency * 1000:.1f} ms")
    assert latency < 0.1


def test_fallback_poll_without_notify():
    received = []
    with (
        _connection(polling_interval=0.5) as conn,
        Consumer(conn, queues=[queue], callbacks=[lambda body, _msg: received.append(body)], accept=["json"]),
    ):
        # A row inserted behind the transport's back sends no NOTIFY.
        with _connection() as producer:
            channel = producer.channel()
            message = channel.prepare_message('{"n": 7}', content_type="application/json", content_encoding="utf-8")
            message["properties"]["delivery_info"] = {"exchange": QUEUE, "routing_key": QUEUE}
            channel._inplace_augment_message(message, QUEUE, QUEUE)
            with psycopg.connect(libpq_url(_TEST_DB_URL), autocommit=True) as c:
                c.execute(
                    "INSERT INTO celery_queue (queue, payload) VALUES (%s, %s)",
                    (QUEUE, psycopg.types.json.Jsonb(message)),
                )
        conn.drain_events(timeout=5)
    assert received == [{"n": 7}]


def test_sending_the_same_task_twice_is_a_no_op():
    task_id = str(uuid.uuid4())
    with _connection() as conn:
        _publish(conn, {"n": 1}, task_id=task_id)
        _publish(conn, {"n": 2}, task_id=task_id)
    rows = _rows()
    assert len(rows) == 1 and str(rows[0][1]) == task_id


def test_parked_message_stays_hidden_until_released():
    blocker = str(uuid.uuid4())
    with _connection() as conn:
        _publish(conn, {"n": 1}, task_id=str(uuid.uuid4()), headers={HEADER_AFTER_TASK: blocker})
        channel = conn.channel()
        assert channel.basic_get(QUEUE) is None
        (row,) = _rows()
        assert str(row[2]) == blocker and row[3] is False

        with psycopg.connect(libpq_url(_TEST_DB_URL), autocommit=True) as c:
            c.execute("UPDATE celery_queue SET visible_after = now(), after_task = NULL WHERE after_task = %s", (blocker,))
        assert channel.basic_get(QUEUE) is not None


def test_get_on_an_empty_queue_raises_empty():
    with _connection() as conn, pytest.raises(Empty):
        conn.channel()._get(QUEUE)
