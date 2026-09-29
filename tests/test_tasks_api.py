"""Tests for the Tasks-API router — ``app.routers.tasks``.

Phase C10: read-only endpoints exposing task information for a deployment.
Covered cases:

  * Owner darf die Task-Liste einer eigenen Deployment lesen.
  * Nicht-Mitglied (STUDENT, kein Team, kein UserToDeployment) bekommt 403.
  * Fremder Lehrender ohne Kursbezug zum Owner bekommt 403.
  * Owner darf eine einzelne Task per ID lesen.
  * Unbekannte Task-ID → 404.
  * Ohne Bearer-Token → 401 (oder 403 von HTTPBearer).
"""

import uuid
from datetime import datetime

import pytest

from app.models import (
    App,
    Deployment,
    Task,
    TaskStatus,
    TaskType,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_app(db, user_id):
    a = App(
        appId=uuid.uuid4(),
        name=f"App {uuid.uuid4().hex[:6]}",
        userId=user_id,
        git_link="https://example.com/repo.git",
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


def _make_deployment(db, user_id, app_id, name="dep-tasks"):
    d = Deployment(
        deploymentId=uuid.uuid4(),
        name=name,
        userId=user_id,
        appId=app_id,
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


def _make_task(
    db,
    deployment_id,
    *,
    type_=TaskType.DEPLOY,
    status=TaskStatus.SUCCESS,
    celery_task_id="celery-task-1",
    created_at=None,
):
    t = Task(
        taskId=uuid.uuid4(),
        deploymentId=deployment_id,
        celeryTaskId=celery_task_id,
        type=type_,
        status=status,
        created_at=created_at or datetime(2026, 1, 1, 12, 0, 0),
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


# ---------------------------------------------------------------------------
# GET /tasks/deployment/{deployment_id}
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_get_deployment_tasks_owner_ok(client, db, mock_user):
    """Owner einer Deployment liest die zugehörigen Tasks."""
    app = _make_app(db, mock_user.userId)
    deployment = _make_deployment(db, mock_user.userId, app.appId)
    task = _make_task(db, deployment.deploymentId)

    response = client.get(f"/tasks/deployment/{deployment.deploymentId}")

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["taskId"] == str(task.taskId)
    assert body[0]["deploymentId"] == str(deployment.deploymentId)


@pytest.mark.integration
def test_get_deployment_tasks_non_member_403(
    student_client, db, mock_admin, mock_student
):
    """Ein STUDENT ohne Team-/Direkt-Zuordnung bekommt 403."""
    # Deployment gehört dem Admin — der Student hat keinerlei Bezug.
    app = _make_app(db, mock_admin.userId)
    deployment = _make_deployment(db, mock_admin.userId, app.appId)
    _make_task(db, deployment.deploymentId)

    response = student_client.get(f"/tasks/deployment/{deployment.deploymentId}")

    assert response.status_code == 403


@pytest.mark.integration
def test_get_deployment_tasks_unrelated_teacher_403(client, db, mock_admin):
    """Ein Lehrender ohne Kursbezug zum Owner bekommt 403.

    Der Router benutzte früher ``permissions.ensure_deployment_owner_view``,
    das jede TEACHER-Rolle pauschal durchliess — damit konnte ein
    beliebiger Lehrender die Task-Logs jeder Deployment lesen, inklusive
    Terraform-Outputs, IPs und Worker-Stacktraces. Jetzt gilt
    ``capabilities.ensure_view_deployment_owner``: Owner, Admin, oder der
    Kurs-Lehrende des Deployment-Owners. ``mock_user`` (TEACHER) ist hier
    keins davon, und ``mock_admin`` hat keinen Kurs.
    """
    app = _make_app(db, mock_admin.userId)
    deployment = _make_deployment(db, mock_admin.userId, app.appId)
    _make_task(db, deployment.deploymentId)

    response = client.get(f"/tasks/deployment/{deployment.deploymentId}")

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "deployment_owner_view_forbidden"


# ---------------------------------------------------------------------------
# GET /tasks/{task_id}
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_get_task_by_id_owner_ok(client, db, mock_user):
    """Owner liest eine einzelne Task per ID."""
    app = _make_app(db, mock_user.userId)
    deployment = _make_deployment(db, mock_user.userId, app.appId)
    task = _make_task(
        db,
        deployment.deploymentId,
        celery_task_id="celery-by-id",
    )

    response = client.get(f"/tasks/{task.taskId}")

    assert response.status_code == 200
    body = response.json()
    assert body["taskId"] == str(task.taskId)
    assert body["deploymentId"] == str(deployment.deploymentId)
    assert body["celeryTaskId"] == "celery-by-id"


@pytest.mark.integration
def test_get_task_404_for_unknown_id(client):
    """Unbekannte Task-ID liefert 404."""
    response = client.get(f"/tasks/{uuid.uuid4()}")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_get_task_unauthenticated_401(unauth_client):
    """Ohne Token verweigert die API den Zugriff auf Tasks.

    FastAPI's ``HTTPBearer`` mappt fehlende Credentials auf 403, mit
    ungültigem Token kommt 401 — beides sind valide „nicht erlaubt"-
    Antworten und werden hier akzeptiert.
    """
    response = unauth_client.get(f"/tasks/{uuid.uuid4()}")
    assert response.status_code in (401, 403)
