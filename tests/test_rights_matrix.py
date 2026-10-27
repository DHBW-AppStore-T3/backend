"""Who may do what, by role and ownership (.github#7 F, security review H-1, M-2).

The capability functions have their own unit tests; these go through the
HTTP routes, so a route that forgets its gate fails here.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app as fastapi_app
from app.models import Course, CourseTeacher, Deployment, User, UserRole
from app.utils.keycloak_auth import get_current_user_keycloak
from tests.conftest import TestingSessionLocal, create_app_in_db

pytestmark = pytest.mark.integration


def _user(db, role, *, course=None):
    user = User(userId=uuid.uuid4(), keycloak_id=f"kc-{uuid.uuid4()}", email=f"{uuid.uuid4().hex[:8]}@dhbw.de",
                username="u", role=role, courseId=course.courseId if course else None)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _course(db, *, teachers=()):
    course = Course(courseId=uuid.uuid4(), name=f"C-{uuid.uuid4().hex[:4]}")
    db.add(course)
    db.flush()
    for t in teachers:
        db.add(CourseTeacher(courseId=course.courseId, userId=t.userId))
    db.commit()
    return course


def _as(user):
    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    fastapi_app.dependency_overrides[get_current_user_keycloak] = lambda: user
    fastapi_app.dependency_overrides[get_db] = override_get_db
    return TestClient(fastapi_app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    fastapi_app.dependency_overrides.clear()


def test_students_register_no_apps(db):
    student = _user(db, UserRole.STUDENT)
    resp = _as(student).post("/apps/", json={"name": "x", "git_link": "https://github.com/acme/x"})
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "role_required"


def test_students_do_not_deploy_even_their_own_private_app(db, queued):
    student = _user(db, UserRole.STUDENT)
    app = create_app_in_db(db, student, is_private=True)
    resp = _as(student).post("/deployments/", json={
        "name": "x", "appId": str(app.appId), "releaseTag": "v1", "teams": [],
    })
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "role_required"
    assert queued() == []
    assert db.query(Deployment).count() == 0


def test_teams_in_a_deployment_request_are_validated(db):
    teacher = _user(db, UserRole.TEACHER)
    student = _user(db, UserRole.STUDENT)
    app = create_app_in_db(db, teacher, is_private=True)
    client = _as(teacher)

    def deploy(teams):
        return client.post("/deployments/", json={
            "name": "x", "appId": str(app.appId), "releaseTag": "v1", "teams": teams,
        })

    unknown = deploy([{"name": "A", "userIds": [str(uuid.uuid4())]}])
    twice = deploy([{"name": "A", "userIds": [str(student.userId)]}, {"name": "B", "userIds": [str(student.userId)]}])
    same_name = deploy([{"name": "A", "userIds": []}, {"name": "A", "userIds": []}])

    assert unknown.status_code == 422 and unknown.json()["detail"]["reason"] == "unknown_team_member"
    assert twice.status_code == 422 and twice.json()["detail"]["reason"] == "member_in_several_teams"
    assert same_name.status_code == 422 and same_name.json()["detail"]["reason"] == "invalid_teams"


def test_teachers_no_longer_open_every_deployment(db):
    owner = _user(db, UserRole.TEACHER)
    other_teacher = _user(db, UserRole.TEACHER)
    admin = _user(db, UserRole.ADMIN)
    app = create_app_in_db(db, owner, is_private=True)
    dep = Deployment(deploymentId=uuid.uuid4(), name="d", appId=app.appId, userId=owner.userId)
    db.add(dep)
    db.commit()

    assert _as(other_teacher).get(f"/deployments/{dep.deploymentId}").status_code == 403
    assert _as(other_teacher).get(f"/deployments/{dep.deploymentId}/my-access").status_code == 403
    assert _as(admin).get(f"/deployments/{dep.deploymentId}").status_code == 200
    assert _as(owner).get(f"/deployments/{dep.deploymentId}").status_code == 200


def test_course_teacher_inspects_no_deployment_at_all(db):
    """The inspect right of course-teachers is gone: only teachers and
    admins deploy, so it covered nothing, and nothing led to a deployment."""
    course_teacher = _user(db, UserRole.TEACHER)
    course = _course(db, teachers=[course_teacher])
    student = _user(db, UserRole.STUDENT, course=course)
    colleague = _user(db, UserRole.TEACHER, course=course)
    deps = {}
    for owner in (student, colleague):
        app = create_app_in_db(db, owner, is_private=True)
        dep = Deployment(deploymentId=uuid.uuid4(), name="d", appId=app.appId, userId=owner.userId)
        db.add(dep)
        db.commit()
        deps[owner.userId] = dep.deploymentId

    client = _as(course_teacher)
    for owner in (student, colleague):
        assert client.get(f"/deployments/{deps[owner.userId]}").status_code == 403
        assert client.get(f"/deployments/{deps[owner.userId]}/resources").status_code == 403
        assert client.get(f"/tasks/deployment/{deps[owner.userId]}").status_code == 403


def test_course_roster_reads_are_for_staff_only(db):
    teacher = _user(db, UserRole.TEACHER)
    student = _user(db, UserRole.STUDENT)
    course = _course(db)

    assert _as(student).get(f"/courses/{course.courseId}").status_code == 403
    assert _as(teacher).get(f"/courses/{course.courseId}").status_code == 200


def test_nobody_pulls_users_out_of_a_course_they_do_not_manage(db):
    me = _user(db, UserRole.TEACHER)
    mine = _course(db, teachers=[me])
    theirs = _course(db, teachers=[_user(db, UserRole.TEACHER)])
    victim = _user(db, UserRole.TEACHER, course=theirs)
    newcomer = _user(db, UserRole.STUDENT)
    client = _as(me)

    stolen = client.post(f"/courses/{mine.courseId}/users", json={"userIds": [str(victim.userId)]})
    joined = client.post(f"/courses/{mine.courseId}/users", json={"userIds": [str(newcomer.userId)]})
    foreign = client.post(f"/courses/{theirs.courseId}/users", json={"userIds": [str(newcomer.userId)]})
    removal = client.delete(f"/courses/{theirs.courseId}/users/{victim.userId}")

    assert stolen.status_code == 403
    assert joined.status_code == 200
    assert foreign.status_code == 403
    assert removal.status_code == 403
    db.expire_all()
    assert db.get(User, victim.userId).courseId == theirs.courseId
