"""postgres queue: celery_queue, task_events, task lease columns, worker role

Revision ID: 5e1f0c2a9b7d
Revises: 4d0e9b1a8c6f
Create Date: 2026-10-09 12:00:00.000000

RabbitMQ and Redis go away (.github#5); Celery runs over the ``pgq``
transport (``app/pgq.py``) on our own Postgres. Relies on
``uq_tasks_active_per_deployment`` (previous revision).

1. ``celery_queue``: the broker table. ``after_task`` parks a message until
   that task's worker let go of it (destroy after cancel, see
   ``task_service``).
2. ``task_events``: progress, log and terminal events of a task. A trigger
   sends ``NOTIFY task_events, '<task id>:<event id>:<type>'`` per row; the
   API's live stream listens to it.
3. ``tasks``: lease (``claimed_by``, ``lease_until``), cancel request,
   ``finalized_at`` (follow-up work done exactly once) and ``outputs_enc``
   (Fernet; Terraform outputs and state as the worker reports them).
   Finished tasks are marked finalized so the new finalizer does not redo
   their follow-up work (access mails) after the upgrade.
4. Role ``appstore_worker`` (NOLOGIN here; the deployment sets LOGIN and the
   password with ``python -m app.worker_db_role``). It may use the queue,
   append events and write the result columns of ``tasks``; nothing else.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "5e1f0c2a9b7d"
down_revision: Union[str, None] = "4d0e9b1a8c6f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

WORKER_ROLE = "appstore_worker"


def upgrade() -> None:
    # 1. Broker table.
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

    # 2. Task events + NOTIFY. Only ids and the type go into the payload
    #    (NOTIFY payloads are capped at 8000 bytes); listeners read the row.
    op.create_table(
        "task_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "task_id",
            sa.UUID(),
            sa.ForeignKey("tasks.taskId", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_task_events_task", "task_events", ["task_id", "id"])
    op.execute(
        """
        CREATE FUNCTION task_events_notify() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('task_events', NEW.task_id::text || ':' || NEW.id::text || ':' || NEW.type);
            RETURN NULL;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER task_events_notify AFTER INSERT ON task_events
            FOR EACH ROW EXECUTE FUNCTION task_events_notify()
        """
    )

    # 3. Lease, cancel, exactly-once follow-up and encrypted results.
    op.add_column("tasks", sa.Column("claimed_by", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("outputs_enc", sa.LargeBinary(), nullable=True))
    op.execute("UPDATE tasks SET finalized_at = now() WHERE status IN ('SUCCESS', 'FAILED', 'CANCELLED')")

    # 4. The worker's role. Roles are cluster-wide; it may exist already
    #    (a second database on the same server, a re-run after a downgrade).
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{WORKER_ROLE}') THEN
                CREATE ROLE {WORKER_ROLE} NOLOGIN;
            END IF;
            EXECUTE format('GRANT CONNECT ON DATABASE %I TO {WORKER_ROLE}', current_database());
        END
        $$
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {WORKER_ROLE}")
    op.execute(f"GRANT SELECT, UPDATE, DELETE ON celery_queue TO {WORKER_ROLE}")
    op.execute(f"GRANT INSERT ON task_events TO {WORKER_ROLE}")
    op.execute(f"GRANT USAGE ON SEQUENCE task_events_id_seq TO {WORKER_ROLE}")
    op.execute(
        f"""
        GRANT SELECT ("taskId", "deploymentId", type, status, claimed_by, lease_until, cancel_requested_at)
           ON tasks TO {WORKER_ROLE}
        """
    )
    op.execute(
        f"""
        GRANT UPDATE (status, started_at, finished_at, logs, outputs_enc, current_phase, progress_pct,
                      claimed_by, lease_until)
           ON tasks TO {WORKER_ROLE}
        """
    )


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON tasks FROM {WORKER_ROLE}")
    op.execute(f"REVOKE ALL ON task_events FROM {WORKER_ROLE}")
    op.execute(f"REVOKE ALL ON SEQUENCE task_events_id_seq FROM {WORKER_ROLE}")
    op.execute(f"REVOKE ALL ON celery_queue FROM {WORKER_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {WORKER_ROLE}")
    op.execute(
        f"""
        DO $$
        BEGIN
            EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM {WORKER_ROLE}', current_database());
            -- Still used by another database of this server: keep it.
            DROP ROLE {WORKER_ROLE};
        EXCEPTION WHEN dependent_objects_still_exist THEN
            RAISE NOTICE 'role {WORKER_ROLE} still has privileges elsewhere, not dropped';
        END
        $$
        """
    )

    op.drop_column("tasks", "outputs_enc")
    op.drop_column("tasks", "finalized_at")
    op.drop_column("tasks", "cancel_requested_at")
    op.drop_column("tasks", "lease_until")
    op.drop_column("tasks", "claimed_by")

    op.execute("DROP TRIGGER task_events_notify ON task_events")
    op.execute("DROP FUNCTION task_events_notify()")
    op.drop_index("ix_task_events_task", table_name="task_events")
    op.drop_table("task_events")

    op.drop_index("ix_celery_queue_after_task", table_name="celery_queue")
    op.drop_index("uq_celery_queue_task_id", table_name="celery_queue")
    op.drop_index("ix_celery_queue_ready", table_name="celery_queue")
    op.drop_table("celery_queue")
