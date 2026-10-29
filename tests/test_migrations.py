"""Migrations on a scratch database (.github#5, .github#7 K).

The rest of the suite builds its schema with ``create_all``, which is how
the dropped ``uq_tasks_active_per_deployment`` went unnoticed. This test
runs the real migration chain instead:

- ``alembic upgrade head`` followed by ``alembic check`` (models and
  migrations agree, every index is declared on the models);
- the ``task_events`` trigger sends ``NOTIFY``;
- the worker role ``appstore_worker`` logs in (``app.worker_db_role``) and
  gets exactly its rights: claiming queued tasks, events, result columns;
  ``permission denied`` on users, apps, deployments and on a queued task's
  job and arguments;
- messages still in the old ``celery_queue`` move into their tasks;
- down to the previous revision and up again.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import psycopg
import pytest
from alembic.config import Config
from psycopg import sql
from psycopg.types.json import Jsonb

from alembic import command
from app.config import settings
from app.database import libpq_url
from app.worker_db_role import scram_sha256_verifier, set_worker_login
from tests.conftest import _TEST_DB_URL

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "8a6766f6326b"
# The last revision with Celery's broker table.
CELERY_QUEUE_REVISION = "c5e7a9b1d3f5"
# Two messages as the pgq transport stored them (a deploy and a destroy parked
# behind it), sent with the code of that revision.
CELERY_MESSAGES = json.loads((BACKEND_ROOT / "tests" / "golden" / "celery_queue_messages.json").read_text("utf-8"))
WORKER_PASSWORD = "migration-test-worker-password"


def _with_database(url: str, dbname: str) -> str:
    base, _, _ = libpq_url(url).rpartition("/")
    return f"{base}/{dbname}"


@pytest.fixture
def scratch_db(monkeypatch):
    """A fresh, empty database on the test server; alembic points at it."""
    admin_url = libpq_url(_TEST_DB_URL)
    name = f"migtest_{uuid.uuid4().hex[:10]}"
    try:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    except psycopg.errors.InsufficientPrivilege:
        pytest.skip("the test user may not create databases")
    url = _with_database(admin_url, name)
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    # No ini file: env.py would hand it to logging.fileConfig, which
    # disables every existing logger for the rest of the test session.
    config = Config()
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    try:
        yield url, config
    finally:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def test_upgrade_check_and_round_trip(scratch_db):
    url, config = scratch_db
    command.upgrade(config, "head")
    command.check(config)  # raises if the models and the migrations differ
    command.downgrade(config, PREVIOUS_REVISION)
    command.upgrade(config, "head")
    command.check(config)

    with psycopg.connect(url) as conn:
        indexes = {row[0] for row in conn.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")}
    assert {"uq_tasks_active_per_deployment", "ix_apps_live", "ix_deployments_live",
            "ix_task_events_task"} <= indexes
    assert not {i for i in indexes if "celery" in i}


def _seed_task(conn, status: str = "PENDING", task_id: uuid.UUID | None = None) -> uuid.UUID:
    user_id, app_id, deployment_id = (uuid.uuid4() for _ in range(3))
    task_id = task_id or uuid.uuid4()
    conn.execute(
        """INSERT INTO users ("userId", email, username, role) VALUES (%s, %s, 'u', 'TEACHER')""",
        (user_id, f"{user_id}@x.de"),
    )
    conn.execute(
        """INSERT INTO apps ("appId", name, "userId", is_private) VALUES (%s, 'a', %s, false)""",
        (app_id, user_id),
    )
    conn.execute(
        """INSERT INTO deployments ("deploymentId", name, "userId", "appId") VALUES (%s, 'd', %s, %s)""",
        (deployment_id, user_id, app_id),
    )
    conn.execute(
        """INSERT INTO tasks ("taskId", "deploymentId", type, status) VALUES (%s, %s, 'DEPLOY', %s)""",
        (task_id, deployment_id, status),
    )
    return task_id


def test_task_events_trigger_notifies(scratch_db):
    url, config = scratch_db
    command.upgrade(config, "head")
    with psycopg.connect(url, autocommit=True) as conn:
        task_id = _seed_task(conn)
        conn.execute("LISTEN task_events")
        event_id = conn.execute(
            "INSERT INTO task_events (task_id, type, payload) VALUES (%s, 'task-log', '{}') RETURNING id",
            (task_id,),
        ).fetchone()[0]
        notes = [n.payload for n in conn.notifies(timeout=2, stop_after=1)]
    assert notes == [f"{task_id}:{event_id}:task-log"]


def test_worker_role_has_only_its_rights(scratch_db):
    url, config = scratch_db
    command.upgrade(config, "head")
    with psycopg.connect(url, autocommit=True) as conn:
        task_id = _seed_task(conn)
        conn.execute("""UPDATE tasks SET job = 'pods.deploy_application', job_kwargs = '{"a": 1}'""")
    set_worker_login(url, WORKER_PASSWORD)

    host_part = url.split("@", 1)[1]
    worker_url = f"postgresql://appstore_worker:{WORKER_PASSWORD}@{host_part}"
    with psycopg.connect(worker_url, autocommit=True) as worker:
        # Allowed: claiming a queued task (as worker/app/job_runtime.py does),
        # appending events, the result columns of a task.
        claimed = worker.execute(
            """
            WITH next AS (
                SELECT t."taskId", t.job, t.job_kwargs FROM tasks t
                 WHERE t.status = 'PENDING'
                   AND NOT EXISTS (SELECT 1 FROM tasks b
                                    WHERE b."deploymentId" = t."deploymentId" AND b.lease_until > now())
                 ORDER BY t.created_at LIMIT 1 FOR UPDATE SKIP LOCKED)
            UPDATE tasks SET status = 'RUNNING', started_at = now(), claimed_by = 'w',
                   lease_until = now() + interval '1 minute'
              FROM next WHERE tasks."taskId" = next."taskId"
            RETURNING tasks."taskId", tasks."deploymentId", tasks.type, next.job, next.job_kwargs
            """
        ).fetchone()
        assert claimed[0] == task_id and claimed[3:] == ("pods.deploy_application", {"a": 1})
        # The trigger dropped the arguments, although the role may not write them.
        assert worker.execute('SELECT job_kwargs FROM tasks WHERE "taskId" = %s', (task_id,)).fetchone() == (None,)
        worker.execute(
            "INSERT INTO task_events (task_id, type, payload) VALUES (%s, 'task-progress', '{}')", (task_id,)
        )
        worker.execute(
            """UPDATE tasks SET status = 'RUNNING', claimed_by = 'w', lease_until = now(), logs = '[]',
                      outputs_enc = '\\x00', current_phase = 'X', progress_pct = 1
                WHERE "taskId" = %s RETURNING "deploymentId", type""",
            (task_id,),
        ).fetchone()
        worker.execute("SELECT pg_try_advisory_lock(1), pg_advisory_unlock(1)").fetchone()

        denied = [
            "SELECT * FROM users",
            "SELECT * FROM apps",
            "SELECT * FROM deployments",
            "SELECT logs FROM tasks",
            "SELECT tf_state FROM tasks",
            "UPDATE tasks SET tf_state = 'x'",
            "UPDATE tasks SET outputs = 'x'",
            # A queued job's content is the API's: no queueing, no rewriting it.
            """INSERT INTO tasks ("taskId", "deploymentId", type, status) VALUES (gen_random_uuid(), gen_random_uuid(), 'DEPLOY', 'PENDING')""",
            "UPDATE tasks SET job = 'pods.destroy_deployment'",
            "UPDATE tasks SET job_kwargs = '{}'",
            "SELECT * FROM lti_nonces",
            "DELETE FROM task_events",
        ]
        for statement in denied:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                worker.execute(statement)


def test_queued_celery_messages_move_into_their_tasks(scratch_db):
    url, config = scratch_db
    command.upgrade(config, CELERY_QUEUE_REVISION)
    deploy, destroy = CELERY_MESSAGES
    with psycopg.connect(url, autocommit=True) as conn:
        # Waiting deploy: its message carries the job and its arguments.
        waiting = _seed_task(conn, task_id=uuid.UUID(deploy["task_id"]))
        # A destroy parked behind a cancelled job that a worker still holds.
        parked = _seed_task(conn, task_id=uuid.UUID(destroy["task_id"]))
        # Running: its message belongs to the worker running it.
        running = _seed_task(conn, status="RUNNING")
        # Pending without a message: it never reached the queue.
        lost = _seed_task(conn)
        for task_id, message in ((waiting, deploy), (parked, destroy), (running, deploy)):
            conn.execute(
                "INSERT INTO celery_queue (queue, payload, task_id, after_task) VALUES ('pods', %s, %s, %s)",
                (Jsonb(message["payload"]), task_id, message["after_task"]),
            )

    command.upgrade(config, "head")

    with psycopg.connect(url) as conn:
        def row(task_id):
            return conn.execute(
                'SELECT status, job, job_kwargs, logs FROM tasks WHERE "taskId" = %s', (task_id,)
            ).fetchone()

        status, job, kwargs, _ = row(waiting)
        assert (status, job) == ("PENDING", "pods.deploy_application")
        assert kwargs["user_vars"] == {"variables": {"greeting": "Grüße", "n": 3}}
        assert kwargs["teams"]["Team 1"][0]["email"] == "a@example.org"
        assert row(parked)[:3] == ("PENDING", "pods.destroy_deployment", {"deployment_id": kwargs["deployment_id"]})
        assert row(running)[:3] == ("RUNNING", None, None)
        status, job, _, logs = row(lost)
        assert status == "FAILED" and job is None and "nie an die Warteschlange" in logs
        tables = {r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
        assert "celery_queue" not in tables
        columns = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'tasks'"
        )}
        assert {"job", "job_kwargs"} <= columns and "celeryTaskId" not in columns


def test_scram_verifier_matches_the_rfc_test_vector_shape():
    verifier = scram_sha256_verifier("pencil", salt=b"\x00" * 16, iterations=4096)
    method, rest = verifier.split("$", 1)
    params, keys = rest.split("$")
    assert method == "SCRAM-SHA-256" and params.startswith("4096:")
    stored, server = keys.split(":")
    assert len(stored) == len(server) == 44  # base64 of 32 bytes


def test_concurrent_migrations_take_turns(scratch_db):
    """Two API replicas migrating at once: the second waits for the first (advisory lock)."""
    import threading

    url, config = scratch_db
    done = threading.Event()
    errors: list[BaseException] = []

    def migrate():
        try:
            command.upgrade(config, "head")
        except BaseException as exc:  # noqa: BLE001 - reported by the assertion below
            errors.append(exc)
        finally:
            done.set()

    with psycopg.connect(url) as holder:
        holder.execute("SELECT pg_advisory_xact_lock(hashtext('appstore-backend:migrations'))")
        worker = threading.Thread(target=migrate, daemon=True)
        worker.start()
        # While another migration holds the lock, this one does not start.
        assert not done.wait(timeout=3)
        holder.rollback()  # releases the transaction-scoped lock
    assert done.wait(timeout=60)
    worker.join(timeout=5)
    assert errors == []
    from alembic.script import ScriptDirectory

    with psycopg.connect(url) as conn:
        (version,) = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    assert version == ScriptDirectory.from_config(config).get_current_head()
