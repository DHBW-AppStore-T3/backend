from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.config import settings
from app.models import UserRole
from app.routers import lti13

pytestmark = pytest.mark.unit


def _mock_request(cookies=None, form=None, query_params=None, method="POST"):
    request = MagicMock()
    request.cookies = cookies or {}
    request.form = AsyncMock(return_value=form or {})
    request.query_params = query_params or {}
    request.method = method
    return request


def _mock_login_request(**params):
    """A GET request carrying `params` in the query string — matches how
    lti13_login reads request.query_params for a GET, vs. request.form()
    for a POST."""
    return _mock_request(query_params=params, method="GET")


async def test_lti13_jwks_returns_empty_keyset():
    response = await lti13.lti13_jwks()
    assert response.status_code == 200
    assert response.body == b'{"keys":[]}'


async def test_lti13_login_redirects_with_state_cookie():
    lti13._nonce_store.clear()
    with (
        patch.object(settings, "LTI13_PLATFORM_ISSUER", "https://moodle.example.com"),
        patch.object(settings, "LTI13_CLIENT_ID", "client-123"),
    ):
        redirect = await lti13.lti13_login(
            request=_mock_login_request(
                iss="https://moodle.example.com",
                login_hint="hint",
                target_link_uri="https://app.example.com",
                client_id="client-123",
                lti_message_hint="msg-hint",
            ),
        )

    assert redirect.status_code == 302
    assert "auth.php" in redirect.headers["location"]
    assert "lti13_state" in redirect.headers.get("set-cookie", "")


async def test_lti13_login_accepts_post_from_moodle():
    """Moodle sends this as a POST with form-encoded params, not a GET —
    verified against a real Moodle 5.x instance, 2026-09-25."""
    lti13._nonce_store.clear()
    with (
        patch.object(settings, "LTI13_PLATFORM_ISSUER", "https://moodle.example.com"),
        patch.object(settings, "LTI13_CLIENT_ID", "client-123"),
    ):
        redirect = await lti13.lti13_login(
            request=_mock_request(
                method="POST",
                form={"iss": "https://moodle.example.com", "client_id": "client-123"},
            ),
        )
    assert redirect.status_code == 302
    assert "auth.php" in redirect.headers["location"]


async def test_lti13_login_warns_on_client_id_mismatch_but_still_redirects():
    lti13._nonce_store.clear()
    with (
        patch.object(settings, "LTI13_PLATFORM_ISSUER", "https://moodle.example.com"),
        patch.object(settings, "LTI13_CLIENT_ID", "configured-client"),
    ):
        redirect = await lti13.lti13_login(
            request=_mock_login_request(
                iss="https://moodle.example.com",
                client_id="different-client",
            ),
        )
    assert redirect.status_code == 302


async def test_lti13_login_requires_iss():
    with pytest.raises(HTTPException) as exc_info:
        await lti13.lti13_login(request=_mock_login_request(iss=None))
    assert exc_info.value.status_code == 400


async def test_lti13_launch_rejects_missing_id_token():
    with pytest.raises(HTTPException) as exc_info:
        await lti13.lti13_launch(request=_mock_request(form={}), db=MagicMock())
    assert exc_info.value.status_code == 400


async def test_lti13_launch_rejects_missing_state_cookie():
    request = _mock_request(form={"id_token": "tok", "state": "abc"})
    with pytest.raises(HTTPException) as exc_info:
        await lti13.lti13_launch(request=request, db=MagicMock())
    assert exc_info.value.status_code == 400


async def test_lti13_launch_rejects_expired_or_unknown_state():
    request = _mock_request(
        cookies={"lti13_state": "known-state"},
        form={"id_token": "tok", "state": "known-state"},
    )
    lti13._nonce_store.clear()  # state was never stored via /login
    with pytest.raises(HTTPException) as exc_info:
        await lti13.lti13_launch(request=request, db=MagicMock())
    assert exc_info.value.status_code == 400


async def test_lti13_launch_rejects_when_not_configured():
    lti13._store_nonce("known-state", "nonce-1")
    request = _mock_request(
        cookies={"lti13_state": "known-state"},
        form={"id_token": "tok", "state": "known-state"},
    )
    with (
        patch.object(settings, "LTI13_CLIENT_ID", ""),
        pytest.raises(HTTPException) as exc_info,
    ):
        await lti13.lti13_launch(request=request, db=MagicMock())
    assert exc_info.value.status_code == 503


async def test_lti13_launch_rejects_invalid_id_token():
    lti13._store_nonce("known-state", "nonce-1")
    request = _mock_request(
        cookies={"lti13_state": "known-state"},
        form={"id_token": "tok", "state": "known-state"},
    )
    with (
        patch.object(settings, "LTI13_CLIENT_ID", "client-123"),
        patch.object(lti13.lti13_service, "validate_id_token", side_effect=ValueError("bad token")),
        pytest.raises(HTTPException) as exc_info,
    ):
        await lti13.lti13_launch(request=request, db=MagicMock())
    assert exc_info.value.status_code == 403


async def test_lti13_launch_rejects_missing_email_claim():
    lti13._store_nonce("known-state", "nonce-1")
    request = _mock_request(
        cookies={"lti13_state": "known-state"},
        form={"id_token": "tok", "state": "known-state"},
    )
    with (
        patch.object(settings, "LTI13_CLIENT_ID", "client-123"),
        patch.object(lti13.lti13_service, "validate_id_token", return_value={}),
        patch.object(
            lti13.lti13_service,
            "extract_user_info",
            return_value={"email": "", "name": "", "role": "student", "course": ""},
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await lti13.lti13_launch(request=request, db=MagicMock())
    assert exc_info.value.status_code == 400


async def test_lti13_launch_creates_new_user_and_course_jit():
    lti13._store_nonce("known-state", "nonce-1")
    request = _mock_request(
        cookies={"lti13_state": "known-state"},
        form={"id_token": "tok", "state": "known-state"},
    )
    db = MagicMock()
    # No existing course, no existing user.
    db.query.return_value.filter.return_value.first.side_effect = [None, None]
    created_course = MagicMock(courseId="course-uuid-1")

    def _refresh(obj):
        if hasattr(obj, "courseId") and obj.courseId is None:
            obj.courseId = created_course.courseId

    db.refresh.side_effect = _refresh

    with (
        patch.object(settings, "LTI13_CLIENT_ID", "client-123"),
        patch.object(settings, "APP_BASE_URL", "https://app.example.com"),
        patch.object(settings, "LTI13_SESSION_SECRET", "test-secret"),
        patch.object(lti13.lti13_service, "validate_id_token", return_value={}),
        patch.object(
            lti13.lti13_service,
            "extract_user_info",
            return_value={
                "email": "new@example.com",
                "name": "New Person",
                "role": "student",
                "course": "Course 101",
            },
        ),
    ):
        response = await lti13.lti13_launch(request=request, db=db)

    assert response.status_code == 200
    assert "session_token=" in response.body.decode()
    assert db.add.call_count == 2  # course + user


async def test_lti13_launch_promotes_existing_user_and_updates_course():
    lti13._store_nonce("known-state", "nonce-1")
    request = _mock_request(
        cookies={"lti13_state": "known-state"},
        form={"id_token": "tok", "state": "known-state"},
    )
    db = MagicMock()
    existing_course = MagicMock(courseId="course-uuid-2")
    existing_user = MagicMock(role=UserRole.STUDENT, courseId=None)
    db.query.return_value.filter.return_value.first.side_effect = [
        existing_course,
        existing_user,
    ]

    with (
        patch.object(settings, "LTI13_CLIENT_ID", "client-123"),
        patch.object(settings, "LTI13_SESSION_SECRET", "test-secret"),
        patch.object(lti13.lti13_service, "validate_id_token", return_value={}),
        patch.object(
            lti13.lti13_service,
            "extract_user_info",
            return_value={
                "email": "existing@example.com",
                "name": "Existing Person",
                "role": "instructor",
                "course": "Course 202",
            },
        ),
    ):
        response = await lti13.lti13_launch(request=request, db=db)

    assert response.status_code == 200
    assert existing_user.role == UserRole.TEACHER
    assert existing_user.courseId == "course-uuid-2"
