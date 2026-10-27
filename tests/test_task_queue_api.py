"""Queueing, cancel, live stream, follow-up and reconciler on the Postgres queue (.github#5).

Integration tests against the test database: the task row with its job and
the ``task_queued`` wake-up, the destroy queued with a cancel, the SSE stream
with ``Last-Event-ID``, the exactly-once follow-up and the reconciler's
treatment of dead workers; the job arguments are dropped when a task leaves
PENDING. Which task a worker may claim is tested in the worker
(``tests/test_job_runtime.py``).
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.database import libpq_url
from app.models import (
    App,
    Deployment,
    Task,
    TaskEvent,
    TaskStatus,
    TaskType,
)
from app.services import reconciler, task_finalizer
from app.task_contract import (
    EVENT_FAILED,
    EVENT_LOG,
    EVENT_PROGRESS,
    EVENT_REVOKED,
    EVENT_SUCCEEDED,
    FAILURE_KIND_WORKER_LOST,
    NOTIFY_TASK_CANCEL,
    NOTIFY_TASK_QUEUED,
    seal_results,
    terminal_payload,
)
from app.utils import crypto
from tests.conftest import _TEST_DB_URL, TestingSessionLocal

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _sessions_on_test_db(monkeypatch):
    """Code that opens its own sessions (stream, reconciler) must use the test database."""
    monkeypatch.setattr("app.routers.deployments.SessionLocal", TestingSessionLocal)
    monkeypatch.setattr("app.services.reconciler.SessionLocal", TestingSessionLocal)


def _seed(db, user, *, task_type=TaskType.DEPLOY, task_status=TaskStatus.SUCCESS, **task_fields) -> tuple:
    """App + deployment + one task."""
    app = App(appId=uuid.uuid4(), name=f"app-{uuid.uuid4().hex[:6]}", userId=user.userId,
              git_link="https://example.com/acme/repo.git")
    db.add(app)
    db.flush()
    deployment = Deployment(deploymentId=uuid.uuid4(), name=f"d-{uuid.uuid4().hex[:6]}", appId=app.appId,
                            userId=user.userId, releaseTag="v1.0.0", runtime="kubernetes",
                            userInputVar=json.dumps({"terraform": {}}))
    db.add(deployment)
    db.flush()
    task_id = uuid.uuid4()
    task_fields.setdefault("created_at", datetime.utcnow())
    task = Task(taskId=task_id, deploymentId=deployment.deploymentId, type=task_type, status=task_status,
                **task_fields)
    db.add(task)
    db.commit()
    return deployment, task


def _events(db, task_id):
    db.expire_all()
    return db.query(TaskEvent).filter(TaskEvent.task_id == task_id).order_by(TaskEvent.id).all()


# ----------------------------------------------------------------
# Queueing
# ----------------------------------------------------------------
def test_a_task_is_queued_with_its_job_and_wakes_the_workers(client, db, mock_user):
    deployment, _ = _seed(db, mock_user)
    with psycopg.connect(libpq_url(_TEST_DB_URL), autocommit=True) as listener:
        listener.execute(f"LISTEN {NOTIFY_TASK_QUEUED}")
        response = client.post(f"/deployments/{deployment.deploymentId}/pause")
        assert response.status_code == 202
        notified = list(listener.notifies(timeout=2, stop_after=1))
    assert len(notified) == 1
    task_id = uuid.UUID(response.json()["task_id"])

    db.expire_all()
    task = db.get(Task, task_id)
    assert task.status == TaskStatus.PENDING and task.lease_until is None
    assert task.job == "pods.pause_deployment"
    assert task.job_kwargs["deployment_id"] == str(deployment.deploymentId)


def test_second_active_task_is_refused_by_the_database(db, mock_user):
    deployment, _ = _seed(db, mock_user, task_status=TaskStatus.RUNNING)
    db.add(Task(taskId=uuid.uuid4(), deploymentId=deployment.deploymentId, type=TaskType.PAUSE,
                status=TaskStatus.PENDING))
    with pytest.raises(IntegrityError, match="uq_tasks_active_per_deployment"):
        db.commit()


# ----------------------------------------------------------------
# Cancel
# ----------------------------------------------------------------
def test_cancel_before_a_worker_claimed_the_job(client, db, mock_user):
    deployment, task = _seed(db, mock_user, task_status=TaskStatus.PENDING, job="pods.deploy_application",
                             job_kwargs={"deployment_id": "x", "user_vars": {"files": "..."}})

    response = client.post(f"/deployments/{deployment.deploymentId}/cancel")
    assert response.status_code == 202
    destroy_id = uuid.UUID(response.json()["task_id"])

    db.expire_all()
    cancelled = db.get(Task, task.taskId)
    # No longer PENDING, so no worker claims it; its arguments are gone.
    assert cancelled.status == TaskStatus.CANCELLED and cancelled.cancel_requested_at is not None
    assert cancelled.job_kwargs is None
    assert [e.type for e in _events(db, task.taskId)] == [EVENT_REVOKED]
    destroy = db.get(Task, destroy_id)
    assert destroy.status == TaskStatus.PENDING and destroy.job == "pods.destroy_deployment"


def test_cancel_while_a_worker_runs_the_job(client, db, mock_user):
    deployment, task = _seed(
        db, mock_user, task_status=TaskStatus.RUNNING, job="pods.deploy_application",
        claimed_by="worker-1:42:ab", lease_until=datetime.now(UTC) + timedelta(minutes=2),
    )

    with psycopg.connect(libpq_url(_TEST_DB_URL), autocommit=True) as listener:
        listener.execute(f"LISTEN {NOTIFY_TASK_CANCEL}")
        response = client.post(f"/deployments/{deployment.deploymentId}/cancel")
        assert response.status_code == 202
        notified = [n.payload for n in listener.notifies(timeout=2, stop_after=1)]
    assert notified == [str(task.taskId)]
    destroy_id = uuid.UUID(response.json()["task_id"])

    db.expire_all()
    cancelled = db.get(Task, task.taskId)
    assert cancelled.status == TaskStatus.CANCELLED and cancelled.cancel_requested_at is not None
    # Still the worker's until it lets go; the destroy waits for that (no worker
    # claims a task of a deployment another task holds a lease on).
    assert cancelled.claimed_by == "worker-1:42:ab" and cancelled.lease_until is not None
    destroy = db.get(Task, destroy_id)
    assert destroy.status == TaskStatus.PENDING and destroy.job == "pods.destroy_deployment"


# ----------------------------------------------------------------
# Live stream
# ----------------------------------------------------------------
def _add_events(db, task, *types):
    ids = []
    for event_type in types:
        if event_type in (EVENT_SUCCEEDED, EVENT_FAILED, EVENT_REVOKED):
            payload = terminal_payload(event_type, deployment_id=str(task.deploymentId), task_id=str(task.taskId),
                                       task_type=task.type.value)
        elif event_type == EVENT_PROGRESS:
            payload = {"type": event_type, "phase": "STARTING", "phase_index": 1, "total_phases": 4,
                       "progress_pct": 25}
        else:
            payload = {"type": event_type, "level": "INFO", "message": f"line {len(ids)}",
                       "iso_timestamp": "2026-10-09T12:00:00Z"}
        event = TaskEvent(task_id=task.taskId, type=event_type, payload=payload)
        db.add(event)
        db.flush()
        ids.append(event.id)
    db.commit()
    return ids


def _frames(client, deployment_id, headers=None) -> list[dict]:
    frames = []
    with client.stream("GET", f"/deployments/{deployment_id}/stream", headers=headers or {}) as response:
        assert response.status_code == 200
        block: dict = {}
        for line in response.iter_lines():
            if not line:
                if block:
                    frames.append(block)
                block = {}
            elif line.startswith(":"):
                continue
            else:
                key, _, value = line.partition(": ")
                block[key] = value
    return frames


def test_stream_sends_backfill_then_ends_with_the_terminal_event(client, db, mock_user):
    deployment, task = _seed(db, mock_user, task_status=TaskStatus.RUNNING)
    ids = _add_events(db, task, EVENT_PROGRESS, EVENT_LOG, EVENT_LOG, EVENT_SUCCEEDED)

    frames = _frames(client, deployment.deploymentId)
    assert [f["event"] for f in frames] == ["snapshot", "progress", "log", "log", "succeeded"]
    assert [int(f["id"]) for f in frames[1:]] == ids
    assert json.loads(frames[-1]["data"])["status"] == "success"


def test_stream_resumes_after_last_event_id(client, db, mock_user):
    deployment, task = _seed(db, mock_user, task_status=TaskStatus.RUNNING)
    ids = _add_events(db, task, EVENT_PROGRESS, EVENT_LOG, EVENT_LOG, EVENT_LOG, EVENT_SUCCEEDED)

    frames = _frames(client, deployment.deploymentId, headers={"Last-Event-ID": str(ids[2])})
    assert [int(f["id"]) for f in frames[1:]] == ids[3:]  # no gap, no repeat


def test_stream_finalizes_a_destroy_before_its_terminal_event(client, db, mock_user):
    deployment, task = _seed(db, mock_user, task_type=TaskType.DESTROY, task_status=TaskStatus.RUNNING)
    _add_events(db, task, EVENT_PROGRESS)

    def worker_finishes():
        time.sleep(0.8)
        with TestingSessionLocal() as s:
            s.execute(text('UPDATE tasks SET status = \'SUCCESS\', finished_at = now() WHERE "taskId" = :id'),
                      {"id": task.taskId})
            s.add(TaskEvent(task_id=task.taskId, type=EVENT_SUCCEEDED,
                            payload=terminal_payload(EVENT_SUCCEEDED, deployment_id=str(deployment.deploymentId),
                                                     task_id=str(task.taskId), task_type="destroy")))
            s.commit()

    finisher = threading.Thread(target=worker_finishes)
    finisher.start()
    frames = _frames(client, deployment.deploymentId)
    finisher.join()
    assert frames[-1]["event"] == "succeeded"
    db.expire_all()
    # Soft-deleted before the terminal frame went out, exactly once.
    assert db.get(Deployment, deployment.deploymentId).deleted_at is not None
    assert db.get(Task, task.taskId).finalized_at is not None


def test_stream_of_a_finished_task_is_just_the_snapshot(client, db, mock_user):
    deployment, task = _seed(db, mock_user, task_status=TaskStatus.SUCCESS)
    _add_events(db, task, EVENT_PROGRESS, EVENT_SUCCEEDED)
    frames = _frames(client, deployment.deploymentId)
    assert [f["event"] for f in frames] == ["snapshot"]


def test_stream_closes_a_task_that_ended_without_terminal_event(client, db, mock_user):
    deployment, task = _seed(db, mock_user, task_status=TaskStatus.PENDING)

    def dispatch_fails():
        time.sleep(0.5)
        with TestingSessionLocal() as s:
            s.execute(text('UPDATE tasks SET status = \'FAILED\' WHERE "taskId" = :id'), {"id": task.taskId})
            s.commit()

    t = threading.Thread(target=dispatch_fails)
    t.start()
    frames = _frames(client, deployment.deploymentId)
    t.join()
    assert [f["event"] for f in frames] == ["snapshot", "failed"]


# ----------------------------------------------------------------
# Follow-up exactly once
# ----------------------------------------------------------------
def test_follow_up_runs_once_with_parallel_finalizers(db, mock_user):
    """Four API processes finalize at once: the destroy's soft-delete runs once."""
    deployment, task = _seed(db, mock_user, task_type=TaskType.DESTROY, task_status=TaskStatus.SUCCESS)
    calls = []
    barrier = threading.Barrier(4)
    real_soft_delete = task_finalizer.crud_deployments.soft_delete_deployment

    def counting_soft_delete(session, deployment_id):
        calls.append(deployment_id)
        return real_soft_delete(session, deployment_id)

    def run(fn):
        barrier.wait()
        with TestingSessionLocal() as s:
            fn(s)

    with patch("app.services.task_finalizer.crud_deployments.soft_delete_deployment",
               side_effect=counting_soft_delete):
        threads = [threading.Thread(target=run, args=(task_finalizer.finalize_pending,)) for _ in range(3)]
        threads.append(threading.Thread(target=run, args=(lambda s: task_finalizer.finalize_task(s, task.taskId),)))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

    assert calls == [deployment.deploymentId]
    db.expire_all()
    assert db.get(Task, task.taskId).finalized_at is not None


def test_a_successful_deploy_is_finalized_without_side_effects(db, mock_user):
    """No access mails any more (.github#7 I): finalizing a deploy only marks it."""
    deployment, task = _seed(db, mock_user, task_type=TaskType.DEPLOY, task_status=TaskStatus.SUCCESS,
                             outputs_enc=seal_results(crypto.results_cipher, terraform_outputs={"x": {"value": 1}},
                                                      tf_state=None))
    with TestingSessionLocal() as s:
        assert task_finalizer.finalize_task(s, task.taskId) is True
    db.expire_all()
    assert db.get(Task, task.taskId).finalized_at is not None
    assert db.get(Deployment, deployment.deploymentId).deleted_at is None


def test_follow_up_of_a_destroy_soft_deletes(db, mock_user):
    deployment, task = _seed(db, mock_user, task_type=TaskType.DESTROY, task_status=TaskStatus.SUCCESS)
    assert task_finalizer.finalize_pending(db) == 1
    db.expire_all()
    assert db.get(Deployment, deployment.deploymentId).deleted_at is not None
    assert task_finalizer.finalize_pending(db) == 0


# ----------------------------------------------------------------
# Reconciler
# ----------------------------------------------------------------
def test_reconciler_fails_a_task_whose_worker_died(db, mock_user):
    _, lost = _seed(db, mock_user, task_status=TaskStatus.RUNNING, claimed_by="w:1",
                    lease_until=datetime.now(UTC) - timedelta(seconds=5))
    _, alive = _seed(db, mock_user, task_status=TaskStatus.RUNNING, claimed_by="w:2",
                     lease_until=datetime.now(UTC) + timedelta(minutes=1))

    reconciler.reconcile_once()

    db.expire_all()
    lost_row, alive_row = db.get(Task, lost.taskId), db.get(Task, alive.taskId)
    assert lost_row.status == TaskStatus.FAILED and lost_row.lease_until is None
    assert "antwortet nicht mehr" in lost_row.logs
    (event,) = _events(db, lost.taskId)
    assert event.type == EVENT_FAILED and event.payload["failure_kind"] == FAILURE_KIND_WORKER_LOST
    assert alive_row.status == TaskStatus.RUNNING and _events(db, alive.taskId) == []


# ----------------------------------------------------------------
# Job arguments
# ----------------------------------------------------------------
@pytest.mark.parametrize("status", [TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED])
def test_the_job_arguments_are_dropped_when_a_task_leaves_pending(db, mock_user, status):
    """The trigger: the worker reads them in its claim; they may hold uploaded files and addresses."""
    args = {"deployment_id": "x", "teams": {"A": [{"email": "a@example.org"}]}}
    _, task = _seed(db, mock_user, task_status=TaskStatus.PENDING, job="pods.deploy_application", job_kwargs=args)
    db.execute(text('UPDATE tasks SET progress_pct = 1 WHERE "taskId" = :id'), {"id": task.taskId})
    db.commit()
    db.expire_all()
    assert db.get(Task, task.taskId).job_kwargs == args  # still PENDING

    db.execute(text('UPDATE tasks SET status = :s WHERE "taskId" = :id'), {"s": status.name, "id": task.taskId})
    db.commit()
    db.expire_all()
    row = db.get(Task, task.taskId)
    assert row.job_kwargs is None and row.job == "pods.deploy_application"


# ----------------------------------------------------------------
# Sealed results
# ----------------------------------------------------------------
def test_task_api_never_returns_outputs_or_state(client, db, mock_user):
    """Sealed in the row, and not in the task API either (.github#7 E)."""
    state = json.dumps({"version": 4, "resources": []})
    _, task = _seed(db, mock_user, task_status=TaskStatus.SUCCESS,
                    outputs_enc=seal_results(crypto.results_cipher, terraform_outputs={"ip": {"value": "::1"}},
                                             tf_state=state))
    body = client.get(f"/tasks/{task.taskId}").json()
    assert "outputs" not in body and "tf_state" not in body
    assert body["status"] == "success"
    # Nothing in plaintext in the row.
    db.expire_all()
    row = db.get(Task, task.taskId)
    assert row.outputs is None and row.tf_state is None
