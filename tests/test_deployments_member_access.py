"""Integration tests for GET /deployments/{id}/members/{user_id}/access.

The owner of a deployment (and an admin) can reveal a team member's
credentials, e.g. to answer a question from a student. It is the only
path that hands a member's password to somebody else (the owner view's
outputs stay redacted, ``my-access`` only ever returns the caller's own
entry), so the access matrix is the point of these tests: operate right
only, never the wider owner view.
"""

import json
import uuid

import pytest

from app.models import Course, CourseTeacher, User, UserRole
from tests.conftest import _make_client
from tests.test_deployments_my_access import _outputs_for, _seed_deployment_with_outputs

pytestmark = pytest.mark.integration


def _url(deployment, user):
    return f"/deployments/{deployment.deploymentId}/members/{user.userId}/access"


def _teacher(db, **fields):
    user = User(
        userId=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4()}",
        email=f"{uuid.uuid4().hex[:8]}@dhbw.de",
        username="other",
        role=UserRole.TEACHER,
        **fields,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def two_member_deployment(db, mock_user, mock_student):
    """Owner ``mock_user`` (teacher); the student and a classmate share Team-1."""
    classmate = User(
        userId=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4()}",
        email="mate@dhbw.de",
        username="mate",
        role=UserRole.STUDENT,
    )
    db.add(classmate)
    db.commit()
    outputs = _outputs_for(
        "Team-1",
        ("student", "student", "pw-student"),
        ("mate", "mate", "pw-mate-SECRET"),
    )
    deployment, _team = _seed_deployment_with_outputs(
        db,
        owner=mock_user,
        members=[mock_student, classmate],
        outputs=outputs,
    )
    return deployment, classmate


def test_owner_reveals_a_members_own_account_only(client, mock_student, two_member_deployment):
    deployment, _classmate = two_member_deployment

    response = client.get(_url(deployment, mock_student))

    assert response.status_code == 200, response.text
    body = response.json()
    assert list(body["user_accounts"]) == ["Team-1-student"]
    assert body["user_accounts"]["Team-1-student"]["auth"] == "pw-student"
    assert "pw-mate-SECRET" not in json.dumps(body)
    assert "Team-1" in body["team_vms"]


def test_the_response_is_never_cached(client, mock_student, two_member_deployment):
    deployment, _classmate = two_member_deployment

    response = client.get(_url(deployment, mock_student))

    assert response.headers["cache-control"] == "no-store"


def test_admin_reveals(admin_client, mock_student, two_member_deployment):
    deployment, _classmate = two_member_deployment

    response = admin_client.get(_url(deployment, mock_student))

    assert response.status_code == 200, response.text
    assert response.json()["user_accounts"]["Team-1-student"]["auth"] == "pw-student"


def test_another_teacher_gets_403(db, mock_student, two_member_deployment):
    deployment, _classmate = two_member_deployment
    stranger = _teacher(db)

    response = _make_client(stranger).get(_url(deployment, mock_student))

    assert response.status_code == 403


def test_a_team_member_cannot_reveal_a_classmate(db, mock_student, two_member_deployment):
    deployment, classmate = two_member_deployment

    response = _make_client(mock_student).get(_url(deployment, classmate))

    assert response.status_code == 403
    assert "pw-mate-SECRET" not in response.text


def test_a_course_teacher_of_a_student_owned_deployment_gets_403(db, mock_student):
    """Inspecting is not operating: no reveal, and (since the course-teacher
    path is gone) not even the owner view."""
    course = Course(courseId=uuid.uuid4(), name="C-1")
    db.add(course)
    db.flush()
    course_teacher = _teacher(db)
    db.add(CourseTeacher(courseId=course.courseId, userId=course_teacher.userId))
    mock_student.courseId = course.courseId
    db.commit()
    outputs = _outputs_for("Team-1", ("student", "student", "pw-student"))
    deployment, _team = _seed_deployment_with_outputs(
        db,
        owner=mock_student,
        members=[mock_student],
        outputs=outputs,
    )

    response = _make_client(course_teacher).get(_url(deployment, mock_student))

    assert response.status_code == 403


def test_a_user_outside_the_teams_gets_empty_maps(client, db, two_member_deployment):
    deployment, _classmate = two_member_deployment
    outsider = _teacher(db)

    response = client.get(_url(deployment, outsider))

    assert response.status_code == 200, response.text
    assert response.json()["user_accounts"] == {}


def test_unknown_deployment_is_404(client, mock_student):
    response = client.get(f"/deployments/{uuid.uuid4()}/members/{mock_student.userId}/access")

    assert response.status_code == 404
