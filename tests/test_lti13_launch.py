from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import Course, User, UserRole


def test_lti13_routes_registered():
    paths = {route.path for route in app.routes}
    assert "/lti13/login" in paths
    assert "/lti13/launch" in paths
    assert "/lti13/jwks" in paths
    assert "/handoff/mint" in paths


def test_login_redirects_to_platform_auth():
    client = TestClient(app)
    with patch.object(settings, "LTI13_PLATFORM_ISSUER", "https://moodle.example.com"):
        response = client.get(
            "/lti13/login",
            params={
                "iss": "https://moodle.example.com",
                "login_hint": "student-1",
                "target_link_uri": "https://app.example.com/",
                "client_id": "client-123",
            },
            follow_redirects=False,
        )
    assert response.status_code == 302
    assert response.headers["location"].startswith("https://moodle.example.com/mod/lti/auth.php?")
    assert "lti13_state" in response.cookies


def test_login_accepts_post_from_moodle():
    """Moodle sends the 3rd-party login initiation as a POST (form-encoded),
    not a GET — verified against a real Moodle 5.x instance, 2026-09-25.
    The IMS spec allows either; only supporting GET meant a real Moodle
    launch always failed with 405 before this was fixed."""
    client = TestClient(app)
    with patch.object(settings, "LTI13_PLATFORM_ISSUER", "https://moodle.example.com"):
        response = client.post(
            "/lti13/login",
            data={
                "iss": "https://moodle.example.com",
                "login_hint": "student-1",
                "target_link_uri": "https://app.example.com/",
                "client_id": "client-123",
            },
            follow_redirects=False,
        )
    assert response.status_code == 302
    assert response.headers["location"].startswith("https://moodle.example.com/mod/lti/auth.php?")
    assert "lti13_state" in response.cookies


def test_login_state_cookie_is_secure_by_default():
    """SameSite=None requires Secure in every modern browser — without it
    the cookie is silently dropped and /launch always fails with 'State
    mismatch', not just in production. Only DEV_MODE opts out (plain-HTTP
    localhost development)."""
    client = TestClient(app)
    with (
        patch.object(settings, "LTI13_PLATFORM_ISSUER", "https://moodle.example.com"),
        patch.object(settings, "DEV_MODE", False),
    ):
        response = client.get(
            "/lti13/login",
            params={"iss": "https://moodle.example.com"},
            follow_redirects=False,
        )
    set_cookie = response.headers["set-cookie"]
    assert "Secure" in set_cookie


def test_login_rejects_unknown_issuer():
    client = TestClient(app)
    with patch.object(settings, "LTI13_PLATFORM_ISSUER", "https://moodle.example.com"):
        response = client.get(
            "/lti13/login",
            params={"iss": "https://evil.example.com"},
            follow_redirects=False,
        )
    assert response.status_code == 400


def _do_login(client):
    with patch.object(settings, "LTI13_PLATFORM_ISSUER", "https://moodle.example.com"):
        login_response = client.get(
            "/lti13/login",
            params={"iss": "https://moodle.example.com", "client_id": "client-123"},
            follow_redirects=False,
        )
    state = login_response.cookies["lti13_state"]
    client.cookies.set("lti13_state", state)
    return state


def test_launch_creates_new_user_and_course(db):
    client = TestClient(app)
    with patch.object(settings, "LTI13_CLIENT_ID", "client-123"):
        state = _do_login(client)
        with (
            patch(
                "app.routers.lti13.lti13_service.validate_id_token",
                return_value={},
            ),
            patch(
                "app.routers.lti13.lti13_service.extract_user_info",
                return_value={
                    "email": "new-student@example.com",
                    "name": "New Student",
                    "role": "student",
                    "course": "Course 101",
                },
            ),
        ):
            response = client.post(
                "/lti13/launch",
                data={"id_token": "fake-token", "state": state},
            )

    assert response.status_code == 200
    assert "session_token=" in response.text

    user = db.query(User).filter(User.email == "new-student@example.com").first()
    assert user is not None
    assert user.role == UserRole.STUDENT

    course = db.query(Course).filter(Course.name == "Course 101").first()
    assert course is not None


def test_launch_promotes_existing_student_to_teacher(db):
    existing = User(
        email="promote-me@example.com",
        username="promote-me@example.com",
        role=UserRole.STUDENT,
    )
    db.add(existing)
    db.commit()

    client = TestClient(app)
    with patch.object(settings, "LTI13_CLIENT_ID", "client-123"):
        state = _do_login(client)
        with (
            patch(
                "app.routers.lti13.lti13_service.validate_id_token",
                return_value={},
            ),
            patch(
                "app.routers.lti13.lti13_service.extract_user_info",
                return_value={
                    "email": "promote-me@example.com",
                    "name": "Promote Me",
                    "role": "instructor",
                    "course": "",
                },
            ),
        ):
            response = client.post(
                "/lti13/launch",
                data={"id_token": "fake-token", "state": state},
            )

    assert response.status_code == 200
    db.refresh(existing)
    assert existing.role == UserRole.TEACHER


def test_launch_rejects_state_mismatch():
    client = TestClient(app)
    with patch.object(settings, "LTI13_CLIENT_ID", "client-123"):
        _do_login(client)
        response = client.post(
            "/lti13/launch",
            data={"id_token": "fake-token", "state": "not-the-real-state"},
        )
    assert response.status_code == 400
