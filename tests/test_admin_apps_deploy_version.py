"""Tests for the version an admin fixes for teachers to deploy.

Endpoint under test: ``PUT /admin/apps/{app_id}/deploy-version``
(admin_apps.set_deploy_version). Only an approved version whose tag still
points to the reviewed commit can be set; the field is cleared again when
that version stops being approved.
"""

import uuid

import pytest

from app.main import app as fastapi_app
from app.models import AppVersionApproval, AppVersionApprovalStatus
from app.utils.keycloak_auth import get_current_user_keycloak
from tests.conftest import FakeGitRemote, approve_version_in_db, create_app_in_db


def _as(user):
    """Swap the active ``get_current_user_keycloak`` override (see
    ``test_admin_apps_deactivate`` for why one client changes roles)."""
    fastapi_app.dependency_overrides[get_current_user_keycloak] = lambda: user


def _set(client, app_id, version_tag):
    return client.put(f"/admin/apps/{app_id}/deploy-version", json={"version_tag": version_tag})


def _deploy_version(client, app_id):
    response = client.get(f"/apps/{app_id}")
    assert response.status_code == 200
    return response.json()["deploy_version"]


@pytest.fixture
def app_with_v1(db, mock_user, fake_git):
    # The detail endpoint only reports a deploy version whose tag still exists.
    fake_git.tags.update({tag: FakeGitRemote.sha_for(tag) for tag in ("v1.0.0", "v2.0.0")})
    app = create_app_in_db(db, mock_user, name="Pinned")
    approve_version_in_db(db, app, "v1.0.0")
    return app


@pytest.mark.integration
def test_new_app_has_no_deploy_version(admin_client, app_with_v1):
    assert _deploy_version(admin_client, app_with_v1.appId) is None


@pytest.mark.integration
def test_admin_sets_an_approved_version(admin_client, app_with_v1):
    response = _set(admin_client, app_with_v1.appId, "v1.0.0")

    assert response.status_code == 200
    assert response.json()["deploy_version"] == "v1.0.0"
    assert _deploy_version(admin_client, app_with_v1.appId) == "v1.0.0"


@pytest.mark.integration
def test_setting_another_version_replaces_it(admin_client, db, app_with_v1):
    approve_version_in_db(db, app_with_v1, "v2.0.0")

    assert _set(admin_client, app_with_v1.appId, "v1.0.0").status_code == 200
    assert _set(admin_client, app_with_v1.appId, "v2.0.0").status_code == 200

    assert _deploy_version(admin_client, app_with_v1.appId) == "v2.0.0"


@pytest.mark.integration
def test_null_clears_the_deploy_version(admin_client, app_with_v1):
    assert _set(admin_client, app_with_v1.appId, "v1.0.0").status_code == 200

    response = _set(admin_client, app_with_v1.appId, None)

    assert response.status_code == 200
    assert response.json()["deploy_version"] is None
    assert _deploy_version(admin_client, app_with_v1.appId) is None
    # Clearing needs no approval and is harmless when nothing is set.
    assert _set(admin_client, app_with_v1.appId, None).status_code == 200


@pytest.mark.integration
def test_deploy_version_is_visible_to_students(admin_client, app_with_v1, mock_student):
    assert _set(admin_client, app_with_v1.appId, "v1.0.0").status_code == 200

    _as(mock_student)

    assert _deploy_version(admin_client, app_with_v1.appId) == "v1.0.0"
    listed = admin_client.get("/apps/").json()
    assert (
        next(a for a in listed if a["appId"] == str(app_with_v1.appId))["deploy_version"]
        == "v1.0.0"
    )


@pytest.mark.integration
def test_unknown_version_returns_404(admin_client, app_with_v1):
    response = _set(admin_client, app_with_v1.appId, "v9.9.9")

    assert response.status_code == 404
    assert _deploy_version(admin_client, app_with_v1.appId) is None


@pytest.mark.integration
@pytest.mark.parametrize(
    "status", [AppVersionApprovalStatus.PENDING, AppVersionApprovalStatus.REJECTED]
)
def test_version_that_is_not_approved_returns_409(admin_client, db, mock_user, status):
    app = create_app_in_db(db, mock_user, name="Not approved")
    approve_version_in_db(db, app, "v1.0.0", status=status)

    response = _set(admin_client, app.appId, "v1.0.0")

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "version_not_approved"
    assert _deploy_version(admin_client, app.appId) is None


@pytest.mark.integration
def test_approval_without_commit_returns_409(admin_client, db, mock_user):
    app = create_app_in_db(db, mock_user, name="Legacy approval")
    # From before versions were pinned to commits: approved, but no commit.
    db.add(
        AppVersionApproval(
            appId=app.appId, version_tag="v1.0.0", status=AppVersionApprovalStatus.APPROVED
        )
    )
    db.commit()

    response = _set(admin_client, app.appId, "v1.0.0")

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "version_not_approved"


@pytest.mark.integration
def test_moved_tag_returns_409(admin_client, app_with_v1, fake_git):
    fake_git.move("v1.0.0")

    response = _set(admin_client, app_with_v1.appId, "v1.0.0")

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "version_moved"
    assert _deploy_version(admin_client, app_with_v1.appId) is None


@pytest.mark.integration
def test_non_admins_get_403(admin_client, db, app_with_v1, mock_user, mock_student):
    for user in (mock_user, mock_student):
        _as(user)
        assert _set(admin_client, app_with_v1.appId, "v1.0.0").status_code == 403

    db.refresh(app_with_v1)
    assert app_with_v1.deploy_version is None


@pytest.mark.integration
def test_unknown_app_returns_404(admin_client):
    assert _set(admin_client, uuid.uuid4(), "v1.0.0").status_code == 404


@pytest.mark.integration
@pytest.mark.parametrize("body", [{}, {"version_tag": ""}])
def test_missing_or_empty_version_tag_returns_422(admin_client, app_with_v1, body):
    response = admin_client.put(f"/admin/apps/{app_with_v1.appId}/deploy-version", json=body)

    assert response.status_code == 422


@pytest.mark.integration
def test_owner_cannot_set_it_via_app_update(admin_client, app_with_v1, mock_user):
    _as(mock_user)

    response = admin_client.put(f"/apps/{app_with_v1.appId}", json={"deploy_version": "v1.0.0"})

    assert response.status_code == 200
    assert response.json()["deploy_version"] is None


@pytest.mark.integration
def test_revoking_the_deploy_version_clears_it(admin_client, db, app_with_v1):
    approve_version_in_db(db, app_with_v1, "v2.0.0")
    assert _set(admin_client, app_with_v1.appId, "v1.0.0").status_code == 200

    # Revoking another version leaves the setting alone ...
    other = admin_client.post(
        f"/admin/apps/{app_with_v1.appId}/versions/v2.0.0/revoke", json={"rejection_reason": "x"}
    )
    assert other.status_code == 200
    assert _deploy_version(admin_client, app_with_v1.appId) == "v1.0.0"

    # ... revoking the set one clears it.
    revoked = admin_client.post(
        f"/admin/apps/{app_with_v1.appId}/versions/v1.0.0/revoke", json={"rejection_reason": "x"}
    )
    assert revoked.status_code == 200
    assert _deploy_version(admin_client, app_with_v1.appId) is None


@pytest.mark.integration
def test_resubmitting_a_moved_tag_clears_it(admin_client, app_with_v1, fake_git):
    assert _set(admin_client, app_with_v1.appId, "v1.0.0").status_code == 200
    fake_git.move("v1.0.0")

    # The new submission replaces the approved entry with a pending one for
    # the new commit: the setting must not keep pointing at it.
    resubmitted = admin_client.post(f"/apps/{app_with_v1.appId}/versions/v1.0.0/submit", json={})
    assert resubmitted.status_code == 201
    assert resubmitted.json()["status"] == "pending"

    assert _deploy_version(admin_client, app_with_v1.appId) is None


@pytest.mark.integration
def test_detail_hides_a_deploy_version_whose_tag_moved(
    admin_client, app_with_v1, mock_student, fake_git
):
    assert _set(admin_client, app_with_v1.appId, "v1.0.0").status_code == 200
    fake_git.move("v1.0.0")

    # Teachers can no longer deploy it (the approval covers the old commit):
    # the detail does not offer it, neither to the admin nor to a student.
    assert _deploy_version(admin_client, app_with_v1.appId) is None
    _as(mock_student)
    assert _deploy_version(admin_client, app_with_v1.appId) is None


@pytest.mark.integration
def test_detail_hides_a_deploy_version_whose_tag_is_gone(admin_client, app_with_v1, fake_git):
    assert _set(admin_client, app_with_v1.appId, "v1.0.0").status_code == 200
    del fake_git.tags["v1.0.0"]

    assert _deploy_version(admin_client, app_with_v1.appId) is None


@pytest.mark.integration
def test_detail_keeps_the_deploy_version_when_the_repository_is_unreadable(
    admin_client, app_with_v1, fake_git
):
    assert _set(admin_client, app_with_v1.appId, "v1.0.0").status_code == 200
    fake_git.readable = False

    # Nothing can be checked then; the stored setting is not hidden because of an outage.
    assert _deploy_version(admin_client, app_with_v1.appId) == "v1.0.0"


@pytest.mark.integration
def test_detail_does_not_write_the_hidden_value(admin_client, db, app_with_v1, fake_git):
    assert _set(admin_client, app_with_v1.appId, "v1.0.0").status_code == 200
    fake_git.move("v1.0.0")
    assert _deploy_version(admin_client, app_with_v1.appId) is None

    # Hiding is on the way out only: a read never changes the stored setting.
    # (The next review of the version clears it.)
    db.refresh(app_with_v1)
    assert app_with_v1.deploy_version == "v1.0.0"
