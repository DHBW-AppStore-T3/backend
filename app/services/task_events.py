"""Task events for the live stream, and the API's LISTEN connection.

The worker appends a row to ``task_events`` for every progress and log
event of a running task; a trigger sends ``NOTIFY task_events`` with
``<task id>:<event id>:<type>``. Each API process holds **one** LISTEN
connection (:class:`EventHub`) and wakes the SSE streams of that task, so
any API replica can serve any stream and a reconnecting client resumes with
``Last-Event-ID``. This replaces the per-process Celery event listener and
the in-process pub/sub, which handled every event once per uvicorn worker
and could only serve subscribers of its own process.

The hub also reacts to ``task_released`` (a worker let go of a task, so a
destroy parked behind it may run) and wakes the follow-up loop when a
terminal event arrives.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import TaskEvent
from app.pgq import libpq_url
from app.task_contract import (
    EVENT_PROGRESS,
    NOTIFY_TASK_EVENTS,
    NOTIFY_TASK_RELEASED,
    TERMINAL_EVENTS,
)

logger = logging.getLogger(__name__)

# Events sent to a client that connects without Last-Event-ID: the tail of
# the transcript (the old in-process ring buffer kept 500 as well), plus the
# newest progress event so the stepper is right from the first frame.
BACKFILL_EVENTS = 500
# Upper bound per query, so a long backlog is sent in chunks.
BATCH = 500


def add_event(db: Session, task_id: uuid.UUID, event_type: str, payload: dict[str, Any]) -> None:
    """Append an event written by the API (cancel, worker lost); committed by the caller."""
    db.add(TaskEvent(task_id=task_id, type=event_type, payload=payload))


def events_after(db: Session, task_id: uuid.UUID, after_id: int) -> list[TaskEvent]:
    """The task's events with an id above ``after_id``, oldest first, at most ``BATCH``."""
    return list(
        db.scalars(
            select(TaskEvent)
            .where(TaskEvent.task_id == task_id, TaskEvent.id > after_id)
            .order_by(TaskEvent.id)
            .limit(BATCH)
        )
    )


def backfill(db: Session, task_id: uuid.UUID) -> list[TaskEvent]:
    """The last ``BACKFILL_EVENTS`` events plus the newest progress event, oldest first."""
    tail = list(
        db.scalars(
            select(TaskEvent)
            .where(TaskEvent.task_id == task_id)
            .order_by(desc(TaskEvent.id))
            .limit(BACKFILL_EVENTS)
        )
    )
    if tail and not any(event.type == EVENT_PROGRESS for event in tail):
        progress = db.scalars(
            select(TaskEvent)
            .where(TaskEvent.task_id == task_id, TaskEvent.type == EVENT_PROGRESS)
            .order_by(desc(TaskEvent.id))
            .limit(1)
        ).first()
        if progress is not None:
            tail.append(progress)
    return sorted(tail, key=lambda event: event.id)


class EventHub:
    """One LISTEN connection per API process, fanned out to the streams of that process."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Event]] = defaultdict(set)
        self._task: asyncio.Task | None = None
        self.listening = False
        #: Called (in the event loop) with the task id of each terminal event.
        self.on_terminal: Callable[[str], None] | None = None
        #: Called (in the event loop) with the task id of each released task.
        self.on_released: Callable[[str], None] | None = None

    # ------------------------------------------------------------------
    # Subscriptions (SSE endpoint)
    # ------------------------------------------------------------------
    @contextlib.contextmanager
    def subscribe(self, task_id: uuid.UUID | str) -> Iterator[asyncio.Event]:
        """An event that is set whenever the task gets a new event row."""
        key = str(task_id)
        wake = asyncio.Event()
        self._subscribers[key].add(wake)
        try:
            yield wake
        finally:
            subscribers = self._subscribers.get(key)
            if subscribers is not None:
                subscribers.discard(wake)
                if not subscribers:
                    self._subscribers.pop(key, None)

    def _wake(self, task_id: str) -> None:
        for wake in self._subscribers.get(task_id, ()):
            wake.set()

    def _wake_all(self) -> None:
        for subscribers in self._subscribers.values():
            for wake in subscribers:
                wake.set()

    # ------------------------------------------------------------------
    # LISTEN loop
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="task-event-hub")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.listening = False

    def _dispatch(self, channel: str, payload: str) -> None:
        if channel == NOTIFY_TASK_EVENTS:
            task_id, _, rest = payload.partition(":")
            _event_id, _, event_type = rest.partition(":")
            self._wake(task_id)
            if event_type in TERMINAL_EVENTS and self.on_terminal is not None:
                self.on_terminal(task_id)
        elif channel == NOTIFY_TASK_RELEASED and self.on_released is not None:
            self.on_released(payload)

    async def _run(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(
                    libpq_url(settings.DATABASE_URL), autocommit=True, application_name="api-listen"
                ) as conn:
                    await conn.execute(f"LISTEN {NOTIFY_TASK_EVENTS}")
                    await conn.execute(f"LISTEN {NOTIFY_TASK_RELEASED}")
                    self.listening = True
                    backoff = 1.0
                    # Streams may have missed events while we were not
                    # listening; let them query once.
                    self._wake_all()
                    logger.info("Task event hub listening")
                    async for notify in conn.notifies():
                        self._dispatch(notify.channel, notify.payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the hub alive
                logger.warning("Task event hub: LISTEN connection lost (%s); retrying in %.0fs", exc, backoff)
            self.listening = False
            self._wake_all()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


# One per API process; started from the FastAPI lifespan.
hub = EventHub()
