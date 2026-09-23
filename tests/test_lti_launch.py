"""Tests for LTI 1.1 launch endpoint.

Covers the regression where ?session_token was missing from the redirect,
causing the frontend to fall back to Keycloak OIDC and show a login screen
instead of the dashboard.
"""

from __future__ import annotations

import urllib.parse
from unittest.mock import patch

import pytest


def _extract_redirect_url(html: str) -> str:
    """Parse the JS redirect URL from the HTML returned by /lti/launch."""
    # HTML is: <script>window.location.href = "URL";</script>
    start = html.find('window.location.href = "') + len('window.location.href = "')
    end = html.find('";', start)
    return html[start:end]


@pytest.fixture
def lti_form_data():
    return {
        "oauth_consumer_key": "appstore-lti-key",
        "oauth_signature": "PLACEHOLDER",
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_version": "1.0",
        "oauth_timestamp": "1000000000",
        "oauth_nonce": "abc123",
        "lti_message_type": "basic-lti-launch-request",
        "lti_version": "LTI-1p0",
        "lis_person_contact_email_primary": "student@dhbw.de",
        "lis_person_name_full": "Max Mustermann",
        "context_title": "Softwareentwicklung 3",
        "roles": "Learner",
    }


def test_lti_launch_includes_session_token(unauth_client, lti_form_data):
    """A valid LTI 1.1 launch must include session_token in the redirect URL.

    Regression: before this fix, session_token was absent and main.ts fell
    through to auth.initialize() → login page shown inside Moodle iframe.
    """
    with (
        patch("app.routers.lti._verify_oauth_signature", return_value=True),
        patch("app.config.settings.LTI_CONSUMER_KEY", "appstore-lti-key"),
    ):
        response = unauth_client.post(
            "/lti/launch",
            data=lti_form_data,
            follow_redirects=False,
        )

    assert response.status_code == 200
    body = response.text

    redirect_url = _extract_redirect_url(body)
    assert redirect_url, "No redirect URL found in response HTML"

    parsed = urllib.parse.urlparse(redirect_url)
    params = urllib.parse.parse_qs(parsed.query)

    # Core assertions: the fix
    assert "session_token" in params, (
        "session_token missing from LTI 1.1 redirect — "
        "frontend will fall back to Keycloak login"
    )
    assert params["session_token"][0], "session_token must not be empty"

    # Existing params still present
    assert params.get("lti", [""])[0] == "1"
    assert params.get("email", [""])[0] == "student@dhbw.de"
    assert params.get("role", [""])[0] == "student"


def test_lti_launch_provisions_user_in_db(unauth_client, db, lti_form_data):
    """LTI 1.1 launch must JIT-provision the launching user so the session
    token can resolve to a DB record when the frontend calls the API."""
    from app.models import User

    with (
        patch("app.routers.lti._verify_oauth_signature", return_value=True),
        patch("app.config.settings.LTI_CONSUMER_KEY", "appstore-lti-key"),
    ):
        response = unauth_client.post("/lti/launch", data=lti_form_data)

    assert response.status_code == 200

    user = db.query(User).filter(User.email == "student@dhbw.de").first()
    assert user is not None, "User was not JIT-provisioned by LTI 1.1 launch"
    assert user.email == "student@dhbw.de"


def test_lti_launch_rejects_wrong_consumer_key(unauth_client, lti_form_data):
    """Wrong consumer key must return 403."""
    lti_form_data["oauth_consumer_key"] = "wrong-key"
    response = unauth_client.post("/lti/launch", data=lti_form_data)
    assert response.status_code == 403


def test_lti_launch_rejects_bad_signature(unauth_client, lti_form_data):
    """Invalid OAuth signature must return 403."""
    with patch("app.config.settings.LTI_CONSUMER_KEY", "appstore-lti-key"):
        response = unauth_client.post("/lti/launch", data=lti_form_data)
    assert response.status_code == 403


def test_lti_launch_promotes_existing_student_to_teacher(unauth_client, db, lti_form_data):
    """An instructor launch must raise an already-provisioned student's role.

    Dennis first appears as a self-service STUDENT, then launches via Moodle
    as a Trainer — the launch must promote him to TEACHER.
    """
    import uuid
    from app.models import User, UserRole

    db.add(User(
        userId=uuid.uuid4(),
        email="dennis.pfisterer@dhbw.de",
        username="dennis.pfisterer",
        role=UserRole.STUDENT,
    ))
    db.commit()

    lti_form_data["lis_person_contact_email_primary"] = "dennis.pfisterer@dhbw.de"
    lti_form_data["roles"] = "Instructor"

    with (
        patch("app.routers.lti._verify_oauth_signature", return_value=True),
        patch("app.config.settings.LTI_CONSUMER_KEY", "appstore-lti-key"),
    ):
        response = unauth_client.post("/lti/launch", data=lti_form_data)

    assert response.status_code == 200
    user = db.query(User).filter(User.email == "dennis.pfisterer@dhbw.de").first()
    assert user.role == UserRole.TEACHER


def test_lti_launch_does_not_demote_teacher(unauth_client, db, lti_form_data):
    """A learner launch must not lower an existing TEACHER's role."""
    import uuid
    from app.models import User, UserRole

    db.add(User(
        userId=uuid.uuid4(),
        email="dennis.pfisterer@dhbw.de",
        username="dennis.pfisterer",
        role=UserRole.TEACHER,
    ))
    db.commit()

    lti_form_data["lis_person_contact_email_primary"] = "dennis.pfisterer@dhbw.de"
    lti_form_data["roles"] = "Learner"

    with (
        patch("app.routers.lti._verify_oauth_signature", return_value=True),
        patch("app.config.settings.LTI_CONSUMER_KEY", "appstore-lti-key"),
    ):
        response = unauth_client.post("/lti/launch", data=lti_form_data)

    assert response.status_code == 200
    user = db.query(User).filter(User.email == "dennis.pfisterer@dhbw.de").first()
    assert user.role == UserRole.TEACHER
