import re
import urllib.parse
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
    # The self-service handoff minted tokens nothing accepted (.github#7 N).
    assert "/handoff/mint" not in paths


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
    mismatch', not just in production. Only APP_ENV=development opts out
    (plain-HTTP localhost development)."""
    client = TestClient(app)
    with (
        patch.object(settings, "LTI13_PLATFORM_ISSUER", "https://moodle.example.com"),
        patch.object(settings, "APP_ENV", "production"),
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


ISS = "https://moodle.example.com"


def _info(**overrides):
    info = {
        "issuer": ISS,
        "subject": "moodle-user-1",
        "email": "new-student@example.com",
        "name": "New Student",
        "role": "student",
        "context_id": "ctx-101",
        "course": "Course 101",
    }
    info.update(overrides)
    return info


def _launch(client, info):
    """One LTI launch of ``info`` through login and launch; the response."""
    with patch.object(settings, "LTI13_CLIENT_ID", "client-123"):
        state = _do_login(client)
        with (
            patch("app.routers.lti13.lti13_service.validate_id_token", return_value={}),
            patch("app.routers.lti13.lti13_service.extract_user_info", return_value=info),
        ):
            return client.post("/lti13/launch", data={"id_token": "fake-token", "state": state})


def _fragment(response) -> dict:
    target = re.search(r'window\.location\.href = "([^"]+)"', response.text).group(1)
    url = urllib.parse.urlsplit(target)
    assert url.query == ""
    return {k: v[0] for k, v in urllib.parse.parse_qs(url.fragment).items()}


def test_launch_creates_new_user_and_course(db):
    client = TestClient(app)
    response = _launch(client, _info())

    assert response.status_code == 200
    fragment = _fragment(response)
    # Only a one-time code travels in the URL: no session token, no address, no name.
    assert set(fragment) == {"lti", "code"}
    assert "New Student" not in response.text and "new-student@example.com" not in response.text
    assert "lti13_binding" in response.cookies

    user = db.query(User).filter(User.email == "new-student@example.com").one()
    assert (user.role, user.lti_role) == (UserRole.STUDENT, UserRole.STUDENT)
    assert (user.lti_issuer, user.lti_subject) == (ISS, "moodle-user-1")
    course = db.query(Course).filter(Course.name == "Course 101").one()
    assert (course.lti_issuer, course.lti_context_id) == (ISS, "ctx-101")
    assert user.courseId == course.courseId


def test_launch_promotes_existing_student_to_teacher(db):
    existing = User(
        email="promote-me@example.com",
        username="promote-me@example.com",
        role=UserRole.STUDENT,
    )
    db.add(existing)
    db.commit()

    response = _launch(TestClient(app), _info(email="Promote-Me@example.com", role="instructor"))

    assert response.status_code == 200
    db.refresh(existing)
    assert existing.role == UserRole.TEACHER
    # The first launch of the identity links it to the account of its address.
    assert (existing.lti_issuer, existing.lti_subject) == (ISS, "moodle-user-1")


def test_a_learner_launch_ends_the_teacher_role_of_an_earlier_launch(db):
    """.github#7 F.5 / review S8: the role is re-evaluated per launch, not a high-water mark."""
    _launch(TestClient(app), _info(role="instructor"))
    _launch(TestClient(app), _info(role="student"))

    user = db.query(User).one()
    assert user.role == UserRole.STUDENT


def test_a_keycloak_teacher_stays_a_teacher_after_a_learner_launch(db):
    from app.utils.keycloak_auth import sync_user_from_keycloak

    sync_user_from_keycloak(
        db, {"id": "kc-1", "email": "new-student@example.com", "username": "t", "roles": ["teacher"]}
    )
    _launch(TestClient(app), _info(role="student"))

    assert db.query(User).one().role == UserRole.TEACHER


def test_the_identity_decides_not_the_address(db):
    """Review S9: a platform account that later carries somebody else's address does not get their account."""
    victim = User(
        email="victim@example.com",
        username="victim",
        role=UserRole.TEACHER,
        lti_issuer=ISS,
        lti_subject="victim-sub",
    )
    db.add(victim)
    db.commit()

    # Another platform account claims the victim's address: refused.
    response = _launch(TestClient(app), _info(subject="attacker-sub", email="victim@example.com"))
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "lti_identity_mismatch"

    # The attacker's own (linked) account changes its address to the victim's: still the attacker's account.
    _launch(TestClient(app), _info(subject="attacker-sub", email="attacker@example.com"))
    response = _launch(TestClient(app), _info(subject="attacker-sub", email="victim@example.com"))
    assert response.status_code == 200
    attacker = db.query(User).filter(User.lti_subject == "attacker-sub").one()
    assert attacker.email == "attacker@example.com"
    db.refresh(victim)
    assert (victim.email, victim.lti_subject) == ("victim@example.com", "victim-sub")


def test_an_admin_account_is_never_linked_by_address(db):
    admin = User(email="admin@example.com", username="admin", role=UserRole.ADMIN, granted_role=UserRole.ADMIN)
    db.add(admin)
    db.commit()

    response = _launch(TestClient(app), _info(email="admin@example.com"))

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "lti_admin_not_linked"
    db.refresh(admin)
    assert admin.lti_subject is None and admin.role == UserRole.ADMIN


def test_courses_are_matched_by_their_context_not_their_title(db):
    _launch(TestClient(app), _info(subject="a", email="a@example.com", context_id="ctx-1", course="Mathe"))
    _launch(TestClient(app), _info(subject="b", email="b@example.com", context_id="ctx-2", course="Mathe"))
    assert db.query(Course).filter(Course.name == "Mathe").count() == 2

    # Renamed in Moodle: the same course, with the new name.
    _launch(TestClient(app), _info(subject="a", email="a@example.com", context_id="ctx-1", course="Mathe I"))
    course = db.query(Course).filter(Course.lti_context_id == "ctx-1").one()
    assert course.name == "Mathe I"
    assert db.query(User).filter(User.email == "a@example.com").one().courseId == course.courseId


def test_a_course_of_before_is_adopted_once_by_its_title(db):
    legacy = Course(name="Course 101")
    db.add(legacy)
    db.commit()

    _launch(TestClient(app), _info())

    db.refresh(legacy)
    assert db.query(Course).count() == 1
    assert legacy.lti_context_id == "ctx-101"


def test_the_code_signs_in_once_with_its_binding_cookie(db):
    client = TestClient(app)
    code = _fragment(_launch(client, _info()))["code"]

    with patch.object(settings, "LTI13_SESSION_SECRET", "s" * 40):
        first = client.post("/lti13/session", json={"code": code})
        assert first.status_code == 200, first.text
        body = first.json()
        assert (body["email"], body["role"]) == ("new-student@example.com", "student")
        me = TestClient(app).get("/users/me", headers={"Authorization": f"Bearer {body['session_token']}"})
        again = client.post("/lti13/session", json={"code": code})

    assert me.status_code == 200 and me.json()["email"] == "new-student@example.com"
    assert again.status_code == 400
    assert again.json()["detail"]["code"] == "lti_code_invalid"


def test_somebody_elses_code_signs_nobody_in(db):
    """Review S19: a crafted link with the attacker's code is useless in the victim's browser."""
    from app.models import LtiLoginCode

    code = _fragment(_launch(TestClient(app), _info()))["code"]

    victim_browser = TestClient(app)
    response = victim_browser.post("/lti13/session", json={"code": code})

    assert response.status_code == 400
    assert db.query(LtiLoginCode).count() == 0  # used up by the attempt


def test_an_expired_code_is_refused(db):
    from datetime import UTC, datetime, timedelta

    from app.models import LtiLoginCode

    client = TestClient(app)
    code = _fragment(_launch(client, _info()))["code"]
    db.query(LtiLoginCode).update({"created_at": datetime.now(UTC) - timedelta(minutes=5)})
    db.commit()

    assert client.post("/lti13/session", json={"code": code}).status_code == 400


def test_a_launch_without_sub_is_refused(db):
    response = _launch(TestClient(app), _info(subject=""))
    assert response.status_code == 400
    assert db.query(User).count() == 0


def test_launch_rejects_state_mismatch():
    client = TestClient(app)
    with patch.object(settings, "LTI13_CLIENT_ID", "client-123"):
        _do_login(client)
        response = client.post(
            "/lti13/launch",
            data={"id_token": "fake-token", "state": "not-the-real-state"},
        )
    assert response.status_code == 400


def test_login_and_launch_work_across_api_processes(db):
    """The state/nonce pair is in the database: a launch reaching another API
    process or replica than its login (``uvicorn --workers``, k8s replicas)
    finds it, and it serves exactly one launch."""
    from app.models import LtiNonce

    login_client = TestClient(app)
    with patch.object(settings, "LTI13_CLIENT_ID", "client-123"):
        state = _do_login(login_client)
    assert db.query(LtiNonce).filter(LtiNonce.state == state).count() == 1

    launch_client = TestClient(app)  # stands in for another process: no shared memory needed
    launch_client.cookies.set("lti13_state", state)
    user_info = _info(email="elsewhere@example.com", context_id="", course="")
    with (
        patch.object(settings, "LTI13_CLIENT_ID", "client-123"),
        patch("app.routers.lti13.lti13_service.validate_id_token", return_value={}),
        patch("app.routers.lti13.lti13_service.extract_user_info", return_value=user_info),
    ):
        first = launch_client.post("/lti13/launch", data={"id_token": "fake-token", "state": state})
        replay = launch_client.post("/lti13/launch", data={"id_token": "fake-token", "state": state})

    assert first.status_code == 200
    assert replay.status_code == 400
    assert db.query(LtiNonce).count() == 0


def test_expired_state_is_refused(db):
    from datetime import UTC, datetime, timedelta

    from app.models import LtiNonce

    db.add(LtiNonce(state="old-state", nonce="old-nonce", created_at=datetime.now(UTC) - timedelta(minutes=10)))
    db.commit()
    client = TestClient(app)
    client.cookies.set("lti13_state", "old-state")
    with patch.object(settings, "LTI13_CLIENT_ID", "client-123"):
        response = client.post("/lti13/launch", data={"id_token": "fake-token", "state": "old-state"})
    assert response.status_code == 400
    assert db.query(LtiNonce).count() == 0


def test_login_operations_have_their_own_ids():
    """One route for GET and POST gave both the same operationId, which broke
    the generated frontend types."""
    ops = app.openapi()["paths"]["/lti13/login"]
    assert ops["get"]["operationId"] != ops["post"]["operationId"]
