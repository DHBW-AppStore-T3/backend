"""Pure-function tests for the deployment lifecycle matrix.

These tests don't touch the HTTP layer or the worker. They exercise
`lifecycle.allowed_actions` directly so a regression in the matrix
fails loudly without having to spin up the whole stack.
"""
import uuid
from datetime import datetime

import pytest

from app.models import App, Deployment, Task, TaskStatus, TaskType, User
from app.services import lifecycle as lifecycle_service
from app.services.lifecycle import DeploymentAction


def _seed_deployment_with_latest_task(
    db, user: User, *, task_type: TaskType, task_status: TaskStatus
) -> Deployment:
    """Create the minimum object graph needed for `get_deployment_status`.

    A deployment needs an App parent (FK), a single Task whose
    (type, status) drives the synthesized lifecycle status, and a row
    in the deployments table.
    """
    app = App(
        appId=uuid.uuid4(),
        name=f"app-{uuid.uuid4().hex[:8]}",
        userId=user.userId,
    )
    db.add(app)
    db.flush()

    deployment = Deployment(
        deploymentId=uuid.uuid4(),
        name=f"d-{uuid.uuid4().hex[:8]}",
        appId=app.appId,
        userId=user.userId,
    )
    db.add(deployment)
    db.flush()

    db.add(
        Task(
            taskId=uuid.uuid4(),
            deploymentId=deployment.deploymentId,
            type=task_type,
            status=task_status,
            created_at=datetime.utcnow(),
        )
    )
    db.commit()
    db.refresh(deployment)
    return deployment


@pytest.mark.parametrize(
    "task_type, task_status, expected",
    [
        # Deploy success → Pause and Destroy both available.
        (TaskType.DEPLOY, TaskStatus.SUCCESS,
         {DeploymentAction.PAUSE, DeploymentAction.DESTROY}),
        # Pause in flight → no actions allowed.
        (TaskType.PAUSE, TaskStatus.PENDING, set()),
        (TaskType.PAUSE, TaskStatus.RUNNING, set()),
        # Pause completed → Resume + Destroy.
        (TaskType.PAUSE, TaskStatus.SUCCESS,
         {DeploymentAction.RESUME, DeploymentAction.DESTROY}),
        # Pause failed → synthetic ``pause_failed`` state: deployment
        # is still up, retry pause, fall back to resume / destroy.
        (TaskType.PAUSE, TaskStatus.FAILED,
         {DeploymentAction.PAUSE, DeploymentAction.RESUME, DeploymentAction.DESTROY}),
        # Resume in flight → no actions.
        (TaskType.RESUME, TaskStatus.RUNNING, set()),
        # Resume success collapses to "success" — full action set returns.
        (TaskType.RESUME, TaskStatus.SUCCESS,
         {DeploymentAction.PAUSE, DeploymentAction.DESTROY}),
        # Resume failed → synthetic ``resume_failed`` state: SHUTOFF
        # instances, retry resume, also allow pause (idempotent) and
        # destroy.
        (TaskType.RESUME, TaskStatus.FAILED,
         {DeploymentAction.RESUME, DeploymentAction.PAUSE, DeploymentAction.DESTROY}),
        # Destroy still works as before.
        (TaskType.DESTROY, TaskStatus.PENDING, set()),
    ],
)
def test_lifecycle_matrix(db, mock_user, task_type, task_status, expected):
    deployment = _seed_deployment_with_latest_task(
        db, mock_user, task_type=task_type, task_status=task_status,
    )
    assert lifecycle_service.allowed_actions(db, deployment) == expected


def test_ensure_action_allowed_raises_with_required_states(db, mock_user):
    """The 409 should mention which statuses would allow the action."""
    from fastapi import HTTPException

    deployment = _seed_deployment_with_latest_task(
        db, mock_user, task_type=TaskType.DEPLOY, task_status=TaskStatus.SUCCESS,
    )
    # Deployment is in `success` — RESUME is invalid.
    with pytest.raises(HTTPException) as exc_info:
        lifecycle_service.ensure_action_allowed(
            db, deployment, DeploymentAction.RESUME,
        )
    assert exc_info.value.status_code == 409
    assert "paused" in str(exc_info.value.detail)


def test_resume_allowed_only_from_paused(db, mock_user):
    deployment = _seed_deployment_with_latest_task(
        db, mock_user, task_type=TaskType.PAUSE, task_status=TaskStatus.SUCCESS,
    )
    actions = lifecycle_service.allowed_actions(db, deployment)
    assert DeploymentAction.RESUME in actions
    # Pause shouldn't be re-offered while paused — there's nothing to pause.
    assert DeploymentAction.PAUSE not in actions


def test_in_flight_statuses_set_is_complete():
    """The IN_FLIGHT_STATUSES constant must list every synthetic
    in-flight state so the gating helpers (DELETE branch, resend-
    access guard) stay in sync with ``_ALLOWED``.
    """
    expected = {"pending", "running", "destroying", "pausing", "resuming"}
    assert expected == lifecycle_service.IN_FLIGHT_STATUSES
    # An in-flight status may offer CANCEL and nothing else. The point of
    # the guard is that no action runs *in parallel* with a live task;
    # cancel is the one action that revokes that very task rather than
    # racing it, which is why ``_ALLOWED`` lists it for pending/running.
    # Anything else appearing here would be a real bug.
    for s in expected:
        allowed = lifecycle_service._ALLOWED.get(s, set())
        assert allowed <= {DeploymentAction.CANCEL}, (
            f"In-flight status {s!r} must allow at most CANCEL, got {allowed}"
        )


def test_pause_failed_status_allows_retry_and_destroy(db, mock_user):
    deployment = _seed_deployment_with_latest_task(
        db, mock_user, task_type=TaskType.PAUSE, task_status=TaskStatus.FAILED,
    )
    actions = lifecycle_service.allowed_actions(db, deployment)
    # Synthetic ``pause_failed`` state — the deployment is still up,
    # so we offer pause-retry, resume (in case the user wants to
    # give up on pausing), and destroy. DELETE is NOT allowed —
    # the OpenStack resources are still alive.
    assert DeploymentAction.PAUSE in actions
    assert DeploymentAction.RESUME in actions
    assert DeploymentAction.DESTROY in actions
    assert DeploymentAction.DELETE not in actions


def test_resume_failed_status_allows_retry_and_destroy(db, mock_user):
    deployment = _seed_deployment_with_latest_task(
        db, mock_user, task_type=TaskType.RESUME, task_status=TaskStatus.FAILED,
    )
    actions = lifecycle_service.allowed_actions(db, deployment)
    assert DeploymentAction.RESUME in actions
    assert DeploymentAction.PAUSE in actions
    assert DeploymentAction.DESTROY in actions
    assert DeploymentAction.DELETE not in actions
