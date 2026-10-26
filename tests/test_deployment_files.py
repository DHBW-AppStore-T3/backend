"""Integration tests for the deployment file-upload contract.

Covers the round-trip from POST /deployments (with ``files`` field)
to GET detail (file bytes stripped) to GET /files/... (full payload
returned to owner). Celery is patched out.
"""
import base64
import json
import uuid
from unittest.mock import patch

import pytest

from app.models import (
    App,
    Deployment,
    User,
    UserRole,
)
from tests.conftest import spec_with


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


@pytest.fixture(autouse=True)
def _app_declares_the_file_variable(fake_git):
    fake_git.files["appstore.yaml"] = spec_with(variables=[{"name": "task_pdf", "type": "file", "mountPath": "/data/task.pdf"}])


def _teams(user):
    return [{"name": "T", "userIds": [str(user.userId)]}]


def _ensure_app(db, user) -> App:
    app = App(
        appId=uuid.uuid4(),
        name=f"app-{uuid.uuid4().hex[:8]}",
        userId=user.userId,
        git_link="https://example.com/acme/repo.git",
        # The owner's own private app: deployable without an approval.
        is_private=True,
    )
    db.add(app)
    db.commit()
    db.refresh(app)
    return app


@pytest.fixture
def patched_celery():
    class _FakeAsyncResult:
        id = "fake-celery-task-id"

    with patch(
        "app.services.task_service.celery_app.send_task",
        return_value=_FakeAsyncResult(),
    ) as m:
        yield m


@pytest.mark.integration
def test_post_deployment_persists_files_under_terraform(
    client, db, mock_user, patched_celery
):
    app = _ensure_app(db, mock_user)

    pdf = b"%PDF-1.4\n%fake pdf bytes for the test\n"
    payload = {
        "name": "with-files",
        "appId": str(app.appId),
        "releaseTag": "v1",
        "userInputVar": {"terraform": {"some_other": "x"}},
        "files": {
            "task_pdf": {
                "all": {
                    "name": "aufgabe.pdf",
                    "content_b64": _b64(pdf),
                    "size": len(pdf),
                    "content_type": "application/pdf",
                }
            }
        },
        "teams": _teams(mock_user),
    }
    response = client.post("/deployments/", json=payload)
    assert response.status_code == 201, response.text

    body = response.json()
    # The list/detail responses strip file bytes — but metadata
    # survives so the UI can render "uploaded: aufgabe.pdf".
    file_var = body["userInputVar"]["terraform"]["task_pdf"]
    upload = file_var["all"]
    assert upload["name"] == "aufgabe.pdf"
    assert upload["size"] == len(pdf)
    assert upload["content_type"] == "application/pdf"
    assert "content_b64" not in upload  # stripped from response

    # The DB row carries the full base64 — the strip is a response-
    # shaping concern, not a persistence one.
    deployment = db.query(Deployment).filter(
        Deployment.deploymentId == uuid.UUID(body["deploymentId"])
    ).one()
    persisted = json.loads(deployment.userInputVar)
    assert persisted["terraform"]["task_pdf"]["all"]["content_b64"] == _b64(pdf)
    # Existing terraform vars were preserved through the merge.
    assert persisted["terraform"]["some_other"] == "x"

    # The job's keyword arguments carry the same dict — the worker sees
    # what's in the DB — and the commit the deployment is pinned to.
    job_kwargs = patched_celery.call_args.kwargs["kwargs"]
    user_vars_arg = job_kwargs["user_vars"]
    assert user_vars_arg["terraform"]["task_pdf"]["all"]["content_b64"] == _b64(pdf)
    assert job_kwargs["commit_sha"] == deployment.commit_sha
    assert len(deployment.commit_sha) == 40


@pytest.mark.integration
def test_post_deployment_413_when_single_file_too_large(
    client, db, mock_user, patched_celery
):
    """One file over 2 MB → 413, no row created, no celery dispatch."""
    app = _ensure_app(db, mock_user)

    big = b"x" * (2 * 1024 * 1024 + 10)
    response = client.post(
        "/deployments/",
        json={
            "name": "too-big",
            "appId": str(app.appId),
            "releaseTag": "v1",
            "teams": _teams(mock_user),
            "files": {
                "task_pdf": {
                    "all": {
                        "name": "huge.bin",
                        "content_b64": _b64(big),
                        "size": len(big),
                    }
                }
            },
        },
    )
    assert response.status_code == 413
    assert response.json()["detail"]["reason"] == "file_too_large"
    patched_celery.assert_not_called()
    # No deployment row should have been written.
    assert (
        db.query(Deployment)
        .filter(Deployment.name == "too-big")
        .first() is None
    )


@pytest.mark.integration
def test_post_deployment_422_on_invalid_base64(
    client, db, mock_user, patched_celery
):
    app = _ensure_app(db, mock_user)

    response = client.post(
        "/deployments/",
        json={
            "name": "bad-b64",
            "appId": str(app.appId),
            "releaseTag": "v1",
            "teams": _teams(mock_user),
            "files": {
                "task_pdf": {
                    "all": {
                        "name": "broken.bin",
                        "content_b64": "not^valid$base64",
                        "size": 12,
                    }
                }
            },
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "file_b64_invalid"
    patched_celery.assert_not_called()


@pytest.mark.integration
def test_detail_endpoint_strips_file_bytes(
    client, db, mock_user, patched_celery
):
    app = _ensure_app(db, mock_user)

    pdf = b"%PDF-1.4\n" + b"PDF data" * 100
    create = client.post(
        "/deployments/",
        json={
            "name": "stripped-detail",
            "appId": str(app.appId),
            "releaseTag": "v1",
            "teams": _teams(mock_user),
            "files": {
                "task_pdf": {
                    "all": {
                        "name": "x.pdf",
                        "content_b64": _b64(pdf),
                        "size": len(pdf),
                        "content_type": "application/pdf",
                    }
                }
            },
        },
    ).json()

    detail = client.get(f"/deployments/{create['deploymentId']}").json()
    upload = detail["userInputVar"]["terraform"]["task_pdf"]["all"]
    assert upload["name"] == "x.pdf"
    assert upload["size"] == len(pdf)
    assert "content_b64" not in upload


@pytest.mark.integration
def test_download_endpoint_returns_owner_bytes(
    client, db, mock_user, patched_celery
):
    app = _ensure_app(db, mock_user)

    payload_bytes = b"hello, world\n" * 10
    create = client.post(
        "/deployments/",
        json={
            "name": "downloadable",
            "appId": str(app.appId),
            "releaseTag": "v1",
            "teams": _teams(mock_user),
            "files": {
                "task_pdf": {
                    "all": {
                        "name": "hello.txt",
                        "content_b64": _b64(payload_bytes),
                        "size": len(payload_bytes),
                        "content_type": "text/plain",
                    }
                }
            },
        },
    ).json()
    deployment_id = create["deploymentId"]

    response = client.get(f"/deployments/{deployment_id}/files/task_pdf/all")
    assert response.status_code == 200
    assert response.content == payload_bytes
    # Served for saving, never rendered: not the uploader's type (.github#7 R).
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "hello.txt" in response.headers["content-disposition"]


@pytest.mark.integration
def test_upload_names_are_stored_without_paths_or_header_breakers(client, db, mock_user, patched_celery):
    app = _ensure_app(db, mock_user)
    payload_bytes = b"%PDF-1.4 x"
    create = client.post(
        "/deployments/",
        json={
            "name": "names",
            "appId": str(app.appId),
            "releaseTag": "v1",
            "teams": _teams(mock_user),
            "files": {
                "task_pdf": {
                    "all": {
                        "name": '../../etc/cron.d/a"\r\nSet-Cookie: x=1.pdf',
                        "content_b64": _b64(payload_bytes),
                        "size": len(payload_bytes),
                        "content_type": "text/html",
                    }
                }
            },
        },
    )
    assert create.status_code == 201, create.text
    deployment_id = create.json()["deploymentId"]

    response = client.get(f"/deployments/{deployment_id}/files/task_pdf/all")

    disposition = response.headers["content-disposition"]
    assert "/" not in disposition.split("filename*=")[0]
    assert chr(13) not in disposition and chr(10) not in disposition
    assert disposition.startswith('attachment; filename="')
    assert "set-cookie" not in {k.lower() for k in response.headers}


@pytest.mark.integration
def test_download_endpoint_404_for_unknown_slot(
    client, db, mock_user, patched_celery
):
    app = _ensure_app(db, mock_user)

    payload_bytes = b"x" * 32
    create = client.post(
        "/deployments/",
        json={
            "name": "with-one-slot",
            "appId": str(app.appId),
            "releaseTag": "v1",
            "teams": _teams(mock_user),
            "files": {
                "task_pdf": {
                    "all": {
                        "name": "h.txt",
                        "content_b64": _b64(payload_bytes),
                        "size": len(payload_bytes),
                    }
                }
            },
        },
    ).json()

    # Wrong variable name → 404.
    bad_var = client.get(
        f"/deployments/{create['deploymentId']}/files/no_such_var/all"
    )
    assert bad_var.status_code == 404
    # Right variable, wrong slot → 404.
    bad_slot = client.get(
        f"/deployments/{create['deploymentId']}/files/task_pdf/Team-X"
    )
    assert bad_slot.status_code == 404


@pytest.mark.integration
def test_download_endpoint_member_403(
    client, db, mock_user, patched_celery, unauth_client
):
    """A team member with read-access to the deployment must NOT be
    able to fetch file bytes — only the owner-view does."""
    app = _ensure_app(db, mock_user)

    payload_bytes = b"secret" * 100
    create = client.post(
        "/deployments/",
        json={
            "name": "owner-only-download",
            "appId": str(app.appId),
            "releaseTag": "v1",
            "teams": _teams(mock_user),
            "files": {
                "task_pdf": {
                    "all": {
                        "name": "secret.txt",
                        "content_b64": _b64(payload_bytes),
                        "size": len(payload_bytes),
                    }
                }
            },
        },
    ).json()

    # Spawn a second user (a student member of the deployment) and
    # rebind the auth dependency to them. The deployment has no team
    # mapping, so we attach the student via UserToDeployment.
    from app.main import app as fastapi_app
    from app.models import UserToDeployment
    from app.utils.keycloak_auth import get_current_user_keycloak

    student = User(
        userId=uuid.uuid4(),
        keycloak_id=f"kc-student-{uuid.uuid4().hex[:6]}",
        email="student@dhbw.de",
        username="stud",
        firstName="Stud",
        lastName="Ent",
        role=UserRole.STUDENT,
    )
    db.add(student)
    db.add(UserToDeployment(
        userToDeploymentId=uuid.uuid4(),
        userId=student.userId,
        deploymentId=uuid.UUID(create["deploymentId"]),
    ))
    db.commit()

    # Member access: download must 403 even though the deployment is
    # readable (read-access is a separate gate).
    fastapi_app.dependency_overrides[get_current_user_keycloak] = lambda: student
    try:
        resp = client.get(
            f"/deployments/{create['deploymentId']}/files/task_pdf/all"
        )
    finally:
        # Hand back to the test client's mock_user so other tests
        # in the same session aren't affected.
        fastapi_app.dependency_overrides[get_current_user_keycloak] = lambda: mock_user
    assert resp.status_code == 403


@pytest.mark.integration
def test_rejected_upload_is_logged_as_a_security_event(
    client, db, mock_user, patched_celery, caplog
):
    app = _ensure_app(db, mock_user)

    evil = b"MZ" + b"\x00" * 200  # a Windows program
    with caplog.at_level("WARNING", logger="app.services.deployment_input"):
        response = client.post(
            "/deployments/",
            json={
                "name": "rejected",
                "appId": str(app.appId),
                "releaseTag": "v1",
                "files": {"task_pdf": {"all": {
                    "name": "evil\nupload_rejected user=forged.exe",
                    "content_b64": _b64(evil),
                    "size": len(evil),
                }}},
            },
        )
    assert response.status_code == 422
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("upload_rejected")]
    assert len(lines) == 1
    assert f"user={mock_user.userId}" in lines[0]
    assert 'reason="file_content_rejected"' in lines[0]
    assert 'variable="task_pdf"' in lines[0]
    assert "forged" not in lines[0]


@pytest.mark.integration
def test_soft_delete_drops_the_uploaded_bytes_but_keeps_the_metadata(
    client, db, mock_user, patched_celery
):
    from app.crud import deployments as crud_deployments

    app = _ensure_app(db, mock_user)
    pdf = b"%PDF-1.4\n" + b"PDF data" * 100
    created = client.post(
        "/deployments/",
        json={
            "name": "to-delete",
            "appId": str(app.appId),
            "releaseTag": "v1",
            "teams": _teams(mock_user),
            "files": {"task_pdf": {"all": {
                "name": "x.pdf", "content_b64": _b64(pdf), "size": len(pdf),
            }}},
        },
    ).json()
    dep_id = uuid.UUID(created["deploymentId"])
    stored = db.query(Deployment).filter(Deployment.deploymentId == dep_id).one()
    assert "content_b64" in stored.userInputVar

    assert crud_deployments.soft_delete_deployment(db, dep_id) is True

    db.expire_all()
    row = db.query(Deployment).filter(Deployment.deploymentId == dep_id).one()
    assert row.deleted_at is not None
    assert "content_b64" not in row.userInputVar
    assert json.loads(row.userInputVar)["terraform"]["task_pdf"]["all"]["name"] == "x.pdf"


@pytest.mark.integration
def test_stored_file_carries_a_mac_the_worker_can_check(client, db, mock_user, patched_celery):
    from app.utils import upload_mac

    app = _ensure_app(db, mock_user)
    pdf = b"%PDF-1.4\n" + b"PDF data" * 100
    created = client.post(
        "/deployments/",
        json={
            "name": "sealed",
            "appId": str(app.appId),
            "releaseTag": "v1",
            "teams": _teams(mock_user),
            "files": {"task_pdf": {"all": {"name": "x.pdf", "content_b64": _b64(pdf), "size": len(pdf)}}},
        },
    ).json()
    stored = db.query(Deployment).filter(Deployment.deploymentId == uuid.UUID(created["deploymentId"])).one()
    entry = json.loads(stored.userInputVar)["terraform"]["task_pdf"]["all"]
    assert entry["mac"] == upload_mac.sign("task_pdf", "all", "x.pdf", pdf)
    assert entry["mac"] != upload_mac.sign("task_pdf", "all", "x.pdf", pdf + b"!")
    # the seal is for the worker, not for API responses
    detail = client.get(f"/deployments/{created['deploymentId']}").json()
    assert "mac" not in detail["userInputVar"]["terraform"]["task_pdf"]["all"]


@pytest.mark.unit
def test_strip_covers_team_and_user_scoped_file_vars():
    from app.services.deployment_input import strip_file_vars_from_user_input

    slot = {"name": "x.pdf", "content_b64": "QQ==", "size": 1, "content_type": "application/pdf", "mac": "ab"}
    out = strip_file_vars_from_user_input(
        {"terraform": {"f": {"Team-1": {"uploaded": dict(slot)}}, "g": {"all": dict(slot)}}}
    )["terraform"]
    for entry in (out["f"]["Team-1"]["uploaded"], out["g"]["all"]):
        assert "content_b64" not in entry and "mac" not in entry
        assert entry["name"] == "x.pdf"


@pytest.mark.unit
def test_is_stored_file_var_knows_both_shapes():
    from app.services.deployment_input import is_stored_file_var

    slot = {"name": "x.pdf", "content_b64": "QQ==", "size": 1, "content_type": "application/pdf"}
    assert is_stored_file_var({"all": dict(slot)})
    assert is_stored_file_var({"Team-1": {"uploaded": dict(slot)}})
    assert not is_stored_file_var({"Team-1": {"other": 1}})
    assert not is_stored_file_var({})
    assert not is_stored_file_var("x")


@pytest.mark.integration
def test_download_endpoint_serves_team_scoped_files(client, db, mock_user, patched_celery):
    app = _ensure_app(db, mock_user)
    payload_bytes = b"team file\n"
    create = client.post(
        "/deployments/",
        json={
            "name": "team-scoped-download",
            "appId": str(app.appId),
            "releaseTag": "v1",
            "teams": _teams(mock_user),
            "files": {
                "task_pdf": {
                    "all": {
                        "name": "t.txt",
                        "content_b64": _b64(payload_bytes),
                        "size": len(payload_bytes),
                        "content_type": "text/plain",
                    }
                }
            },
        },
    ).json()
    deployment_id = create["deploymentId"]

    # Rewrite the stored variable into the scope=team|user shape.
    from app.models import Deployment

    row = db.query(Deployment).filter(Deployment.deploymentId == deployment_id).one()
    stored = json.loads(row.userInputVar)
    block_key = next(k for k in ("variables", "terraform") if k in stored)
    stored[block_key]["task_pdf"] = {"Team-1": {"uploaded": stored[block_key]["task_pdf"]["all"]}}
    row.userInputVar = json.dumps(stored)
    db.commit()

    response = client.get(f"/deployments/{deployment_id}/files/task_pdf/Team-1")
    assert response.status_code == 200
    assert response.content == payload_bytes
