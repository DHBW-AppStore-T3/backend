"""Background pass over the task queue (.github#5).

The task row is the only truth about a task now: the worker writes status,
logs and results straight into it, so there is no second state (Celery's
result backend) to reconcile against. What is left to look after:

1. **Dead workers.** A RUNNING task whose lease ran out belongs to a worker
   that stopped renewing it (crashed, killed, cut off from the database).
   The task is failed as ``worker_lost`` with a terminal event, so open
   streams end. Nothing is retried: a half-applied Terraform run needs a
   person to look at it (destroy or deploy again from the UI).
2. **Lost dispatches.** A PENDING task without a queue message, older than
   ``DISPATCH_GRACE_SECONDS``: the API process died between committing the
   row and sending it. The message arguments only ever existed in the
   message, so the task cannot be re-sent; it is failed instead.
3. **Parked messages** (destroy after cancel) whose blocking task is no
   longer held by a worker but whose ``task_released`` notification was
   missed, e.g. because the worker died.
4. **Follow-up work** of finished tasks (``task_finalizer``), and once an
   hour the retention of old task events.

Every step is safe to run in several API processes at once (``SKIP LOCKED``
claims, idempotent updates). The pass runs every ``RECONCILE_INTERVAL_SECONDS``
and right away when the event hub reports a terminal event.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Task, TaskStatus
from app.services import task_events, task_finalizer, task_service
from app.task_contract import (
    EVENT_FAILED,
    FAILURE_KIND_INFRASTRUCTURE,
    FAILURE_KIND_WORKER_LOST,
    terminal_payload,
)
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

RECONCILE_INTERVAL_SECONDS = 15
# A PENDING task gets this long to show up in the queue.
DISPATCH_GRACE_SECONDS = 60
# RUNNING tasks from before the queue migration have no lease; they are
# considered lost once they ran longer than the worker's job limit.
LEGACY_RUNNING_GRACE_SECONDS = 2 * 60 * 60
EVENT_PURGE_INTERVAL_SECONDS = 60 * 60
BATCH = 50

WORKER_LOST_MESSAGE = (
    "Der Worker, der diesen Auftrag ausgeführt hat, antwortet nicht mehr "
    "(abgestürzt oder neu gestartet). Der Auftrag wurde abgebrochen und wird "
    "nicht automatisch wiederholt. Bitte prüfen Sie den Zustand der Ressourcen "
    "und starten Sie die Aktion bei Bedarf erneut."
)
LOST_DISPATCH_MESSAGE = (
    "Der Auftrag wurde nie an die Warteschlange übergeben (der API-Prozess "
    "wurde währenddessen beendet). Bitte die Aktion erneut starten."
)


def _fail(db: Session, task: Task, message: str, failure_kind: str) -> None:
    """Fail ``task`` with ``message`` appended to its log and a terminal event; committed by the caller."""
    task.status = TaskStatus.FAILED
    task.finished_at = utcnow()
    task.lease_until = None
    task.logs = f"{task.logs}\n\n{message}" if task.logs else message
    task_events.add_event(
        db,
        task.taskId,
        EVENT_FAILED,
        terminal_payload(
            EVENT_FAILED,
            deployment_id=str(task.deploymentId),
            task_id=str(task.taskId),
            task_type=task.type.value if task.type else None,
            failure_kind=failure_kind,
        ),
    )


def reap_expired_leases(db: Session) -> int:
    """Fail RUNNING tasks whose worker stopped renewing the lease. Returns how many."""
    tasks = db.scalars(
        select(Task)
        .where(
            Task.status == TaskStatus.RUNNING,
            text(
                "(tasks.lease_until < now()"
                " OR (tasks.lease_until IS NULL AND tasks.claimed_by IS NULL"
                "     AND tasks.started_at < timezone('UTC', now()) - make_interval(secs => :legacy)))"
            ).bindparams(legacy=LEGACY_RUNNING_GRACE_SECONDS),
        )
        .with_for_update(skip_locked=True)
        .limit(BATCH)
    ).all()
    for task in tasks:
        logger.warning("Task %s: lease of worker %s expired, failing it (worker_lost)", task.taskId, task.claimed_by)
        _fail(db, task, WORKER_LOST_MESSAGE, FAILURE_KIND_WORKER_LOST)
    db.commit()
    return len(tasks)


def fail_lost_dispatches(db: Session) -> int:
    """Fail PENDING tasks that never reached the queue. Returns how many."""
    tasks = db.scalars(
        select(Task)
        .where(
            Task.status == TaskStatus.PENDING,
            text("tasks.created_at < timezone('UTC', now()) - make_interval(secs => :grace)").bindparams(
                grace=DISPATCH_GRACE_SECONDS
            ),
            text('NOT EXISTS (SELECT 1 FROM celery_queue q WHERE q.task_id = tasks."taskId")'),
        )
        .with_for_update(skip_locked=True)
        .limit(BATCH)
    ).all()
    for task in tasks:
        logger.warning("Task %s: never reached the queue, failing it", task.taskId)
        _fail(db, task, LOST_DISPATCH_MESSAGE, FAILURE_KIND_INFRASTRUCTURE)
    db.commit()
    return len(tasks)


_state: dict[str, Any] = {"last_purge": 0.0}


def reconcile_once() -> None:
    """One full pass; each step in its own session so one failure does not stop the others."""
    steps = (
        ("reap expired leases", reap_expired_leases),
        ("fail lost dispatches", fail_lost_dispatches),
        ("release parked messages", task_service.release_parked),
        ("finalize finished tasks", task_finalizer.finalize_pending),
    )
    for name, step in steps:
        try:
            with SessionLocal() as db:
                step(db)
        except Exception:
            logger.exception("Reconciler step '%s' failed; will retry next pass", name)
    if time.monotonic() - _state["last_purge"] >= EVENT_PURGE_INTERVAL_SECONDS:
        _state["last_purge"] = time.monotonic()
        try:
            with SessionLocal() as db:
                task_finalizer.purge_events(db)
        except Exception:
            logger.exception("Purging old task events failed")


async def run_reconciler(wake: asyncio.Event | None = None) -> None:
    """Top-level coroutine started from the FastAPI lifespan.

    Runs a pass every ``RECONCILE_INTERVAL_SECONDS`` and whenever ``wake``
    is set (terminal event, released task).
    """
    wake = wake or asyncio.Event()
    logger.info("Reconciler loop starting (interval=%ss)", RECONCILE_INTERVAL_SECONDS)
    try:
        while True:
            wake.clear()
            try:
                await asyncio.to_thread(reconcile_once)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Reconciler pass failed; will retry next tick")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout=RECONCILE_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        logger.info("Reconciler loop cancelled — shutting down")
        raise
