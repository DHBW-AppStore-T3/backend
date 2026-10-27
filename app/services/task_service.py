"""Task lifecycle helpers.

The task row is the queue entry (.github#5): `enqueue_task_in_tx` inserts it
as PENDING with the job the worker runs (``tasks.job``) and the job's
arguments (``tasks.job_kwargs``), and sends ``NOTIFY task_queued``, all in
the caller's transaction. The caller commits the surrounding business state
and the task atomically; Postgres delivers the NOTIFY with the commit. There
is no second step that could fail after the commit, so a task is either
queued with its deployment or not at all.

A worker claims the oldest PENDING task of a deployment that no other task
holds a lease on (``worker/app/job_runtime.py``). The destroy that follows a
cancel therefore waits until the cancelled job's worker let go of it
(security review A-5, race 2).
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.crud import tasks as crud_tasks
from app.models import Task, TaskStatus, TaskType
from app.task_contract import NOTIFY_TASK_QUEUED

# Name of the Postgres partial unique index backing the "one active
# task per deployment" rule. String-matched when translating
# IntegrityError → ActiveTaskExistsError so other unique-constraint
# violations aren't swallowed.
_ACTIVE_TASK_UNIQUE_INDEX = "uq_tasks_active_per_deployment"


class ActiveTaskExistsError(Exception):
    """A PENDING/RUNNING task already exists for this deployment."""


def enqueue_task_in_tx(
    db: Session,
    deployment_id: uuid.UUID,
    task_type: TaskType,
    job: str,
    job_kwargs: dict[str, Any],
) -> Task:
    """Queue a PENDING task in the caller's transaction.

    Does NOT call `db.commit()` — the caller is responsible for
    committing the surrounding state alongside this row, so that
    deployment + teams + task are all visible (or all rolled back)
    atomically. Workers are woken with the commit.

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

    db_task = Task(
        taskId=uuid.uuid4(),
        deploymentId=deployment_id,
        type=task_type,
        status=TaskStatus.PENDING,
        job=job,
        job_kwargs=job_kwargs,
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
    db.execute(text("SELECT pg_notify(:channel, '')"), {"channel": NOTIFY_TASK_QUEUED})
    db.refresh(db_task)
    return db_task
