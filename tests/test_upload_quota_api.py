"""POST /deployments: storage quota, request-body limit and the audit line, end to end."""

import base64
import random
import uuid

import pytest

from app.config import settings
from app.crud import deployments as crud_deployments
from app.models import App, Deployment
from tests.conftest import spec_with


def _pdf(size: int) -> bytes:
    """A file of ``size`` bytes that does not compress (the database compresses what it stores)."""
    return b"%PDF-1.4\n" + random.Random(7).randbytes(size - 9)


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


@pytest.fixture(autouse=True)
def _app_declares_the_file_variable(fake_git):
    fake_git.files["appstore.yaml"] = spec_with(
        variables=[{"name": "task_pdf", "type": "file", "mountPath": "/data/task.pdf"}]
    )


def _ensure_app(db, user) -> App:
    app = App(
        appId=uuid.uuid4(),
        name=f"app-{uuid.uuid4().hex[:8]}",
        userId=user.userId,
        git_link="https://example.com/acme/repo.git",
        is_private=True,
    )
    db.add(app)
    db.commit()
    db.refresh(app)
    return app


def _payload(app, user, pdf: bytes, name: str = "d") -> dict:
    return {
        "name": name,
        "appId": str(app.appId),
        "releaseTag": "v1",
        "teams": [{"name": "T", "userIds": [str(user.userId)]}],
        "files": {
            "task_pdf": {"all": {"name": "x.pdf", "content_b64": _b64(pdf), "size": len(pdf)}}
        },
    }


@pytest.fixture
def small_quota():
    old = (settings.MAX_STORED_INPUT_BYTES_PER_OWNER, settings.MAX_STORED_INPUT_BYTES_TOTAL)
    yield
    settings.MAX_STORED_INPUT_BYTES_PER_OWNER, settings.MAX_STORED_INPUT_BYTES_TOTAL = old


@pytest.mark.integration
def test_an_owner_cannot_store_more_than_the_quota_and_gets_it_back_on_delete(
    client, db, mock_user, queued, small_quota, caplog
):
    app = _ensure_app(db, mock_user)
    pdf = _pdf(4000)  # 5.3 KB as base64
    settings.MAX_STORED_INPUT_BYTES_PER_OWNER = 14_000
    settings.MAX_STORED_INPUT_BYTES_TOTAL = 0

    first = client.post("/deployments/", json=_payload(app, mock_user, pdf, "first"))
    second = client.post("/deployments/", json=_payload(app, mock_user, pdf, "second"))
    assert first.status_code == 201 and second.status_code == 201

    used = crud_deployments.stored_input_bytes(db, mock_user.userId)
    assert 10_000 < used < 14_000

    with caplog.at_level("WARNING", logger="app.services.deployment_input"):
        third = client.post("/deployments/", json=_payload(app, mock_user, pdf, "third"))
    assert third.status_code == 413
    detail = third.json()["detail"]
    assert detail["reason"] == "owner_storage_quota_exceeded" and detail["limit_bytes"] == 14_000
    assert db.query(Deployment).filter(Deployment.name == "third").count() == 0
    assert any(
        r.getMessage().startswith("upload_rejected")
        and 'reason="owner_storage_quota_exceeded"' in r.getMessage()
        for r in caplog.records
    )

    # Deleting a deployment frees its share.
    assert crud_deployments.soft_delete_deployment(db, uuid.UUID(first.json()["deploymentId"]))
    db.expire_all()
    assert crud_deployments.stored_input_bytes(db, mock_user.userId) < used - 4_000
    retry = client.post("/deployments/", json=_payload(app, mock_user, pdf, "third"))
    assert retry.status_code == 201


@pytest.mark.integration
def test_the_platform_wide_limit_answers_503(client, db, mock_user, queued, small_quota):
    app = _ensure_app(db, mock_user)
    pdf = _pdf(4000)
    settings.MAX_STORED_INPUT_BYTES_PER_OWNER = 0
    settings.MAX_STORED_INPUT_BYTES_TOTAL = 8_000

    assert (
        client.post("/deployments/", json=_payload(app, mock_user, pdf, "one")).status_code == 201
    )
    blocked = client.post("/deployments/", json=_payload(app, mock_user, pdf, "two"))
    assert blocked.status_code == 503
    assert blocked.json()["detail"] == {"reason": "upload_storage_full"}


@pytest.mark.integration
def test_a_deployment_without_files_is_not_limited_by_the_quota(
    client, db, mock_user, queued, small_quota
):
    app = _ensure_app(db, mock_user)
    settings.MAX_STORED_INPUT_BYTES_PER_OWNER = 1
    settings.MAX_STORED_INPUT_BYTES_TOTAL = 1
    payload = _payload(app, mock_user, b"x")
    del payload["files"]
    assert client.post("/deployments/", json=payload).status_code == 201


@pytest.mark.integration
def test_an_accepted_upload_is_logged_with_user_deployment_and_hash(
    client, db, mock_user, queued, caplog
):
    import hashlib

    app = _ensure_app(db, mock_user)
    pdf = b"%PDF-1.4\n%audit me\n"
    with caplog.at_level("INFO", logger="app.services.deployment_input"):
        response = client.post("/deployments/", json=_payload(app, mock_user, pdf))
    assert response.status_code == 201
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("upload_accepted")]
    assert len(lines) == 1
    assert f"user={mock_user.userId}" in lines[0]
    assert f"deployment={response.json()['deploymentId']}" in lines[0]
    assert f"sha256={hashlib.sha256(pdf).hexdigest()}" in lines[0]
    assert "x.pdf" not in lines[0]


@pytest.mark.integration
def test_a_refused_upload_is_not_logged_as_accepted(client, db, mock_user, queued, caplog):
    app = _ensure_app(db, mock_user)
    evil = b"MZ" + b"\x00" * 200  # a Windows program
    with caplog.at_level("INFO", logger="app.services.deployment_input"):
        response = client.post("/deployments/", json=_payload(app, mock_user, evil))
    assert response.status_code == 422
    assert not [r for r in caplog.records if r.getMessage().startswith("upload_accepted")]


@pytest.mark.integration
def test_an_oversized_body_is_refused_before_login_and_parsing(client, small_quota):
    old = settings.MAX_REQUEST_BODY_BYTES
    # The app was wired with the default at import time: the real 16 MiB limit applies.
    big = b'{"name":"' + b"a" * (old + 10) + b'"}'
    response = client.post(
        "/deployments/", content=big, headers={"content-type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "request_body_too_large"
