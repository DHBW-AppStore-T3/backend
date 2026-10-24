"""Writes a browser sends for another site are refused (app/utils/cross_site.py, review S3)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.utils.cross_site import CrossSiteWriteGuard, is_allowed

pytestmark = pytest.mark.unit

UI = "https://appstore.zone.example"
SELF_SERVICE = "https://self-service.zone.example"
STUDENT_APP = "https://ide-1234abcd.zone.example"
TRUSTED = frozenset({UI, SELF_SERVICE})


@pytest.mark.parametrize(
    ("method", "path", "site", "origin", "allowed"),
    [
        # the UIs' own requests
        ("POST", "/deployments/", "same-origin", UI, True),
        ("POST", "/deployments/", "same-origin", None, True),
        ("DELETE", "/deployments/x", "same-origin", SELF_SERVICE, True),
        ("POST", "/deployments/", "none", None, True),
        # a page of a student app, same site but another origin
        ("POST", "/admin/apps/x/versions/v1/approve", "same-site", STUDENT_APP, False),
        ("POST", "/deployments/x/workloads/w/reset", "same-site", None, False),
        ("PUT", "/apps/x", "cross-site", "https://evil.example", False),
        ("PATCH", "/apps/x", "cross-site", None, False),
        # a UI on another origin that is trusted (development: vite on another port)
        ("POST", "/deployments/", "same-site", UI, True),
        # browsers without Fetch Metadata: decided by Origin
        ("POST", "/deployments/", None, UI, True),
        ("POST", "/deployments/", None, STUDENT_APP, False),
        ("POST", "/deployments/", None, "null", False),
        # not a browser (scripts, the VM executor's OpenTofu): no ambient cookie
        ("POST", "/internal/tfstate/x", None, None, True),
        # reads are never refused
        ("GET", "/deployments/", "cross-site", STUDENT_APP, True),
        ("HEAD", "/health", "cross-site", None, True),
        ("OPTIONS", "/deployments/", "cross-site", STUDENT_APP, True),
        # Moodle's form posts of the LTI 1.3 launch
        ("POST", "/lti13/login", "cross-site", "https://moodle.other.example", True),
        ("POST", "/lti13/launch", "same-site", "https://moodle.zone.example", True),
    ],
)
def test_is_allowed(method, path, site, origin, allowed):
    assert is_allowed(method, path, site, origin, TRUSTED) is allowed


def test_without_trusted_origins_only_fetch_metadata_decides():
    assert is_allowed("POST", "/x", None, STUDENT_APP, frozenset()) is True
    assert is_allowed("POST", "/x", "same-site", STUDENT_APP, frozenset()) is False


def test_origins_compare_without_case_and_trailing_slash():
    assert is_allowed("POST", "/x", "same-site", "HTTPS://AppStore.zone.example/", TRUSTED) is True


def _client():
    api = FastAPI()

    @api.post("/deployments/{deployment_id}/pause")
    def pause(deployment_id: str):
        return {"paused": deployment_id}

    @api.get("/deployments/")
    def listing():
        return []

    return TestClient(CrossSiteWriteGuard(api, trusted_origins=[UI + "/", " ", SELF_SERVICE]))


def test_the_middleware_refuses_with_a_code_and_never_reaches_the_route():
    response = _client().post(
        "/deployments/d1/pause", headers={"Sec-Fetch-Site": "same-site", "Origin": STUDENT_APP}
    )

    assert response.status_code == 403
    assert response.json() == {
        "detail": {
            "code": "cross_site_request",
            "message": "Changes are only accepted from the AppStore's own pages",
        }
    }


def test_the_middleware_lets_the_uis_and_reads_through():
    client = _client()

    assert client.post(
        "/deployments/d1/pause", headers={"Sec-Fetch-Site": "same-origin"}
    ).json() == {"paused": "d1"}
    assert client.post("/deployments/d1/pause", headers={"Origin": SELF_SERVICE}).status_code == 200
    assert client.get("/deployments/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200


def test_the_api_has_the_guard():
    from app.main import app

    response = TestClient(app).post(
        "/deployments/00000000-0000-0000-0000-000000000000/pause",
        headers={"Sec-Fetch-Site": "same-site", "Origin": STUDENT_APP},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "cross_site_request"


def test_trusted_origins_are_the_frontend_the_cors_origins_and_the_extra_ones():
    cfg = Settings(
        APP_ENV="development",
        DATABASE_URL="postgresql://x/y",
        CREDENTIAL_ENCRYPTION_KEY="Q1PNlFd4It9oQPtjCcXcmB7wGDkY4w8KwpIRNSF4u7U=",
        APP_BASE_URL="https://AppStore.zone.example/some/path",
        CORS_ORIGINS=["http://localhost:5173"],
        CSRF_TRUSTED_ORIGINS=f" {SELF_SERVICE}/ ,,",
    )

    assert cfg.trusted_write_origins == {UI, "http://localhost:5173", SELF_SERVICE}
