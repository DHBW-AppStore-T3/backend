"""Tests for the admin recommendation of apps.

Endpoint under test: ``PUT /admin/apps/{app_id}/recommended``
(admin_apps.set_app_recommended). The flag highlights an app in the
catalog; only an admin sets it, everyone reads it from the app data.
"""

import uuid

import pytest

from app.main import app as fastapi_app
from app.utils.keycloak_auth import get_current_user_keycloak
from tests.conftest import approve_version_in_db, create_app_in_db


def _as(user):
    """Swap the active ``get_current_user_keycloak`` override (see
    ``test_admin_apps_deactivate`` for why one client changes roles)."""
    fastapi_app.dependency_overrides[get_current_user_keycloak] = lambda: user


def _app_entry(client, app_id):
    listed = client.get("/apps/")
    assert listed.status_code == 200
    return next(a for a in listed.json() if a["appId"] == str(app_id))


@pytest.mark.integration
def test_new_app_is_not_recommended(admin_client, db, mock_user):
    app = create_app_in_db(db, mock_user, name="Plain")

    assert _app_entry(admin_client, app.appId)["recommended"] is False


@pytest.mark.integration
def test_admin_can_recommend_and_unrecommend(admin_client, db, mock_user):
    app = create_app_in_db(db, mock_user, name="Pick me")
    url = f"/admin/apps/{app.appId}/recommended"

    on = admin_client.put(url, json={"recommended": True})
    assert on.status_code == 200
    assert on.json()["recommended"] is True
    assert admin_client.get(f"/apps/{app.appId}").json()["recommended"] is True

    off = admin_client.put(url, json={"recommended": False})
    assert off.status_code == 200
    assert off.json()["recommended"] is False
    assert admin_client.get(f"/apps/{app.appId}").json()["recommended"] is False


@pytest.mark.integration
def test_recommended_is_visible_to_students(admin_client, db, mock_user, mock_student):
    app = create_app_in_db(db, mock_user, name="Public", is_private=False)
    approve_version_in_db(db, app)
    assert (
        admin_client.put(
            f"/admin/apps/{app.appId}/recommended", json={"recommended": True}
        ).status_code
        == 200
    )

    _as(mock_student)

    assert _app_entry(admin_client, app.appId)["recommended"] is True


@pytest.mark.integration
def test_teacher_and_student_cannot_set_recommended(admin_client, db, mock_user, mock_student):
    app = create_app_in_db(db, mock_user, name="Not yours to pick")
    url = f"/admin/apps/{app.appId}/recommended"

    for user in (mock_user, mock_student):
        _as(user)
        assert admin_client.put(url, json={"recommended": True}).status_code == 403

    db.expire_all()
    assert app.recommended is False


@pytest.mark.integration
def test_owner_cannot_recommend_via_app_update(admin_client, db, mock_user):
    app = create_app_in_db(db, mock_user, name="Mine")
    _as(mock_user)

    response = admin_client.put(f"/apps/{app.appId}", json={"recommended": True})

    assert response.status_code == 200
    assert response.json()["recommended"] is False
    db.expire_all()
    assert app.recommended is False


@pytest.mark.integration
def test_unknown_app_returns_404(admin_client):
    response = admin_client.put(
        f"/admin/apps/{uuid.uuid4()}/recommended", json={"recommended": True}
    )

    assert response.status_code == 404


@pytest.mark.integration
def test_missing_body_field_returns_422(admin_client, db, mock_user):
    app = create_app_in_db(db, mock_user, name="Empty body")

    response = admin_client.put(f"/admin/apps/{app.appId}/recommended", json={})

    assert response.status_code == 422
