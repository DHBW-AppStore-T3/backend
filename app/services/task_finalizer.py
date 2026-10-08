"""Follow-up work of finished tasks, exactly once (.github#5).

The follow-up of a task used to run in the Celery event listener, once per
uvicorn process: four soft-deletes and four access mails per deploy. Here it
is claimed through ``tasks.finalized_at``; whichever API process sets it
does the work, every other process skips the task:

- **destroy succeeded** → soft-delete the deployment, in the same
  transaction as the claim (a crash repeats it, which is harmless);
- **deploy succeeded** → the access mails. The claim is committed first: a
  crash in between loses the mails (members can resend them) instead of
  sending them twice.

Two ways in: the background pass (``finalize_pending``, rows claimed with
``SKIP LOCKED``) and the live stream, which finalizes a task before it sends
the terminal event (``finalize_task``), so a client that reloads after a
successful destroy no longer finds the deployment.

``purge_events`` deletes the live events of tasks that finished more than
``EVENT_RETENTION_DAYS`` ago; the full transcript stays in ``tasks.logs``.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.crud import deployments as crud_deployments
from app.models import Task, TaskStatus, TaskType
from app.services import deployment_notifier, task_results

logger = logging.getLogger(__name__)

FINISHED = (TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED)
# Rows per background pass; a backlog is worked off over several passes.
BATCH = 20
EVENT_RETENTION_DAYS = 7


def _follow_up(db: Session, task: Task) -> None:
    """Mark ``task`` finalized and run its follow-up; commits."""
    task.finalized_at = text("now()")
    if task.status == TaskStatus.SUCCESS and task.type == TaskType.DESTROY:
        # The OpenStack resources are gone: hide the deployment, keep the
        # row and its tasks for the record. Commits together with the mark.
        crud_deployments.soft_delete_deployment(db, task.deploymentId)
        db.commit()
        logger.info("Soft-deleted deployment %s after its destroy", task.deploymentId)
        return
    db.commit()
    if task.status == TaskStatus.SUCCESS and task.type == TaskType.DEPLOY:
        try:
            deployment_notifier.notify_deployment_succeeded(
                db, task.deploymentId, terraform_outputs=task_results.outputs(task)
            )
        except Exception:
            # Already finalized: retrying would resend the mails that did
            # go out. The notifier handles SMTP errors itself.
            logger.exception("Deploy notification for deployment %s failed", task.deploymentId)


def finalize_task(db: Session, task_id: uuid.UUID) -> bool:
    """Finalize one finished task now; waits if another process is at it. True if this call did it."""
    task = db.scalars(
        select(Task)
        .where(Task.taskId == task_id, Task.status.in_(FINISHED), Task.finalized_at.is_(None))
        .with_for_update()
    ).first()
    if task is None:
        db.rollback()
        return False
    _follow_up(db, task)
    return True


def finalize_pending(db: Session) -> int:
    """Finalize up to ``BATCH`` finished tasks, oldest first. Returns how many."""
    done = 0
    for _ in range(BATCH):
        task = db.scalars(
            select(Task)
            .where(Task.status.in_(FINISHED), Task.finalized_at.is_(None))
            .order_by(Task.finished_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        ).first()
        if task is None:
            db.rollback()
            break
        try:
            _follow_up(db, task)
        except Exception:
            logger.exception("Follow-up of task %s failed", task.taskId)
            db.rollback()
            # Leave it finalized anyway; a follow-up that fails every time
            # must not block the queue of finished tasks.
            db.execute(
                text('UPDATE tasks SET finalized_at = now() WHERE "taskId" = :id AND finalized_at IS NULL'),
                {"id": task.taskId},
            )
            db.commit()
        done += 1
    return done


def purge_events(db: Session) -> int:
    """Delete the events of tasks that finished more than ``EVENT_RETENTION_DAYS`` ago."""
    deleted = db.execute(
        text(
            """
            DELETE FROM task_events e
             USING tasks t
             WHERE e.task_id = t."taskId"
               AND t.finalized_at IS NOT NULL
               AND t.finished_at < timezone('UTC', now()) - make_interval(days => :days)
            """
        ),
        {"days": EVENT_RETENTION_DAYS},
    ).rowcount
    db.commit()
    if deleted:
        logger.info("Purged %d task events older than %d days", deleted, EVENT_RETENTION_DAYS)
    return deleted
