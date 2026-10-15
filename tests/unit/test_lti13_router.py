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
    lti13_login_get reads request.query_params, vs. request.form() in
    lti13_login_post."""
    return _mock_request(query_params=params, method="GET")


def _known_state(nonce="nonce-1"):
    """The nonce store as the launch sees it: one stored state (no database in unit tests)."""
    return patch.object(lti13, "_pop_nonce", return_value=nonce)


async def test_lti13_jwks_returns_empty_keyset():
    response = await lti13.lti13_jwks()
    assert response.status_code == 200
    assert response.body == b'{"keys":[]}'


async def test_lti13_login_redirects_with_state_cookie():
    db = MagicMock()
    with (
        patch.object(settings, "LTI13_PLATFORM_ISSUER", "https://moodle.example.com"),
        patch.object(settings, "LTI13_CLIENT_ID", "client-123"),
    ):
        redirect = await lti13.lti13_login_get(
            db=db,
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
    # The state/nonce pair goes to the database, not into process memory.
    stored = db.add.call_args.args[0]
    assert stored.state in redirect.headers["set-cookie"]
    assert f"nonce={stored.nonce}" in redirect.headers["location"]
    db.commit.assert_called()


async def test_lti13_login_accepts_post_from_moodle():
    """Moodle sends this as a POST with form-encoded params, not a GET —
    verified against a real Moodle 5.x instance, 2026-09-25."""
    with (
        patch.object(settings, "LTI13_PLATFORM_ISSUER", "https://moodle.example.com"),
        patch.object(settings, "LTI13_CLIENT_ID", "client-123"),
    ):
        redirect = await lti13.lti13_login_post(
            db=MagicMock(),
            request=_mock_request(
                method="POST",
                form={"iss": "https://moodle.example.com", "client_id": "client-123"},
            ),
        )
    assert redirect.status_code == 302
    assert "auth.php" in redirect.headers["location"]


async def test_lti13_login_warns_on_client_id_mismatch_but_still_redirects():
    with (
        patch.object(settings, "LTI13_PLATFORM_ISSUER", "https://moodle.example.com"),
        patch.object(settings, "LTI13_CLIENT_ID", "configured-client"),
    ):
        redirect = await lti13.lti13_login_get(
            db=MagicMock(),
            request=_mock_login_request(
                iss="https://moodle.example.com",
                client_id="different-client",
            ),
        )
    assert redirect.status_code == 302


async def test_lti13_login_requires_iss():
    with pytest.raises(HTTPException) as exc_info:
        await lti13.lti13_login_get(request=_mock_login_request(iss=None), db=MagicMock())
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
    with _known_state(None), pytest.raises(HTTPException) as exc_info:  # never stored via /login
        await lti13.lti13_launch(request=request, db=MagicMock())
    assert exc_info.value.status_code == 400


async def test_lti13_launch_rejects_when_not_configured():
    request = _mock_request(
        cookies={"lti13_state": "known-state"},
        form={"id_token": "tok", "state": "known-state"},
    )
    with (
        _known_state(),
        patch.object(settings, "LTI13_CLIENT_ID", ""),
        pytest.raises(HTTPException) as exc_info,
    ):
        await lti13.lti13_launch(request=request, db=MagicMock())
    assert exc_info.value.status_code == 503


async def test_lti13_launch_rejects_invalid_id_token():
    request = _mock_request(
        cookies={"lti13_state": "known-state"},
        form={"id_token": "tok", "state": "known-state"},
    )
    with (
        _known_state(),
        patch.object(settings, "LTI13_CLIENT_ID", "client-123"),
        patch.object(lti13.lti13_service, "validate_id_token", side_effect=ValueError("bad token")),
        pytest.raises(HTTPException) as exc_info,
    ):
        await lti13.lti13_launch(request=request, db=MagicMock())
    assert exc_info.value.status_code == 403


async def test_lti13_launch_rejects_missing_email_claim():
    request = _mock_request(
        cookies={"lti13_state": "known-state"},
        form={"id_token": "tok", "state": "known-state"},
    )
    with (
        _known_state(),
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
        _known_state(),
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
        _known_state(),
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
