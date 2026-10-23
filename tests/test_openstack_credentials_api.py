"""OpenStack credentials per project, and project peers (dhbw-appstore's model).

Keystone is patched at ``openstack_validator.validate``; what is tested is
that only accepted credentials are stored, that their project comes from
Keystone and not from the request, that credentials from before (typed
project) count for nobody until re-checked, and who may do what with a
deployment in a project.
"""
from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.crud import openstack_credentials as crud_creds
from app.models import (
    App,
    Deployment,
    Task,
    TaskStatus,
    TaskType,
    User,
    UserOpenStackCredential,
    UserRole,
)
from app.services import openstack_validator, tf_state_store
from app.services.openstack_validator import CredentialRejected, Scope
from app.utils import crypto
from tests.conftest import TEST_PROJECT, _make_client, add_credential

pytestmark = pytest.mark.integration

VALIDATE = "app.routers.openstack_credentials.openstack_validator.validate"
SHA = "0123456789abcdef0123456789abcdef01234567"

_PASSWORD_PAYLOAD = {
    "auth_type": "password",
    "auth_url": "https://keystone.example/v3",
    "region_name": "RegionOne",
    "project_name": "demo",
    "user_domain_name": "Default",
    "project_domain_name": "Default",
    "identifier": "alice",
    "secret": "s3cret",
}

_APPCRED_PAYLOAD = {
    "auth_type": "v3applicationcredential",
    "auth_url": "https://keystone.example/v3",
    "region_name": "RegionOne",
    # Claims a project; only what Keystone says counts.
    "project_id": "claimed-project",
    "identifier": "appcred-id",
    "secret": "appcred-secret",
}

_CLOUDS_YAML = """\
clouds:
  mycloud:
    auth_type: v3applicationcredential
    auth:
      auth_url: https://keystone.example/v3
      application_credential_id: appcred-id
      application_credential_secret: appcred-secret
    region_name: RegionOne
"""


def _accepted(project_id=TEST_PROJECT, project_name="Projekt 1"):
    return patch(VALIDATE, return_value=Scope(project_id=project_id, project_name=project_name))


@pytest.fixture
def patched_celery():
    class _FakeAsyncResult:
        id = "fake-celery-task-id"

    with patch("app.services.task_service.celery_app.send_task", return_value=_FakeAsyncResult()) as m:
        yield m


# ----------------------------------------------------------------
# saving
# ----------------------------------------------------------------
def test_an_accepted_credential_is_stored_for_the_tokens_project(client, db, mock_user):
    with _accepted():
        response = client.post("/me/openstack-credentials", json=_APPCRED_PAYLOAD)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["project_id"] == TEST_PROJECT
    assert body["project_name"] == "Projekt 1"
    assert body["project_verified"] is True
    assert "secret" not in json.dumps(body) and "appcred-secret" not in json.dumps(body)
    [row] = crud_creds.list_for_user(db, mock_user.userId)
    assert crypto.decrypt(row.encrypted_secret) == "appcred-secret"
    assert row.last_validated_at is not None


def test_a_rejected_credential_is_not_stored(client, db, mock_user):
    with patch(VALIDATE, side_effect=CredentialRejected("invalid_credentials", 401)):
        response = client.post("/me/openstack-credentials", json=_PASSWORD_PAYLOAD)

    assert response.status_code == 422
    assert response.json()["detail"] == {"reason": "openstack_credentials_invalid", "cause": "invalid_credentials"}
    assert crud_creds.list_for_user(db, mock_user.userId) == []


def test_one_credential_per_project_saving_again_rotates_it(client, db, mock_user):
    with _accepted():
        first = client.post("/me/openstack-credentials", json=_APPCRED_PAYLOAD).json()
    with _accepted():
        second = client.post("/me/openstack-credentials", json={**_APPCRED_PAYLOAD, "secret": "rotated"})
    with _accepted("project-2", "Projekt 2"):
        other = client.post("/me/openstack-credentials", json=_PASSWORD_PAYLOAD)

    assert second.status_code == 200
    assert second.json()["credentialId"] == first["credentialId"]
    assert other.status_code == 201
    rows = {r.project_id: r for r in crud_creds.list_for_user(db, mock_user.userId)}
    assert set(rows) == {TEST_PROJECT, "project-2"}
    assert crypto.decrypt(rows[TEST_PROJECT].encrypted_secret) == "rotated"
    listed = client.get("/me/openstack-credentials").json()
    assert [c["project_name"] for c in listed] == ["Projekt 1", "Projekt 2"]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({**_PASSWORD_PAYLOAD, "user_domain_name": None}, "user_domain_name"),
        ({**_PASSWORD_PAYLOAD, "project_name": None}, "project_id or project_name"),
    ],
)
def test_password_credentials_need_domain_and_project(client, payload, message):
    with patch(VALIDATE) as validate:
        response = client.post("/me/openstack-credentials", json=payload)
    assert response.status_code == 422
    assert message in response.text
    validate.assert_not_called()


def test_from_yaml(client, db, mock_user):
    with _accepted():
        response = client.post(
            "/me/openstack-credentials/from-yaml", json={"clouds_yaml": _CLOUDS_YAML, "cloud_name": "mycloud"}
        )

    assert response.status_code == 201, response.text
    [row] = crud_creds.list_for_user(db, mock_user.userId)
    assert crypto.decrypt(row.encrypted_identifier) == "appcred-id"


def test_from_yaml_unknown_cloud_is_422_without_asking_keystone(client):
    with patch(VALIDATE) as validate:
        response = client.post(
            "/me/openstack-credentials/from-yaml", json={"clouds_yaml": _CLOUDS_YAML, "cloud_name": "nope"}
        )
    assert response.status_code == 422
    validate.assert_not_called()


# ----------------------------------------------------------------
# Keystone URL allowlist (.github#7 C)
# ----------------------------------------------------------------
@pytest.fixture
def production_allowlist(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "OPENSTACK_ALLOWED_AUTH_URLS", "https://keystone.example:5000/v3")


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://keystone.example:5000/v3", True),
        ("https://KEYSTONE.example:5000/v3/", True),
        ("https://keystone.example:5000", True),
        ("https://keystone.example/v3", False),  # other port
        ("http://keystone.example:5000/v3", False),
        ("https://keystone.example.evil:5000/v3", False),
        ("https://user:pw@keystone.example:5000/v3", False),
        ("https://169.254.169.254/v3", False),
        ("", False),
    ],
)
def test_only_allowlisted_keystone_urls(production_allowlist, url, allowed):
    assert openstack_validator.auth_url_allowed(url) is allowed


def test_production_without_an_allowlist_allows_nothing(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "OPENSTACK_ALLOWED_AUTH_URLS", "")
    assert openstack_validator.auth_url_allowed("https://keystone.example/v3") is False
    monkeypatch.setattr(settings, "APP_ENV", "development")
    assert openstack_validator.auth_url_allowed("https://keystone.example/v3") is True
    assert openstack_validator.auth_url_allowed("http://keystone.example/v3") is False


def test_a_credential_for_another_keystone_is_refused_before_anything_is_sent(client, db, mock_user, production_allowlist):
    with patch("app.services.openstack_validator.openstack.connect") as connect:
        response = client.post(
            "/me/openstack-credentials", json={**_APPCRED_PAYLOAD, "auth_url": "https://10.0.0.5:5000/v3"}
        )

    assert response.status_code == 422
    assert response.json()["detail"] == {"reason": "openstack_credentials_invalid", "cause": "auth_url_not_allowed"}
    connect.assert_not_called()
    assert crud_creds.list_for_user(db, mock_user.userId) == []


def test_a_stored_credential_outside_the_allowlist_is_not_used(client, db, mock_user, production_allowlist):
    row = add_credential(db, mock_user, auth_url="https://old-keystone.example/v3")

    with patch("app.services.openstack_client.openstack.connect") as connect:
        response = client.get(f"/me/openstack-credentials/{row.credentialId}/resources/networks")

    assert response.status_code == 422
    assert response.json()["detail"] == {"reason": "auth_url_not_allowed"}
    connect.assert_not_called()


# ----------------------------------------------------------------
# test / delete, and other people's credentials
# ----------------------------------------------------------------
def test_retest_records_the_outcome(client, db, mock_user):
    row = add_credential(db, mock_user)

    with patch(VALIDATE, side_effect=CredentialRejected("invalid_credentials", 401)):
        failed = client.post(f"/me/openstack-credentials/{row.credentialId}/test").json()
    with _accepted("somewhere-else"):
        moved = client.post(f"/me/openstack-credentials/{row.credentialId}/test").json()
    with _accepted():
        ok = client.post(f"/me/openstack-credentials/{row.credentialId}/test").json()

    assert failed["last_validation_error"] == "invalid_credentials"
    assert moved["last_validation_error"] == "project_changed"
    assert moved["project_id"] == TEST_PROJECT
    assert ok["last_validation_error"] is None and ok["last_validated_at"] is not None


def test_other_peoples_credentials_do_not_exist_for_the_caller(client, db, mock_admin):
    theirs = add_credential(db, mock_admin)

    with patch(VALIDATE) as validate:
        assert client.post(f"/me/openstack-credentials/{theirs.credentialId}/test").status_code == 404
    assert client.delete(f"/me/openstack-credentials/{theirs.credentialId}").status_code == 404
    assert client.get(f"/me/openstack-credentials/{theirs.credentialId}/quota").status_code == 404
    assert client.get(f"/me/openstack-credentials/{theirs.credentialId}/resources/networks").status_code == 404
    assert client.get("/me/openstack-credentials").json() == []
    validate.assert_not_called()
    assert db.get(UserOpenStackCredential, theirs.credentialId) is not None


def _live_deployment(db, owner, project_id=TEST_PROJECT) -> Deployment:
    app = App(name="a", userId=owner.userId, git_link="https://github.com/example/app")
    db.add(app)
    db.flush()
    dep = Deployment(
        name="d",
        appId=app.appId,
        userId=owner.userId,
        commit_sha=SHA,
        os_project_id=project_id,
        releaseTag="v1",
        userInputVar=json.dumps({"terraform": {}}),
    )
    db.add(dep)
    db.flush()
    db.add(Task(deploymentId=dep.deploymentId, type=TaskType.DEPLOY, status=TaskStatus.SUCCESS))
    db.commit()
    return dep


def test_delete_is_refused_while_own_deployments_live_in_the_project(client, db, mock_user):
    busy = add_credential(db, mock_user)
    idle = add_credential(db, mock_user, "project-2", identifier="other")
    _live_deployment(db, mock_user)

    locked = client.delete(f"/me/openstack-credentials/{busy.credentialId}")
    assert locked.status_code == 409
    assert locked.json()["detail"] == {"reason": "openstack_credentials_locked", "active_deployments": 1}
    assert client.delete(f"/me/openstack-credentials/{idle.credentialId}").status_code == 204
    listed = client.get("/me/openstack-credentials").json()
    assert [(c["project_id"], c["active_deployments"]) for c in listed] == [(TEST_PROJECT, 1)]


# ----------------------------------------------------------------
# credentials from before projects were verified
# ----------------------------------------------------------------
def test_an_old_credential_counts_for_nobodys_project(db, mock_user):
    # Typed someone else's project id into an application credential,
    # which never even used it.
    add_credential(db, mock_user, "victim-project", verified=False)

    assert crud_creds.holds_project(db, mock_user.userId, "victim-project") is False
    assert crud_creds.project_ids_of(db, mock_user.userId) == []


def test_rechecking_an_old_credential_verifies_it_and_places_old_deployments(client, db, mock_user):
    old = add_credential(db, mock_user, "typed-project", verified=False)
    placed_later = _live_deployment(db, mock_user, project_id=None)
    elsewhere = _live_deployment(db, mock_user, project_id="project-9")

    with _accepted("real-project", "Real"):
        body = client.post(f"/me/openstack-credentials/{old.credentialId}/test").json()

    assert body["project_verified"] is True
    assert (body["project_id"], body["project_name"]) == ("real-project", "Real")
    assert body["last_validation_error"] is None
    db.expire_all()
    assert db.get(Deployment, placed_later.deploymentId).os_project_id == "real-project"
    assert db.get(Deployment, elsewhere.deploymentId).os_project_id == "project-9"


def test_an_old_credential_whose_project_has_a_newer_one_stays_unverified(client, db, mock_user):
    add_credential(db, mock_user, "real-project")
    old = add_credential(db, mock_user, "typed-project", verified=False, identifier="old")
    dep = _live_deployment(db, mock_user, project_id=None)

    with _accepted("real-project"):
        body = client.post(f"/me/openstack-credentials/{old.credentialId}/test").json()

    assert body["project_verified"] is False
    assert body["last_validation_error"] == "project_has_credential"
    db.expire_all()
    assert db.get(Deployment, dep.deploymentId).os_project_id == "real-project"
    # Nothing needs it any more.
    assert client.delete(f"/me/openstack-credentials/{old.credentialId}").status_code == 204


def test_an_old_credential_cannot_be_deleted_while_it_is_the_only_way_to_place_deployments(client, db, mock_user):
    old = add_credential(db, mock_user, "typed-project", verified=False)
    _live_deployment(db, mock_user, project_id=None)

    response = client.delete(f"/me/openstack-credentials/{old.credentialId}")

    assert response.status_code == 409
    assert response.json()["detail"] == {"reason": "openstack_credentials_locked", "active_deployments": 1}


def test_an_old_deployment_waits_for_the_recheck(client, db, mock_user, patched_celery):
    add_credential(db, mock_user, "typed-project", verified=False)
    dep = _live_deployment(db, mock_user, project_id=None)

    response = client.post(f"/deployments/{dep.deploymentId}/pause")

    assert response.status_code == 409
    assert response.json()["detail"] == {"reason": "deployment_project_unknown"}
    patched_celery.assert_not_called()


# ----------------------------------------------------------------
# project peers
# ----------------------------------------------------------------
@pytest.fixture
def peer(db):
    user = User(
        keycloak_id="peer-keycloak-id",
        email="peer@dhbw.de",
        username="peer",
        firstName="Peer",
        lastName="Teacher",
        role=UserRole.TEACHER,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def peer_client(peer):
    from app.main import app

    yield _make_client(peer)
    app.dependency_overrides.clear()


def test_a_project_peer_sees_and_runs_the_deployment_with_their_own_credential(
    db, mock_user, peer, peer_client, patched_celery
):
    add_credential(db, mock_user, identifier="owner-cred")
    peer_credential = add_credential(db, peer, identifier="peer-cred")
    dep = _live_deployment(db, mock_user)

    assert [d["deploymentId"] for d in peer_client.get("/deployments/").json()] == [str(dep.deploymentId)]
    assert peer_client.get(f"/deployments/{dep.deploymentId}").status_code == 200
    response = peer_client.post(f"/deployments/{dep.deploymentId}/pause")

    assert response.status_code == 202, response.text
    kwargs = patched_celery.call_args.kwargs["kwargs"]
    envelope = kwargs["openstack_envelope"]
    assert crypto.decrypt(base64.b64decode(envelope["encrypted_identifier_b64"])) == "peer-cred"
    assert envelope["project_id"] == peer_credential.project_id


def test_a_credential_for_another_project_makes_no_peer(db, mock_user, peer, peer_client, patched_celery):
    add_credential(db, peer, "project-2", identifier="peer-cred")
    dep = _live_deployment(db, mock_user)

    assert peer_client.get("/deployments/").json() == []
    assert peer_client.get(f"/deployments/{dep.deploymentId}").status_code == 403
    assert peer_client.post(f"/deployments/{dep.deploymentId}/pause").status_code == 403
    patched_celery.assert_not_called()


def test_admins_are_read_only_on_vm_deployments(db, mock_user, admin_client, patched_celery):
    add_credential(db, mock_user)
    dep = _live_deployment(db, mock_user)

    assert admin_client.get(f"/deployments/{dep.deploymentId}").status_code == 200
    assert admin_client.post(f"/deployments/{dep.deploymentId}/pause").status_code == 403
    assert admin_client.delete(f"/deployments/{dep.deploymentId}").status_code == 403
    patched_celery.assert_not_called()


def test_the_owner_without_a_credential_for_the_project_cannot_act(client, db, mock_user, patched_celery):
    dep = _live_deployment(db, mock_user)

    response = client.post(f"/deployments/{dep.deploymentId}/pause")

    assert response.status_code == 403
    assert response.json()["detail"] == {
        "reason": "openstack_credentials_missing_for_project",
        "project_id": TEST_PROJECT,
    }


def test_every_job_gets_its_own_sealed_state_token(client, db, mock_user, patched_celery):
    add_credential(db, mock_user)
    dep = _live_deployment(db, mock_user)

    assert client.post(f"/deployments/{dep.deploymentId}/pause").status_code == 202

    kwargs = patched_celery.call_args.kwargs["kwargs"]
    token = crypto.decrypt_b64(kwargs["state_token_enc"])
    task = db.query(Task).filter(Task.deploymentId == dep.deploymentId, Task.type == TaskType.PAUSE).one()
    assert task.state_token_hash == tf_state_store.token_hash(token)
    assert token not in json.dumps(kwargs)


def test_lifecycle_with_a_credential_outside_the_allowlist_is_refused(
    client, db, mock_user, patched_celery, production_allowlist
):
    add_credential(db, mock_user, auth_url="https://old-keystone.example/v3")
    dep = _live_deployment(db, mock_user)

    response = client.post(f"/deployments/{dep.deploymentId}/pause")

    assert response.status_code == 422
    assert response.json()["detail"] == {"reason": "auth_url_not_allowed"}
    patched_celery.assert_not_called()


def _deploy(client, app, credential_id):
    return client.post(
        "/deployments/",
        json={
            "name": "d",
            "appId": str(app.appId),
            "releaseTag": "v1.0.0",
            "credentialId": str(credential_id) if credential_id else None,
            "teams": [],
        },
    )


def test_deploying_needs_one_of_ones_own_verified_credentials(client, db, mock_user, mock_admin, patched_celery):
    theirs = add_credential(db, mock_admin)
    old = add_credential(db, mock_user, "typed-project", verified=False)
    app = App(name="a", userId=mock_user.userId, git_link="https://github.com/example/app", is_private=True)
    db.add(app)
    db.commit()

    missing = _deploy(client, app, None)
    foreign = _deploy(client, app, theirs.credentialId)
    unverified = _deploy(client, app, old.credentialId)

    assert (missing.status_code, missing.json()["detail"]) == (412, {"reason": "openstack_credentials_missing"})
    assert foreign.status_code == 404
    assert (unverified.status_code, unverified.json()["detail"]) == (409, {"reason": "openstack_credentials_unverified"})
    assert db.query(Deployment).count() == 0
    patched_celery.assert_not_called()


def test_a_deployment_lives_in_the_project_of_its_credential(client, db, mock_user, patched_celery):
    credential = add_credential(db, mock_user, "project-7", identifier="mine")
    app = App(name="a", userId=mock_user.userId, git_link="https://github.com/example/app", is_private=True)
    db.add(app)
    db.commit()

    response = _deploy(client, app, credential.credentialId)

    assert response.status_code == 201, response.text
    assert response.json()["os_project_id"] == "project-7"
    envelope = patched_celery.call_args.kwargs["kwargs"]["openstack_envelope"]
    assert crypto.decrypt(base64.b64decode(envelope["encrypted_identifier_b64"])) == "mine"


# ----------------------------------------------------------------
# validator
# ----------------------------------------------------------------
def _upsert(**overrides):
    from app.schemas import OpenStackCredentialUpsert

    return OpenStackCredentialUpsert(**{**_APPCRED_PAYLOAD, **overrides})


def _connect_returning(access):
    from keystoneauth1.identity.base import BaseIdentityPlugin

    auth = MagicMock(spec=BaseIdentityPlugin)
    auth.get_access.return_value = access
    conn = MagicMock()
    conn.session.auth = auth
    return patch("app.services.openstack_validator.openstack.connect", return_value=conn)


def test_validator_takes_the_project_from_the_token():
    access = MagicMock(project_id="real-project", project_name="Real")
    with _connect_returning(access) as connect:
        scope = openstack_validator.validate(_upsert())

    assert scope == Scope(project_id="real-project", project_name="Real")
    kwargs = connect.call_args.kwargs
    assert kwargs["api_timeout"] == openstack_validator.TIMEOUT_SECONDS
    assert kwargs["application_credential_secret"] == "appcred-secret"
    assert "project_id" not in kwargs  # an application credential carries its own scope


def test_validator_refuses_unscoped_tokens():
    with _connect_returning(MagicMock(project_id=None)), pytest.raises(CredentialRejected) as exc:
        openstack_validator.validate(_upsert())
    assert exc.value.reason == "not_project_scoped"


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        ("unauthorized", "invalid_credentials"),
        ("not_found", "project_not_found"),
        ("connect", "unreachable"),
        ("other", "error"),
    ],
)
def test_validator_reasons_never_carry_the_error_text(error, reason):
    from keystoneauth1 import exceptions as ks_exc

    raised = {
        "unauthorized": ks_exc.Unauthorized("secret appcred-secret"),
        "not_found": ks_exc.NotFound("x"),
        "connect": ks_exc.ConnectFailure("down"),
        "other": RuntimeError("body: appcred-secret"),
    }[error]
    with patch("app.services.openstack_validator.openstack.connect", side_effect=raised), pytest.raises(
        CredentialRejected
    ) as exc:
        openstack_validator.validate(_upsert())
    assert exc.value.reason == reason
    assert "appcred-secret" not in str(exc.value)
