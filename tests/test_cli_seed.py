"""``python -m app.cli seed``: demo data through the CRUD/service layer, idempotent, without HTTP or login."""

import pytest
import yaml

from app import cli
from app.models import (
    App,
    AppVersionApproval,
    AppVersionApprovalStatus,
    Course,
    CourseTeacher,
    User,
    UserRole,
)
from tests.test_pod_apps import SPEC

pytestmark = pytest.mark.integration

CFG = {
    "courseName": "Demo",
    "apps": [{"name": "ide", "gitLink": "https://github.com/example/ide", "versions": ["v2.0.0"]}],
}


def _realm():
    return [
        {"id": "kc-admin", "username": "admin", "email": "admin@dhbw.de", "roles": ["admin"]},
        {"id": "kc-teach", "username": "dozent", "email": "dozent@dhbw.de", "roles": ["teacher"]},
        {"id": "kc-stud", "username": "student1", "email": "student1@dhbw.de", "roles": ["student"]},
    ]


@pytest.fixture
def pod_repo(fake_git):
    from app.routers import apps as apps_router

    fake_git.files["appstore.yaml"] = yaml.safe_dump(SPEC)
    apps_router.clear_variable_cache()


def test_seed_provisions_users_course_and_approved_apps(db, pod_repo):
    cli.seed(db, CFG, _realm())

    assert {u.username: u.role for u in db.query(User)} == {
        "admin": UserRole.ADMIN, "dozent": UserRole.TEACHER, "student1": UserRole.STUDENT}
    course = db.query(Course).one()
    assert db.query(CourseTeacher).filter_by(courseId=course.courseId).count() == 1
    assert db.query(User).filter_by(username="student1").one().courseId == course.courseId
    app = db.query(App).one()
    assert app.is_private is False and db.get(User, app.userId).username == "admin"
    approval = db.query(AppVersionApproval).one()
    assert approval.status == AppVersionApprovalStatus.APPROVED
    assert approval.runtime == "kubernetes" and approval.spec_sha256 and approval.commit_sha


def test_seed_is_idempotent(db, pod_repo):
    cli.seed(db, CFG, _realm())
    cli.seed(db, CFG, _realm())

    assert db.query(User).count() == 3
    assert db.query(Course).count() == 1
    assert db.query(CourseTeacher).count() == 1
    assert db.query(App).count() == 1
    assert db.query(AppVersionApproval).count() == 1


def test_seed_does_not_touch_existing_data(db, pod_repo):
    cli.seed(db, CFG, _realm())
    app = db.query(App).one()
    app.description = "edited by hand"
    db.commit()

    cli.seed(db, CFG, _realm())

    assert db.query(App).one().description == "edited by hand"


def test_seed_skips_apps_whose_repository_is_not_allowed(db, pod_repo):
    cfg = {"apps": [{"name": "evil", "gitLink": "https://evil.example.org/x/y", "versions": ["v1"]}]}

    cli.seed(db, cfg, _realm())

    assert db.query(App).count() == 0


def test_seed_without_admin_skips_apps(db, pod_repo):
    cli.seed(db, CFG, [u for u in _realm() if u["username"] != "admin"])

    assert db.query(App).count() == 0
    assert db.query(Course).count() == 1


def test_cli_has_no_http_surface():
    from app.main import app as api

    assert not [r for r in api.routes if "seed" in getattr(r, "path", "")]
