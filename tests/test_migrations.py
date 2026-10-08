"""Migrations on a scratch database (.github#5, .github#7 K).

The rest of the suite builds its schema with ``create_all``, which is how
the dropped ``uq_tasks_active_per_deployment`` went unnoticed. This test
runs the real migration chain instead:

- ``alembic upgrade head`` followed by ``alembic check`` (models and
  migrations agree, every index is declared on the models);
- the ``task_events`` trigger sends ``NOTIFY``;
- the worker role ``appstore_worker`` logs in (``app.worker_db_role``) and
  gets exactly its rights: queue, events, result columns; ``permission
  denied`` on users, apps and credentials;
- down to the previous revision and up again.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import psycopg
import pytest
from alembic.config import Config
from psycopg import sql

from alembic import command
from app.config import settings
from app.pgq import libpq_url
from app.worker_db_role import scram_sha256_verifier, set_worker_login
from tests.conftest import _TEST_DB_URL

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "8a6766f6326b"
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
            "ix_celery_queue_ready", "uq_celery_queue_task_id", "ix_task_events_task"} <= indexes


def _seed_task(conn) -> uuid.UUID:
    user_id, app_id, deployment_id, task_id = (uuid.uuid4() for _ in range(4))
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
        """INSERT INTO tasks ("taskId", "deploymentId", type, status) VALUES (%s, %s, 'DEPLOY', 'PENDING')""",
        (task_id, deployment_id),
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
    set_worker_login(url, WORKER_PASSWORD)

    host_part = url.split("@", 1)[1]
    worker_url = f"postgresql://appstore_worker:{WORKER_PASSWORD}@{host_part}"
    with psycopg.connect(worker_url, autocommit=True) as worker:
        # Allowed: the queue, appending events, the result columns of a task.
        worker.execute("SELECT count(*) FROM celery_queue").fetchone()
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
            "SELECT * FROM user_openstack_credentials",
            "SELECT * FROM deployments",
            "SELECT logs FROM tasks",
            "SELECT tf_state FROM tasks",
            "UPDATE tasks SET tf_state = 'x'",
            "UPDATE tasks SET outputs = 'x'",
            "INSERT INTO celery_queue (queue, payload) VALUES ('q', '{}')",
            "DELETE FROM task_events",
        ]
        for statement in denied:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                worker.execute(statement)


def test_scram_verifier_matches_the_rfc_test_vector_shape():
    verifier = scram_sha256_verifier("pencil", salt=b"\x00" * 16, iterations=4096)
    method, rest = verifier.split("$", 1)
    params, keys = rest.split("$")
    assert method == "SCRAM-SHA-256" and params.startswith("4096:")
    stored, server = keys.split(":")
    assert len(stored) == len(server) == 44  # base64 of 32 bytes
