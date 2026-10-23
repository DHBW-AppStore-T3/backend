"""Secrets out of the task API, the owner view and old rows (.github#7 E, security review H-8)."""
import json
import uuid
from datetime import datetime

import pytest

from app import seal_legacy_results
from app.models import App, Deployment, Task, TaskEvent, TaskStatus, TaskType
from app.services import task_results
from app.task_contract import open_results
from app.utils import crypto

OUTPUTS = {
    "team_vms": {"value": {"Team-1": {"url": "https://vm"}}, "sensitive": False},
    "user_accounts": {"value": {"Team-1-a": {"auth": "pw-SECRET"}}, "sensitive": True},
    "db_password": {"value": "db-SECRET", "sensitive": True},
    # An app that forgot ``sensitive``: user_accounts is blanked anyway.
}


def _deployment_with_task(db, owner, **task_fields):
    app = App(appId=uuid.uuid4(), name="a", userId=owner.userId, git_link="https://example.com/acme/a")
    db.add(app)
    db.flush()
    dep = Deployment(deploymentId=uuid.uuid4(), name="d", appId=app.appId, userId=owner.userId, releaseTag="v1")
    db.add(dep)
    db.flush()
    task = Task(taskId=uuid.uuid4(), deploymentId=dep.deploymentId, type=TaskType.DEPLOY,
                status=TaskStatus.SUCCESS, created_at=datetime.utcnow(), **task_fields)
    db.add(task)
    db.commit()
    return dep, task


@pytest.mark.unit
def test_redaction_blanks_sensitive_outputs_and_accounts():
    unmarked = {"user_accounts": {"value": {"k": {"auth": "pw"}}}, "ip": {"value": "::1"}}
    out = task_results.redact_sensitive_outputs({**OUTPUTS, **{"x": "raw"}})
    assert out["team_vms"] == OUTPUTS["team_vms"]
    assert out["user_accounts"] == {"value": None, "sensitive": True, "redacted": True}
    assert out["db_password"]["value"] is None and out["db_password"]["redacted"]
    assert task_results.redact_sensitive_outputs(unmarked)["user_accounts"]["value"] is None
    assert task_results.redact_sensitive_outputs(unmarked)["ip"] == {"value": "::1"}
    assert task_results.redact_sensitive_outputs(None) is None


@pytest.mark.integration
def test_owner_view_shows_no_credentials(client, db, mock_user):
    from app.task_contract import seal_results

    dep, _ = _deployment_with_task(
        db, mock_user, outputs_enc=seal_results(crypto.results_cipher, terraform_outputs=OUTPUTS, tf_state=None)
    )
    body = client.get(f"/deployments/{dep.deploymentId}").json()
    raw = json.dumps(body)
    assert "pw-SECRET" not in raw and "db-SECRET" not in raw
    assert body["outputs"]["raw"]["team_vms"]["value"] == {"Team-1": {"url": "https://vm"}}
    assert body["outputs"]["raw"]["user_accounts"]["redacted"] is True


@pytest.mark.integration
def test_legacy_rows_are_sealed_and_scrubbed(db, mock_user, capsys):
    leaky = {"timestamp": "t", "level": "INFO", "category": "system", "message": "Terraform deployment output",
             "user_accounts": {"value": {"k": {"auth": "pw-SECRET"}}}, "ip": {"value": "::1"}}
    plain = {"timestamp": "t", "level": "INFO", "category": "phase", "message": "=== STARTING ==="}
    state = json.dumps({"version": 4, "resources": []})
    _, task = _deployment_with_task(
        db, mock_user, outputs=json.dumps(OUTPUTS), tf_state=state, logs=json.dumps([plain, leaky]),
    )
    db.add(TaskEvent(task_id=task.taskId, type="task-log", payload={**leaky, "deployment_id": "d"}))
    db.add(TaskEvent(task_id=task.taskId, type="task-log", payload=plain))
    db.commit()

    assert seal_legacy_results.main(["--dry-run"]) == 0
    db.expire_all()
    assert db.get(Task, task.taskId).outputs is not None  # dry run wrote nothing

    assert seal_legacy_results.main([]) == 0
    db.expire_all()
    row = db.get(Task, task.taskId)
    assert row.outputs is None and row.tf_state is None
    assert open_results(crypto.results_cipher, row.outputs_enc) == {"terraform_outputs": OUTPUTS, "tf_state": state}
    logs = json.loads(row.logs)
    assert logs[0] == plain
    assert logs[1]["output_names"] == ["ip", "user_accounts"]
    assert "pw-SECRET" not in row.logs
    payloads = [e.payload for e in db.query(TaskEvent).filter_by(task_id=task.taskId).order_by(TaskEvent.id)]
    assert "pw-SECRET" not in json.dumps(payloads)
    assert payloads[0]["output_names"] == ["ip", "user_accounts"] and payloads[0]["deployment_id"] == "d"
    assert payloads[1] == plain
    assert "1 task result(s) were sealed" in capsys.readouterr().out


@pytest.mark.unit
def test_non_json_logs_are_left_alone():
    assert seal_legacy_results.scrub_logs("Failed to dispatch") == ("Failed to dispatch", False)
    assert seal_legacy_results.scrub_logs(None) == (None, False)
