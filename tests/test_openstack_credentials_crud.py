"""``app.crud.openstack_credentials``: one credential per user and project.

Stored only with the project Keystone named (``project_verified``);
credentials from before carry a typed project and count for nobody until a
re-check names theirs. Secrets are encrypted at rest and leave the table
either as ciphertext (job envelope) or as plaintext for the backend's own
calls, never anything else.
"""
from __future__ import annotations

import base64
import json

import pytest
from sqlalchemy.exc import IntegrityError

from app.crud import openstack_credentials as crud_creds
from app.models import (
    App,
    Deployment,
    OpenStackAuthType,
    Task,
    TaskStatus,
    TaskType,
    UserOpenStackCredential,
)
from app.schemas import OpenStackCredentialUpsert
from app.utils import crypto
from tests.conftest import TEST_PROJECT, add_credential

pytestmark = pytest.mark.integration


def _payload(**overrides) -> OpenStackCredentialUpsert:
    base = {
        "auth_type": OpenStackAuthType.PASSWORD,
        "auth_url": "https://keystone.example/v3",
        "region_name": "RegionOne",
        "project_name": "typed-name",
        "user_domain_name": "Default",
        "identifier": "alice",
        "secret": "hunter2",
    }
    base.update(overrides)
    return OpenStackCredentialUpsert(**base)


def _deployment(db, owner, project_id):
    app = App(name="a", userId=owner.userId, git_link="https://github.com/example/app")
    db.add(app)
    db.flush()
    dep = Deployment(name="d", appId=app.appId, userId=owner.userId, os_project_id=project_id)
    db.add(dep)
    db.flush()
    db.add(Task(deploymentId=dep.deploymentId, type=TaskType.DEPLOY, status=TaskStatus.SUCCESS))
    db.commit()
    return dep


def test_save_encrypts_and_takes_the_project_from_keystone(db, mock_user):
    row, created = crud_creds.save(db, mock_user.userId, _payload(), "kp-1", "Keystone Name")

    assert created is True
    assert (row.project_id, row.project_name, row.project_verified) == ("kp-1", "Keystone Name", True)
    assert b"hunter2" not in row.encrypted_secret and b"alice" not in row.encrypted_identifier
    assert crypto.decrypt(row.encrypted_secret) == "hunter2"
    assert row.last_validated_at is not None and row.last_validation_error is None


def test_save_for_the_same_project_replaces(db, mock_user):
    first, _ = crud_creds.save(db, mock_user.userId, _payload(), "kp-1", None)
    second, created = crud_creds.save(db, mock_user.userId, _payload(secret="rotated"), "kp-1", None)
    other, other_created = crud_creds.save(db, mock_user.userId, _payload(), "kp-2", None)

    assert created is False and second.credentialId == first.credentialId
    assert crypto.decrypt(second.encrypted_secret) == "rotated"
    assert other_created is True
    assert {r.project_id for r in crud_creds.list_for_user(db, mock_user.userId)} == {"kp-1", "kp-2"}


def test_verified_projects_are_unique_per_user_unverified_ones_are_not(db, mock_user):
    add_credential(db, mock_user, "kp-1", verified=False)
    add_credential(db, mock_user, "kp-1")  # an old row typed the same id: allowed

    with pytest.raises(IntegrityError):
        add_credential(db, mock_user, "kp-1")
    db.rollback()


def test_lookups_never_cross_users_and_ignore_unverified_rows(db, mock_user, mock_admin):
    mine = add_credential(db, mock_user)
    theirs = add_credential(db, mock_admin)
    add_credential(db, mock_user, "typed", verified=False)

    assert crud_creds.get_owned(db, mock_user.userId, mine.credentialId).credentialId == mine.credentialId
    assert crud_creds.get_owned(db, mock_user.userId, theirs.credentialId) is None
    assert crud_creds.get_for_project(db, mock_user.userId, TEST_PROJECT).credentialId == mine.credentialId
    assert crud_creds.get_for_project(db, mock_user.userId, "typed") is None
    assert crud_creds.get_for_project(db, mock_user.userId, None) is None
    assert crud_creds.project_ids_of(db, mock_user.userId) == [TEST_PROJECT]


def test_stamp_validation_failure_keeps_the_last_success(db, mock_user):
    row = add_credential(db, mock_user)
    row = crud_creds.stamp_validation(db, row, None)
    stamped = row.last_validated_at

    row = crud_creds.stamp_validation(db, row, "invalid_credentials")

    assert row.last_validation_error == "invalid_credentials"
    assert row.last_validated_at == stamped


def test_a_recheck_never_moves_a_verified_credential(db, mock_user):
    row = add_credential(db, mock_user)

    row = crud_creds.stamp_validation(db, row, None, ("elsewhere", "Else"))

    assert (row.project_id, row.project_verified) == (TEST_PROJECT, True)


def test_a_recheck_of_an_old_credential_places_only_its_owners_unplaced_deployments(db, mock_user, mock_admin):
    old = add_credential(db, mock_user, "typed", verified=False)
    unplaced = _deployment(db, mock_user, None)
    placed = _deployment(db, mock_user, "kp-9")
    someone_elses = _deployment(db, mock_admin, None)

    row = crud_creds.stamp_validation(db, old, None, ("kp-real", "Real"))

    assert (row.project_id, row.project_name, row.project_verified) == ("kp-real", "Real", True)
    db.expire_all()
    assert db.get(Deployment, unplaced.deploymentId).os_project_id == "kp-real"
    assert db.get(Deployment, placed.deploymentId).os_project_id == "kp-9"
    assert db.get(Deployment, someone_elses.deploymentId).os_project_id is None


def test_a_failed_recheck_places_nothing(db, mock_user):
    old = add_credential(db, mock_user, "typed", verified=False)
    unplaced = _deployment(db, mock_user, None)

    row = crud_creds.stamp_validation(db, old, "invalid_credentials", None)

    assert row.project_verified is False
    db.expire_all()
    assert db.get(Deployment, unplaced.deploymentId).os_project_id is None


def test_dispatch_envelope_is_ciphertext_only(db, mock_user):
    row = add_credential(db, mock_user, identifier="the-id")

    envelope = crud_creds.dispatch_envelope(row)

    assert "app-cred-secret" not in json.dumps(envelope) and "the-id" not in json.dumps(envelope)
    assert crypto.decrypt(base64.b64decode(envelope["encrypted_identifier_b64"])) == "the-id"
    assert crypto.decrypt(base64.b64decode(envelope["encrypted_secret_b64"])) == "app-cred-secret"
    assert envelope["auth_type"] == "v3applicationcredential"
    assert envelope["project_id"] == TEST_PROJECT


def test_decrypted_is_plaintext_for_the_backend_only(db, mock_user):
    row = add_credential(db, mock_user, identifier="the-id")

    plain = crud_creds.decrypted(row)

    assert (plain["identifier"], plain["secret"]) == ("the-id", "app-cred-secret")
    assert plain["auth_url"] == "https://keystone.example/v3"


def test_delete(db, mock_user):
    row = add_credential(db, mock_user)

    crud_creds.delete(db, row)

    assert db.query(UserOpenStackCredential).count() == 0
