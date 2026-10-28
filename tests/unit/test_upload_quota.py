"""Space that uploaded files may take in the database, and the audit line of stored files."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from fastapi import HTTPException

from app.config import settings
from app.schemas import DeploymentFileUpload
from app.services.deployment_input import (
    attach_files_to_user_input,
    check_upload_quota,
    log_accepted_uploads,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def limits():
    old = (settings.MAX_STORED_INPUT_BYTES_PER_OWNER, settings.MAX_STORED_INPUT_BYTES_TOTAL)
    settings.MAX_STORED_INPUT_BYTES_PER_OWNER = 1000
    settings.MAX_STORED_INPUT_BYTES_TOTAL = 5000
    yield
    settings.MAX_STORED_INPUT_BYTES_PER_OWNER, settings.MAX_STORED_INPUT_BYTES_TOTAL = old


def _files(b64_len: int, count: int = 1) -> dict:
    upload = DeploymentFileUpload(name="a.txt", content_b64="A" * b64_len, size=1)
    return {"notes": {f"slot{i}": upload for i in range(count)}}


def _fail(*_):
    raise AssertionError("not expected to be asked")


def test_an_upload_that_fits_passes(limits):
    check_upload_quota(_files(400, count=2), lambda: 100, lambda: 100)  # 100 + 800 <= 1000


def test_an_upload_over_the_owners_share_is_refused_with_the_numbers(limits):
    with pytest.raises(HTTPException) as exc:
        check_upload_quota(_files(400, count=2), lambda: 201, lambda: 0)
    assert exc.value.status_code == 413
    assert exc.value.detail == {
        "reason": "owner_storage_quota_exceeded",
        "limit_bytes": 1000,
        "used_bytes": 201,
        "needed_bytes": 800,
    }


def test_an_upload_that_would_fill_the_database_is_refused_with_503(limits):
    with pytest.raises(HTTPException) as exc:
        check_upload_quota(_files(400), lambda: 0, lambda: 4700)
    assert exc.value.status_code == 503 and exc.value.detail == {"reason": "upload_storage_full"}


def test_a_request_without_files_is_not_checked_and_asks_nobody(limits):
    check_upload_quota(None, _fail, _fail)
    check_upload_quota({}, _fail, _fail)


def test_both_limits_off_means_no_database_questions():
    old = (settings.MAX_STORED_INPUT_BYTES_PER_OWNER, settings.MAX_STORED_INPUT_BYTES_TOTAL)
    settings.MAX_STORED_INPUT_BYTES_PER_OWNER = settings.MAX_STORED_INPUT_BYTES_TOTAL = 0
    try:
        check_upload_quota(_files(10_000_000), _fail, _fail)
    finally:
        settings.MAX_STORED_INPUT_BYTES_PER_OWNER, settings.MAX_STORED_INPUT_BYTES_TOTAL = old


def test_only_the_limit_that_is_set_is_asked():
    old = (settings.MAX_STORED_INPUT_BYTES_PER_OWNER, settings.MAX_STORED_INPUT_BYTES_TOTAL)
    settings.MAX_STORED_INPUT_BYTES_PER_OWNER, settings.MAX_STORED_INPUT_BYTES_TOTAL = 0, 5000
    try:
        check_upload_quota(_files(100), _fail, lambda: 10)
    finally:
        settings.MAX_STORED_INPUT_BYTES_PER_OWNER, settings.MAX_STORED_INPUT_BYTES_TOTAL = old


def test_accepted_files_are_reported_with_size_and_hash():
    content = b"hello"
    upload = DeploymentFileUpload(
        name="notes.txt", content_b64=base64.b64encode(content).decode(), size=len(content)
    )
    var = {"name": "notes", "varScope": "all", "fileExtensions": ["txt"]}
    accepted: list[dict] = []
    attach_files_to_user_input({"terraform": {}}, {"notes": {"all": upload}}, [var], accepted)
    assert accepted == [
        {
            "variable": "notes",
            "slot": "all",
            "size": 5,
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    ]


def test_the_audit_line_names_user_deployment_and_hash_but_not_the_client_file_name(caplog):
    entry = {
        "variable": "notes",
        "slot": 'T"\nupload_accepted user=forged',
        "size": 5,
        "sha256": "ab" * 32,
    }
    with caplog.at_level("INFO", logger="app.services.deployment_input"):
        log_accepted_uploads("u-1", "d-1", [entry])
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 1 and "\n" not in lines[0]
    assert lines[0].startswith('upload_accepted user=u-1 deployment=d-1 variable="notes" slot=')
    assert "size=5" in lines[0] and f"sha256={'ab' * 32}" in lines[0]
    # The slot name is JSON-quoted, so it cannot start a line of its own.
    assert json.dumps(entry["slot"]) in lines[0]
