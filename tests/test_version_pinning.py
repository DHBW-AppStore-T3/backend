"""Approvals and deployments bound to the commit (.github#7 G).

What an admin approves is the commit a tag pointed to at submission, and a
deployment runs exactly the commit it was created with. A tag moved after
the review no longer counts as approved, and nothing is deployed without the
app's variable declarations (an unreadable repository fails the request).
"""
import json
import uuid
from datetime import datetime
from unittest.mock import patch

import pytest

from app.models import (
    AppVersionApproval,
    AppVersionApprovalStatus,
    Deployment,
    Task,
    TaskStatus,
    TaskType,
)
from tests.conftest import (
    TEST_PROJECT,
    FakeGitRemote,
    add_credential,
    approve_version_in_db,
    create_app_in_db,
)

pytestmark = pytest.mark.integration


class _FakeAsyncResult:
    id = "fake-celery-id"


@pytest.fixture
def sent():
    with patch("app.services.task_service.celery_app.send_task", return_value=_FakeAsyncResult()) as m:
        yield m


def _credential(db, user):
    return add_credential(db, user)


def _deploy(client, app, cred, tag="v1.0.0"):
    return client.post("/deployments/", json={
        "name": "pinned", "appId": str(app.appId), "releaseTag": tag, "teams": [],
        "credentialId": str(cred.credentialId),
    })


# ----------------------------------------------------------------
# Submit / approve
# ----------------------------------------------------------------
def test_submit_records_the_commit_the_tag_points_to(client, db, mock_user, fake_git):
    app = create_app_in_db(db, mock_user)

    resp = client.post(f"/apps/{app.appId}/versions/v1.0.0/submit", json={})

    assert resp.status_code == 201, resp.text
    assert resp.json()["commit_sha"] == FakeGitRemote.sha_for("v1.0.0")
    # The marker scan read exactly that commit.
    assert fake_git.clones[-1][2] == FakeGitRemote.sha_for("v1.0.0")


def test_approve_refuses_a_tag_moved_since_submission(client, admin_client, db, mock_user, fake_git):
    app = create_app_in_db(db, mock_user)
    assert client.post(f"/apps/{app.appId}/versions/v1.0.0/submit", json={}).status_code == 201
    fake_git.move("v1.0.0")

    resp = admin_client.post(f"/admin/apps/{app.appId}/versions/v1.0.0/approve")

    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "version_moved"
    db.expire_all()
    approval = db.query(AppVersionApproval).filter_by(appId=app.appId).one()
    assert approval.status == AppVersionApprovalStatus.PENDING


def test_approve_fails_closed_when_the_repository_is_unreadable(client, admin_client, db, mock_user, fake_git):
    app = create_app_in_db(db, mock_user)
    assert client.post(f"/apps/{app.appId}/versions/v1.0.0/submit", json={}).status_code == 201
    fake_git.readable = False

    resp = admin_client.post(f"/admin/apps/{app.appId}/versions/v1.0.0/approve")

    assert resp.status_code == 502
    assert resp.json()["detail"]["reason"] == "git_unreachable"


def test_student_sees_only_versions_still_at_the_approved_commit(student_client, db, mock_user, fake_git):
    app = create_app_in_db(db, mock_user)
    fake_git.tags = {"v1.0.0": FakeGitRemote.sha_for("v1.0.0"), "v2.0.0": FakeGitRemote.sha_for("v2.0.0")}
    approve_version_in_db(db, app, "v1.0.0")
    approve_version_in_db(db, app, "v2.0.0")
    fake_git.move("v2.0.0")

    resp = student_client.get(f"/apps/{app.appId}")

    assert resp.status_code == 200
    assert [v["version"] for v in resp.json()["versions"]] == ["v1.0.0"]


def test_registration_refuses_a_host_outside_the_allowlist(client):
    resp = client.post("/apps/", json={"name": "x", "git_link": "https://evil.example/acme/app"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "git_host_not_allowed"


def test_registration_stores_the_normalised_url(client):
    resp = client.post("/apps/", json={"name": "x", "git_link": "https://someone:tok@github.com/acme/app.git"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["git_link"] == "https://github.com/acme/app.git"


# ----------------------------------------------------------------
# Deploy
# ----------------------------------------------------------------
def test_deploy_of_an_approved_commit_pins_it(client, db, mock_user, mock_admin, sent, fake_git):
    app = create_app_in_db(db, mock_admin)
    approve_version_in_db(db, app, "v1.0.0")
    cred = _credential(db, mock_user)

    resp = _deploy(client, app, cred)

    assert resp.status_code == 201, resp.text
    assert resp.json()["commit_sha"] == FakeGitRemote.sha_for("v1.0.0")
    assert sent.call_args.kwargs["kwargs"]["commit_sha"] == FakeGitRemote.sha_for("v1.0.0")


def test_deploy_refuses_an_unapproved_version_of_a_public_app(client, db, mock_user, mock_admin, sent):
    app = create_app_in_db(db, mock_admin)
    approve_version_in_db(db, app, "v1.0.0")
    cred = _credential(db, mock_user)

    resp = _deploy(client, app, cred, tag="v2.0.0")

    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "version_not_approved"
    sent.assert_not_called()


def test_deploy_refuses_a_tag_moved_after_approval(client, db, mock_user, mock_admin, sent, fake_git):
    app = create_app_in_db(db, mock_admin)
    approve_version_in_db(db, app, "v1.0.0")
    fake_git.move("v1.0.0")
    cred = _credential(db, mock_user)

    resp = _deploy(client, app, cred)

    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "version_not_approved"
    assert db.query(Deployment).count() == 0


def test_deploy_needs_a_version(client, db, mock_user, sent):
    app = create_app_in_db(db, mock_user, is_private=True)
    resp = client.post("/deployments/", json={"name": "x", "appId": str(app.appId), "teams": []})
    assert resp.status_code == 422


def test_owner_deploys_any_tag_of_a_private_app_pinned(client, db, mock_user, sent, fake_git):
    app = create_app_in_db(db, mock_user, is_private=True)
    cred = _credential(db, mock_user)

    resp = _deploy(client, app, cred, tag="feature-x")

    assert resp.status_code == 201, resp.text
    assert resp.json()["commit_sha"] == FakeGitRemote.sha_for("feature-x")


def test_deploy_fails_closed_without_the_repository(client, db, mock_user, sent, fake_git):
    app = create_app_in_db(db, mock_user, is_private=True)
    cred = _credential(db, mock_user)
    fake_git.readable = False

    resp = _deploy(client, app, cred)

    assert resp.status_code == 502
    assert db.query(Deployment).count() == 0
    sent.assert_not_called()


def test_deploy_of_an_unknown_tag_is_404(client, db, mock_user, sent, fake_git):
    app = create_app_in_db(db, mock_user, is_private=True)
    cred = _credential(db, mock_user)
    fake_git.any_tag = False

    resp = _deploy(client, app, cred, tag="nope")

    assert resp.status_code == 404
    assert resp.json()["detail"]["reason"] == "version_not_found"


# ----------------------------------------------------------------
# Lifecycle jobs run the pinned commit
# ----------------------------------------------------------------
def _deployment(db, user, app, *, commit_sha):
    dep = Deployment(
        deploymentId=uuid.uuid4(), name="d", appId=app.appId, userId=user.userId,
        releaseTag="v1.0.0", commit_sha=commit_sha, os_project_id=TEST_PROJECT,
        userInputVar=json.dumps({"terraform": {}}),
    )
    db.add(dep)
    db.flush()
    db.add(Task(taskId=uuid.uuid4(), deploymentId=dep.deploymentId, type=TaskType.DEPLOY,
                status=TaskStatus.SUCCESS, created_at=datetime.utcnow()))
    db.commit()
    return dep


def test_lifecycle_jobs_carry_the_pinned_commit_even_if_the_tag_moved(client, db, mock_user, sent, fake_git):
    app = create_app_in_db(db, mock_user, is_private=True)
    _credential(db, mock_user)
    pinned = FakeGitRemote.sha_for("v1.0.0")
    dep = _deployment(db, mock_user, app, commit_sha=pinned)
    fake_git.move("v1.0.0")

    resp = client.post(f"/deployments/{dep.deploymentId}/pause")

    assert resp.status_code == 202, resp.text
    assert sent.call_args.kwargs["kwargs"]["commit_sha"] == pinned


def test_a_legacy_deployment_is_pinned_on_its_next_job(client, db, mock_user, sent, fake_git):
    app = create_app_in_db(db, mock_user, is_private=True)
    _credential(db, mock_user)
    dep = _deployment(db, mock_user, app, commit_sha=None)

    resp = client.post(f"/deployments/{dep.deploymentId}/pause")

    assert resp.status_code == 202, resp.text
    db.expire_all()
    stored = db.get(Deployment, dep.deploymentId)
    assert stored.commit_sha == FakeGitRemote.sha_for("v1.0.0")
    assert sent.call_args.kwargs["kwargs"]["commit_sha"] == stored.commit_sha
