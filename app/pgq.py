"""Kombu transport ``pgq``: Celery's broker is a table in our own Postgres.

Replaces RabbitMQ (.github#5). Celery stays; only the transport under it
changes. Kombu's built-in SQL transport was ruled out: it polls, takes rows
``FOR UPDATE`` without ``SKIP LOCKED``, never deletes acknowledged rows and
loses a message whose consumer died hard. This one:

- **stores** each message as one row of ``celery_queue``. ``_put`` inserts it
  and sends ``NOTIFY pgq_<queue>`` in the same statement, so the wake-up is
  delivered only once the row is committed. A message carries its Celery task
  id, which the API sets to ``tasks.taskId``; a second send of the same task
  is a no-op (unique index), so the reconciler may re-send safely.
- **claims** the oldest visible row with ``FOR UPDATE SKIP LOCKED``: two
  consumers never get the same message and never wait for each other. The
  claim hides the row until ``visible_after``, a lease that a thread of the
  consuming process keeps renewing while the message is unacknowledged.
- **acknowledges** by deleting the row. A requeue or a restore at shutdown
  makes the row visible again instead of inserting a copy.
- **wakes** a waiting consumer through ``LISTEN`` on a connection of its own.
  NOTIFY is not replayed after a dropped connection, so ``drain_events`` also
  polls every ``polling_interval`` seconds (default 5).
- **parks** a message sent with the header ``pgq_after: <task id>``: it is
  stored with ``after_task`` set and stays invisible until someone releases
  it (``visible_after = now()``). The API uses this for the destroy that
  follows a cancel, which must not start while the cancelled job still runs.

After a hard crash (SIGKILL, OOM, node gone) the lease runs out and the
message becomes visible again. Whether the job may run again is not decided
here: the worker's task prologue sees the task row already ``RUNNING`` and
fails it as ``worker_lost`` instead of repeating an infrastructure job.

Connection URL: ``pgq+postgresql://user:password@host:5432/dbname``.
Transport options: ``visibility_timeout`` (seconds, default 300),
``polling_interval`` (fallback poll, default 5).

There is no fanout, so Celery events, remote control (``inspect``,
``revoke``), mingle and gossip stay off. Events and cancellation go through
the database instead (``task_events``, ``tasks.cancel_requested_at``).

This file is identical in ``backend/app/pgq.py`` and ``worker/app/pgq.py``;
``tests/unit/test_shared_files.py`` checks its hash in both repositories.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import threading
import uuid
from queue import Empty
from time import monotonic

import psycopg
from kombu.transport import virtual
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

logger = logging.getLogger(__name__)

TABLE = "celery_queue"
NOTIFY_PREFIX = "pgq_"
# Message header: park the message until the task with this id is released.
HEADER_AFTER_TASK = "pgq_after"

DEFAULT_VISIBILITY_TIMEOUT = 300.0
DEFAULT_POLLING_INTERVAL = 5.0


def notify_channel(queue: str) -> str:
    """The NOTIFY channel that wakes consumers of ``queue``."""
    return NOTIFY_PREFIX + queue


def libpq_url(url: str) -> str:
    """``url`` without a SQLAlchemy driver suffix, e.g. ``postgresql+psycopg2://`` -> ``postgresql://``."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    return f"{scheme.split('+', 1)[0]}://{rest}"


def _as_uuid(raw) -> str | None:
    try:
        return str(uuid.UUID(str(raw))) if raw else None
    except ValueError:
        return None


def _task_id(message: dict) -> str | None:
    """The Celery task id of a message (protocol 2 header), if it is a UUID."""
    return _as_uuid((message.get("headers") or {}).get("id") or (message.get("properties") or {}).get("correlation_id"))


def _after_task(message: dict) -> str | None:
    """The task a parked message waits for (header ``pgq_after``), if any."""
    return _as_uuid((message.get("headers") or {}).get(HEADER_AFTER_TASK))


class Channel(virtual.Channel):
    """One consumer or producer channel; owns one autocommit connection."""

    supports_fanout = False

    def __init__(self, connection, **kwargs):
        super().__init__(connection, **kwargs)
        self._lock = threading.RLock()
        self._conn: psycopg.Connection | None = None
        # Who holds a claimed row. Acks, requeues and lease renewals only
        # touch rows this channel claimed, never a row another consumer took
        # over after this channel's lease ran out.
        self.consumer_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

    # ------------------------------------------------------------------
    # Database access
    # ------------------------------------------------------------------
    def _execute(self, query, params=(), *, fetch: str | None = None):
        """Run one statement in autocommit mode; reconnects on the next call after a broken connection."""
        with self._lock:
            if self._conn is None or self._conn.closed:
                self._conn = psycopg.connect(
                    self.connection.dsn, autocommit=True, application_name=f"pgq:{os.getpid()}"
                )
            try:
                cur = self._conn.execute(query, params)
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "all":
                    return cur.fetchall()
                return cur.rowcount
            except (psycopg.OperationalError, psycopg.InterfaceError):
                self._close_db()
                raise

    def _close_db(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            # Closing a broken connection may fail too.
            with contextlib.suppress(Exception):
                conn.close()

    # ------------------------------------------------------------------
    # Virtual channel interface
    # ------------------------------------------------------------------
    def _put(self, queue, message, **kwargs):
        """Insert ``message`` into ``queue`` and wake its consumers (one statement, one transaction).

        A parked message (header ``pgq_after``) is stored invisible and wakes nobody.
        """
        after = _after_task(message)
        self._execute(
            sql.SQL(
                "WITH ins AS ("
                " INSERT INTO {table} (queue, payload, task_id, after_task, visible_after)"
                " VALUES (%s, %s, %s::uuid, %s::uuid, CASE WHEN %s::uuid IS NULL THEN now() ELSE 'infinity' END)"
                " ON CONFLICT (task_id) WHERE task_id IS NOT NULL DO NOTHING RETURNING after_task)"
                " SELECT pg_notify(%s, '') FROM ins WHERE ins.after_task IS NULL"
            ).format(table=sql.Identifier(TABLE)),
            (queue, Jsonb(message), _task_id(message), after, after, notify_channel(queue)),
        )

    def _get(self, queue, timeout=None):
        """Claim the oldest visible message of ``queue``; raise ``Empty`` when there is none."""
        row = self._execute(
            sql.SQL(
                "UPDATE {table} SET visible_after = now() + make_interval(secs => %s),"
                " claimed_by = %s, delivery_count = delivery_count + 1"
                " WHERE id = (SELECT id FROM {table} WHERE queue = %s AND visible_after <= now()"
                " ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)"
                " RETURNING id, payload, delivery_count"
            ).format(table=sql.Identifier(TABLE)),
            (self.connection.visibility_timeout, self.consumer_id, queue),
            fetch="one",
        )
        if row is None:
            raise Empty()
        row_id, payload, delivery_count = row
        properties = payload.setdefault("properties", {})
        # The row id travels with the message so ack and restore find it.
        properties["pgq_id"] = row_id
        properties["pgq_delivery_count"] = delivery_count
        if delivery_count > 1:
            properties.setdefault("delivery_info", {})["redelivered"] = True
        return payload

    def _size(self, queue):
        row = self._execute(
            sql.SQL("SELECT count(*) FROM {table} WHERE queue = %s AND visible_after <= now()").format(
                table=sql.Identifier(TABLE)
            ),
            (queue,),
            fetch="one",
        )
        return row[0] if row else 0

    def _purge(self, queue):
        """Delete the waiting (unclaimed) messages of ``queue``; returns how many."""
        return self._execute(
            sql.SQL("DELETE FROM {table} WHERE queue = %s AND claimed_by IS NULL").format(table=sql.Identifier(TABLE)),
            (queue,),
        )

    def _delete(self, queue, *args, **kwargs):
        self._purge(queue)

    def _lookup(self, exchange, routing_key, default=None):
        # Bindings live in each process's memory (virtual BrokerState). A
        # producer that never declared the consumer's binding still has to
        # reach the queue: with direct routing the queue is the routing key.
        return super()._lookup(exchange, routing_key, default) or [routing_key]

    def _row_id(self, delivery_tag):
        message = self.qos._delivered.get(delivery_tag)
        return message.properties.get("pgq_id") if message is not None else None

    def basic_ack(self, delivery_tag, multiple=False):
        """Acknowledge = delete the row this channel claimed."""
        row_id = self._row_id(delivery_tag)
        if row_id is not None:
            self._execute(
                sql.SQL("DELETE FROM {table} WHERE id = %s AND claimed_by = %s").format(table=sql.Identifier(TABLE)),
                (row_id, self.consumer_id),
            )
        super().basic_ack(delivery_tag, multiple)

    def basic_reject(self, delivery_tag, requeue=False):
        """Requeue = make the row visible again (via ``_restore``); otherwise delete it."""
        if not requeue:
            row_id = self._row_id(delivery_tag)
            if row_id is not None:
                self._execute(
                    sql.SQL("DELETE FROM {table} WHERE id = %s AND claimed_by = %s").format(
                        table=sql.Identifier(TABLE)
                    ),
                    (row_id, self.consumer_id),
                )
        super().basic_reject(delivery_tag, requeue=requeue)

    def _restore(self, message):
        """Make an unacknowledged message visible again (requeue, restore at shutdown)."""
        row_id = message.properties.get("pgq_id")
        if row_id is None:
            # Not one of our rows (cannot happen with this transport); fall
            # back to re-publishing it like the base class does.
            super()._restore(message)
            return
        self._execute(
            sql.SQL(
                "WITH upd AS (UPDATE {table} SET visible_after = now(), claimed_by = NULL"
                " WHERE id = %s AND claimed_by = %s RETURNING queue)"
                " SELECT pg_notify(%s::text || upd.queue, '') FROM upd"
            ).format(table=sql.Identifier(TABLE)),
            (row_id, self.consumer_id, NOTIFY_PREFIX),
        )

    def close(self):
        super().close()
        self._close_db()


class Transport(virtual.Transport):
    """Kombu transport over ``celery_queue`` with LISTEN/NOTIFY wake-ups."""

    Channel = Channel

    #: Fallback poll in seconds when no NOTIFY arrives.
    polling_interval = DEFAULT_POLLING_INTERVAL
    default_port = 5432
    driver_type = "pgq"
    driver_name = "psycopg"

    connection_errors = virtual.Transport.connection_errors + (psycopg.OperationalError, psycopg.InterfaceError)
    channel_errors = virtual.Transport.channel_errors + (psycopg.DatabaseError,)

    implements = virtual.Transport.implements.extend(
        asynchronous=False,
        exchange_type=frozenset(["direct"]),
        heartbeats=False,
    )

    def __init__(self, client, **kwargs):
        super().__init__(client, **kwargs)
        options = client.transport_options
        self.visibility_timeout = float(options.get("visibility_timeout", DEFAULT_VISIBILITY_TIMEOUT))
        self._wakeup = threading.Event()
        self._stop = threading.Event()
        self._threads_lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    @property
    def dsn(self) -> str:
        """libpq URL of the database. ``pgq+postgresql://…`` reaches Kombu as hostname ``postgresql://…``."""
        hostname = self.client.hostname or ""
        if "://" in hostname:
            return libpq_url(hostname)
        client = self.client
        params = {
            "host": client.hostname or "localhost",
            "port": client.port or self.default_port,
            "user": client.userid,
            "password": client.password,
            "dbname": client.virtual_host if client.virtual_host not in (None, "", "/") else None,
        }
        return make_conninfo(**{key: value for key, value in params.items() if value is not None})

    # ------------------------------------------------------------------
    # Consuming
    # ------------------------------------------------------------------
    def drain_events(self, connection, timeout=None):
        """Deliver one message; between empty polls wait for a NOTIFY or the fallback interval."""
        self._start_background_threads()
        time_start = monotonic()
        get = self.cycle.get
        while True:
            # Cleared before polling: a NOTIFY that arrives during the poll
            # must still cut the following wait short.
            self._wakeup.clear()
            try:
                get(self._deliver, timeout=timeout)
            except Empty:
                wait = self.polling_interval
                if timeout is not None:
                    left = timeout - (monotonic() - time_start)
                    if left <= 0:
                        raise TimeoutError()
                    wait = min(wait, left)
                self._wakeup.wait(wait)
            else:
                return

    def _active_queues(self) -> set[str]:
        return {queue for channel in list(self.channels) for queue in channel._active_queues}

    def _consumer_ids(self) -> list[str]:
        return [channel.consumer_id for channel in list(self.channels)]

    def _start_background_threads(self) -> None:
        with self._threads_lock:
            if self._threads:
                return
            self._stop.clear()
            for target, name in ((self._listen_loop, "pgq-listen"), (self._renew_loop, "pgq-lease")):
                thread = threading.Thread(target=target, name=name, daemon=True)
                thread.start()
                self._threads.append(thread)

    def _listen_loop(self) -> None:
        """Keep a LISTEN connection for the consumed queues; set the wake-up event on every NOTIFY."""
        backoff = 1.0
        while not self._stop.is_set():
            try:
                with psycopg.connect(self.dsn, autocommit=True, application_name=f"pgq-listen:{os.getpid()}") as conn:
                    listening: set[str] = set()
                    backoff = 1.0
                    # Anything published while we were not listening is
                    # found by the poll that this wake-up triggers.
                    self._wakeup.set()
                    while not self._stop.is_set():
                        for queue in self._active_queues() - listening:
                            conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(notify_channel(queue))))
                            listening.add(queue)
                        for _notify in conn.notifies(timeout=1.0):
                            self._wakeup.set()
            except Exception as exc:  # noqa: BLE001 - keep listening whatever happens
                if self._stop.is_set():
                    return
                logger.warning("pgq: LISTEN connection lost (%s); retrying in %.0fs", exc, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)

    def _renew_loop(self) -> None:
        """Extend the lease of every row this process holds, so a long job keeps its message hidden."""
        interval = max(self.visibility_timeout / 3, 1.0)
        conn: psycopg.Connection | None = None
        while not self._stop.wait(interval):
            try:
                if conn is None or conn.closed:
                    conn = psycopg.connect(self.dsn, autocommit=True, application_name=f"pgq-lease:{os.getpid()}")
                conn.execute(
                    sql.SQL(
                        "UPDATE {table} SET visible_after = now() + make_interval(secs => %s)"
                        " WHERE claimed_by = ANY(%s)"
                    ).format(table=sql.Identifier(TABLE)),
                    (self.visibility_timeout, self._consumer_ids()),
                )
            except Exception as exc:  # noqa: BLE001 - try again next round
                logger.warning("pgq: lease renewal failed: %s", exc)
                if conn is not None:
                    with contextlib.suppress(Exception):
                        conn.close()
                conn = None
        if conn is not None:
            conn.close()

    def close_connection(self, connection):
        self._stop.set()
        self._wakeup.set()
        super().close_connection(connection)
