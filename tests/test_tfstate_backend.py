"""OpenTofu's HTTP state backend in the API (ported from dhbw-appstore)."""

from __future__ import annotations

import json

import pytest

from app.models import Deployment, Task, TaskStatus, TaskType, TerraformState
from app.services import task_finalizer, tf_state_store
from tests.conftest import TEST_PROJECT, create_app_in_db

pytestmark = pytest.mark.integration

_STATE = json.dumps({"version": 4, "serial": 1, "resources": [{"type": "random_password", "name": "pw"}]})


def _running_task(db, owner, token="tok-1", deployment=None) -> Task:
    if deployment is None:
        app = create_app_in_db(db, owner)
        deployment = Deployment(os_project_id=TEST_PROJECT, name="d", appId=app.appId, userId=owner.userId)
        db.add(deployment)
        db.flush()
    task = Task(
        deploymentId=deployment.deploymentId,
        type=TaskType.DEPLOY,
        status=TaskStatus.RUNNING,
        state_token_hash=tf_state_store.token_hash(token),
    )
    db.add(task)
    db.commit()
    return task


def _url(task: Task) -> str:
    return f"/internal/tfstate/{task.deploymentId}"


def _auth(task: Task, token="tok-1") -> tuple[str, str]:
    return (str(task.taskId), token)


def _lock_body(lock_id: str) -> str:
    return json.dumps({"ID": lock_id, "Operation": "OperationTypeApply", "Who": "w@x"})


def test_state_round_trip_is_encrypted_at_rest(unauth_client, db, mock_user):
    task = _running_task(db, mock_user)

    assert unauth_client.get(_url(task), auth=_auth(task)).status_code == 204
    assert unauth_client.post(_url(task), content=_STATE, auth=_auth(task)).status_code == 200

    response = unauth_client.get(_url(task), auth=_auth(task))
    assert response.status_code == 200
    assert response.text == _STATE
    db.expire_all()
    stored = db.get(TerraformState, task.deploymentId).state
    assert b"random_password" not in stored


@pytest.mark.parametrize(
    "tamper",
    ["no_auth", "wrong_token", "finished_task", "pending_task", "other_deployment", "not_a_uuid", "no_token_hash"],
)
def test_the_token_opens_only_its_own_running_task(unauth_client, db, mock_user, tamper):
    task = _running_task(db, mock_user)
    url, auth = _url(task), _auth(task)
    if tamper == "no_auth":
        auth = None
    elif tamper == "wrong_token":
        auth = (str(task.taskId), "guess")
    elif tamper == "finished_task":
        task.status = TaskStatus.SUCCESS
        db.commit()
    elif tamper == "pending_task":
        task.status = TaskStatus.PENDING
        db.commit()
    elif tamper == "other_deployment":
        other = _running_task(db, mock_user, token="tok-2")
        url = _url(other)
    elif tamper == "not_a_uuid":
        auth = ("x", "tok-1")
    elif tamper == "no_token_hash":
        task.state_token_hash = None
        db.commit()

    response = unauth_client.get(url, auth=auth)

    assert response.status_code == 401


def test_a_held_lock_blocks_other_lockers_and_writers(unauth_client, db, mock_user):
    task = _running_task(db, mock_user)
    auth = _auth(task)

    assert unauth_client.request("LOCK", _url(task), content=_lock_body("a"), auth=auth).status_code == 200

    competing = unauth_client.request("LOCK", _url(task), content=_lock_body("b"), auth=auth)
    assert competing.status_code == 423
    assert competing.json()["ID"] == "a"
    assert unauth_client.post(f"{_url(task)}?ID=b", content=_STATE, auth=auth).status_code == 423
    assert unauth_client.post(f"{_url(task)}?ID=a", content=_STATE, auth=auth).status_code == 200
    assert unauth_client.request("UNLOCK", _url(task), content=_lock_body("b"), auth=auth).status_code == 409

    assert unauth_client.request("UNLOCK", _url(task), content=_lock_body("a"), auth=auth).status_code == 200
    assert unauth_client.request("LOCK", _url(task), content=_lock_body("b"), auth=auth).status_code == 200


def test_the_lock_of_a_task_that_ended_is_taken_over(unauth_client, db, mock_user):
    first = _running_task(db, mock_user)
    assert (
        unauth_client.request("LOCK", _url(first), content=_lock_body("a"), auth=_auth(first)).status_code == 200
    )
    # The worker died mid-apply; the reaper failed the task. The next task of
    # the deployment must not be blocked by the lock it left behind.
    first.status = TaskStatus.FAILED
    db.commit()
    second = _running_task(db, mock_user, token="tok-2", deployment=db.get(Deployment, first.deploymentId))

    response = unauth_client.request("LOCK", _url(second), content=_lock_body("b"), auth=_auth(second, "tok-2"))

    assert response.status_code == 200


def test_garbage_and_oversized_bodies_are_refused(unauth_client, db, mock_user):
    task = _running_task(db, mock_user)

    assert unauth_client.post(_url(task), content="not json", auth=_auth(task)).status_code == 400
    assert unauth_client.request("LOCK", _url(task), content="{}", auth=_auth(task)).status_code == 400
    too_big = b"x" * (16 * 1024 * 1024 + 1)
    assert unauth_client.post(_url(task), content=too_big, auth=_auth(task)).status_code == 413
    assert db.get(TerraformState, task.deploymentId) is None or db.get(TerraformState, task.deploymentId).state is None


def test_state_endpoints_are_not_in_the_public_api(unauth_client):
    paths = unauth_client.get("/openapi.json").json()["paths"]
    assert not [p for p in paths if p.startswith("/internal")]


def test_a_successful_destroy_forgets_the_state(db, mock_user):
    task = _running_task(db, mock_user)
    tf_state_store.write(db, task.deploymentId, _STATE, None)
    task.type = TaskType.DESTROY
    task.status = TaskStatus.SUCCESS
    db.commit()

    assert task_finalizer.finalize_task(db, task.taskId) is True

    db.expire_all()
    assert db.get(TerraformState, task.deploymentId) is None
    assert db.get(Deployment, task.deploymentId).deleted_at is not None


def test_import_keeps_an_existing_state(db, mock_user):
    task = _running_task(db, mock_user)

    assert tf_state_store.import_state(db, task.deploymentId, _STATE) is True
    assert tf_state_store.import_state(db, task.deploymentId, json.dumps({"version": 4, "serial": 9})) is False
    assert tf_state_store.read(db, task.deploymentId) == _STATE


def test_import_moves_states_from_the_old_pg_backend(db, mock_user, capsys):
    import psycopg
    from psycopg import sql

    from app import import_pg_tfstate
    from app.pgq import libpq_url
    from tests.conftest import _TEST_DB_URL

    task = _running_task(db, mock_user)
    schema = import_pg_tfstate.schema_name(str(task.deploymentId))
    url = libpq_url(_TEST_DB_URL)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(
            sql.SQL("CREATE TABLE {}.states (id serial PRIMARY KEY, name text UNIQUE, data text)").format(
                sql.Identifier(schema)
            )
        )
        conn.execute(
            sql.SQL("INSERT INTO {}.states (name, data) VALUES ('default', %s)").format(sql.Identifier(schema)),
            (_STATE,),
        )
    try:
        assert import_pg_tfstate.main(["--tfstate-url", url, "--dry-run"]) == 0
        db.expire_all()
        assert tf_state_store.read(db, task.deploymentId) is None

        assert import_pg_tfstate.main(["--tfstate-url", url]) == 0
        db.expire_all()
        assert tf_state_store.read(db, task.deploymentId) == _STATE

        assert import_pg_tfstate.main(["--tfstate-url", url]) == 0
        assert "already has a state in the API, left alone" in capsys.readouterr().out
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
