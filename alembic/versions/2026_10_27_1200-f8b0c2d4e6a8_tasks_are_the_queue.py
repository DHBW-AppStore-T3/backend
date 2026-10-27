"""The task row is the queue: tasks.job / job_kwargs, no celery_queue, no celeryTaskId

Revision ID: f8b0c2d4e6a8
Revises: c5e7a9b1d3f5
Create Date: 2026-10-27 12:00:00.000000

Celery is gone. A task row is its own queue entry; the worker claims PENDING
rows from ``tasks`` (``worker/app/job_runtime.py``):

- ``tasks.job``: the registered job the worker runs (``pods.deploy_application``
  and so on); ``tasks.job_kwargs``: its keyword arguments. They may hold uploaded
  files and members' addresses: the trigger ``tasks_drop_job_kwargs`` drops them
  the moment the task leaves PENDING (claimed, cancelled, failed), whoever
  changes the status. The worker reads them in the statement that claims the
  task.
- Messages still waiting in ``celery_queue`` move into their PENDING tasks: the
  job is the message's ``task`` header, the arguments are the keyword arguments
  in its body (base64 JSON ``[args, kwargs, embed]``). A PENDING task without a
  message is failed: it could never run.
- ``celery_queue`` and ``tasks.celeryTaskId`` (always the task id) are dropped.
- ``appstore_worker`` may read the new columns and ``created_at`` (the claim
  order), not write them: a queued job's content stays the API's.

API and worker roll out together: the previous worker reads ``celery_queue``,
this one ``tasks.job``.

The downgrade recreates the empty ``celery_queue`` and ``celeryTaskId`` (set to
the task id). PENDING tasks are failed there; their arguments are not turned
back into Celery messages.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f8b0c2d4e6a8"
down_revision: Union[str, None] = "c5e7a9b1d3f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

WORKER_ROLE = "appstore_worker"
_WORKER_READS = "created_at, job, job_kwargs"
_QUEUE_COLUMNS = "visible_after, claimed_by, delivery_count, after_task"

_NEVER_QUEUED = (
    "Der Auftrag wurde nie an die Warteschlange übergeben (Umstellung der Warteschlange). "
    "Bitte die Aktion erneut starten."
)
_NOT_RUN = "Der Auftrag wurde nicht ausgeführt (Rückbau der Warteschlange). Bitte die Aktion erneut starten."


# Also in app/models.py, for the schema ``create_all`` builds (tests).
_DROP_JOB_KWARGS_FUNCTION = """
    CREATE OR REPLACE FUNCTION tasks_drop_job_kwargs() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        NEW.job_kwargs := NULL;
        RETURN NEW;
    END
    $$
"""
_DROP_JOB_KWARGS_TRIGGER = """
    CREATE TRIGGER tasks_drop_job_kwargs BEFORE UPDATE OF status ON tasks
        FOR EACH ROW WHEN (NEW.status <> 'PENDING' AND NEW.job_kwargs IS NOT NULL)
        EXECUTE FUNCTION tasks_drop_job_kwargs()
"""


def _fail_pending(condition: str, message: str) -> None:
    op.execute(
        sa.text(
            f"""
            UPDATE tasks SET status = 'FAILED', finished_at = timezone('UTC', now()), logs = :message
             WHERE status = 'PENDING' AND {condition}
            """
        ).bindparams(message=message)
    )


def upgrade() -> None:
    op.add_column("tasks", sa.Column("job", sa.String(), nullable=True))
    op.add_column("tasks", sa.Column("job_kwargs", postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    op.execute(
        """
        UPDATE tasks t
           SET job = q.payload -> 'headers' ->> 'task',
               job_kwargs = (CASE WHEN q.payload -> 'properties' ->> 'body_encoding' = 'base64'
                                  THEN convert_from(decode(q.payload ->> 'body', 'base64'), 'UTF8')
                                  ELSE q.payload ->> 'body' END)::jsonb -> 1
          FROM celery_queue q
         WHERE q.task_id = t."taskId" AND t.status = 'PENDING'
        """
    )
    _fail_pending("job IS NULL", _NEVER_QUEUED)

    op.drop_index("ix_celery_queue_after_task", table_name="celery_queue")
    op.drop_index("uq_celery_queue_task_id", table_name="celery_queue")
    op.drop_index("ix_celery_queue_ready", table_name="celery_queue")
    op.drop_table("celery_queue")
    op.drop_column("tasks", "celeryTaskId")

    op.execute(_DROP_JOB_KWARGS_FUNCTION)
    op.execute(_DROP_JOB_KWARGS_TRIGGER)
    op.execute(f"GRANT SELECT ({_WORKER_READS}) ON tasks TO {WORKER_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE SELECT ({_WORKER_READS}) ON tasks FROM {WORKER_ROLE}")
    op.execute("DROP TRIGGER tasks_drop_job_kwargs ON tasks")
    op.execute("DROP FUNCTION tasks_drop_job_kwargs()")
    _fail_pending("true", _NOT_RUN)

    op.add_column("tasks", sa.Column("celeryTaskId", sa.String(), nullable=True))
    op.execute('UPDATE tasks SET "celeryTaskId" = "taskId"::text')

    op.create_table(
        "celery_queue",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("queue", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=True),
        sa.Column("after_task", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("visible_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("delivery_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_index("ix_celery_queue_ready", "celery_queue", ["queue", "visible_after", "id"])
    op.create_index(
        "uq_celery_queue_task_id",
        "celery_queue",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("task_id IS NOT NULL"),
    )
    op.create_index(
        "ix_celery_queue_after_task",
        "celery_queue",
        ["after_task"],
        postgresql_where=sa.text("after_task IS NOT NULL"),
    )
    # The rights of migrations 5e1f0c2a9b7d and 9e1a3c5b7d2f.
    op.execute(f"GRANT SELECT, DELETE ON celery_queue TO {WORKER_ROLE}")
    op.execute(f"GRANT UPDATE ({_QUEUE_COLUMNS}) ON celery_queue TO {WORKER_ROLE}")

    op.drop_column("tasks", "job_kwargs")
    op.drop_column("tasks", "job")
