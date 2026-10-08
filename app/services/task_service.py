"""Task lifecycle helpers.

Two-phase dispatch:

1. `prepare_task_in_tx` — INSERT a PENDING task row in the caller's
   transaction (no commit, no queue I/O). The caller commits the
   surrounding business state and the new task row atomically.

2. `dispatch_to_celery` — outside the original TX, send the task through
   Celery, i.e. insert it into ``celery_queue`` (the ``pgq`` transport,
   .github#5). The Celery task id is the task row's ``taskId``, so a second
   send of the same task is a no-op. On a send failure the row is marked
   FAILED. Either way the user sees a row reflecting reality — no
   splitbrain.

A send can also be *parked* behind another task (`dispatch_after`): the
message waits invisibly in the queue until that task's worker has let go of
it. Cancel uses this so the cleanup destroy never runs next to the job it
cancels (security review A-5, race 2).
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.crud import tasks as crud_tasks
from app.models import Task, TaskStatus, TaskType
from app.pgq import HEADER_AFTER_TASK, NOTIFY_PREFIX

logger = logging.getLogger(__name__)


# Name of the Postgres partial unique index backing the "one active
# task per deployment" rule. String-matched when translating
# IntegrityError → ActiveTaskExistsError so other unique-constraint
# violations aren't swallowed.
_ACTIVE_TASK_UNIQUE_INDEX = "uq_tasks_active_per_deployment"


class ActiveTaskExistsError(Exception):
    """A PENDING/RUNNING task already exists for this deployment."""


def prepare_task_in_tx(
    db: Session,
    deployment_id: uuid.UUID,
    task_type: TaskType,
) -> Task:
    """Insert a PENDING task row in the caller's transaction.

    Does NOT call `db.commit()` — the caller is responsible for
    committing the surrounding state alongside this row, so that
    deployment + teams + task are all visible (or all rolled back)
    atomically.

    The Celery task id is fixed up front: it is the row's ``taskId``.

    Raises `ActiveTaskExistsError` if the deployment already has a
    PENDING/RUNNING task. A Postgres partial unique index enforces this
    at the DB level too: the pre-check catches the common case, and the
    ``except IntegrityError`` around ``db.flush()`` catches the racy one
    (two concurrent requests both pass the pre-check).
    """
    existing = crud_tasks.get_tasks(db, deployment_id=deployment_id)
    for task in existing:
        if task.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
            raise ActiveTaskExistsError(
                f"Deployment {deployment_id} already has active task {task.taskId}"
            )

    task_id = uuid.uuid4()
    db_task = Task(
        taskId=task_id,
        deploymentId=deployment_id,
        type=task_type,
        status=TaskStatus.PENDING,
        celeryTaskId=str(task_id),
    )
    db.add(db_task)
    try:
        db.flush()
    except IntegrityError as e:
        # Race: another transaction inserted a PENDING/RUNNING task for
        # the same deployment between our pre-check and flush. Translate
        # the constraint violation into the domain exception so the
        # caller's 409 branch fires.
        if _ACTIVE_TASK_UNIQUE_INDEX in str(e.orig):
            raise ActiveTaskExistsError(
                f"Deployment {deployment_id} already has an active task "
                "(detected via DB unique constraint)"
            ) from e
        raise
    db.refresh(db_task)
    return db_task


def dispatch_to_celery(
    db: Session,
    task: Task,
    celery_task_name: str,
    celery_args: list,
    *,
    after_task_id: uuid.UUID | None = None,
) -> tuple[Task, str]:
    """Send a prepared task to the queue.

    MUST be called after the surrounding TX committed. Runs in fresh
    transactions so the task row is updated independently of any later
    request-handling commit.

    ``after_task_id`` parks the message until that task is let go by its
    worker (see `release_parked`).

    On a send failure the task is marked FAILED and the original
    exception is re-raised — the caller turns that into a 503.
    """
    headers = {HEADER_AFTER_TASK: str(after_task_id)} if after_task_id else None
    try:
        result = celery_app.send_task(
            celery_task_name,
            args=celery_args,
            task_id=str(task.taskId),
            headers=headers,
        )
    except Exception:
        logger.exception(
            "Queue dispatch failed for task %s (deployment %s)",
            task.taskId,
            task.deploymentId,
        )
        task.status = TaskStatus.FAILED
        task.logs = "Failed to dispatch to the task queue"
        db.commit()
        db.refresh(task)
        raise

    task.celeryTaskId = result.id
    db.commit()
    db.refresh(task)
    if after_task_id is not None:
        # The blocking task may have been let go before the message was
        # parked; its worker then found nothing to release.
        release_parked(db, after_task_id)
        logger.info("Task %s queued behind task %s", task.taskId, after_task_id)
    else:
        logger.info("Task %s queued", task.taskId)
    return task, result.id


def release_parked(db: Session, after_task_id: uuid.UUID | None = None) -> int:
    """Make parked messages visible whose blocking task no worker holds any more.

    With ``after_task_id`` only the messages waiting for that task; without,
    all of them (the reconciler's safety net for a lost ``task_released``
    notification or a worker that died). A task counts as held while its
    lease has not run out. Returns how many messages were released.
    """
    rows = db.execute(
        text(
            """
            UPDATE celery_queue q
               SET visible_after = now(), after_task = NULL
             WHERE q.after_task IS NOT NULL
               AND (CAST(:after AS uuid) IS NULL OR q.after_task = CAST(:after AS uuid))
               AND NOT EXISTS (
                    SELECT 1 FROM tasks b
                     WHERE b."taskId" = q.after_task AND b.lease_until > now())
            RETURNING q.queue
            """
        ),
        {"after": str(after_task_id) if after_task_id else None},
    ).fetchall()
    for queue in {row.queue for row in rows}:
        db.execute(text("SELECT pg_notify(:channel, '')"), {"channel": NOTIFY_PREFIX + queue})
    db.commit()
    if rows:
        logger.info("Released %d parked task message(s)", len(rows))
    return len(rows)
