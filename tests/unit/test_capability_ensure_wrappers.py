"""Regression tests for the ``ensure_*`` capability wrappers.

This module is the **regression guarantee** for two security bugs:

* Bug #1 — Privilege-Escalation: ``ensure_operate_deployment`` and
  ``ensure_view_deployment_owner`` must not let unrelated users
  pause/destroy or inspect-only foreign deployments.
* Bug #2 — Teacher-Bypass: ``ensure_edit_app`` / ``ensure_delete_app``
  / ``ensure_submit_app_version`` must reject teachers acting on
  apps they do not own.

Where the sibling :mod:`test_capabilities` module verifies the
``can_*`` booleans, this module exercises the ``ensure_*`` wrappers
end-to-end: they must raise :class:`fastapi.HTTPException` with the
documented ``status_code=403`` and a structured ``detail["code"]``
that the frontend pattern-matches on. Without these tests the bugs
could silently come back by way of a refactor flipping a boolean.

DB-less by design — ``conftest.py`` in this directory disables the
parent autouse Postgres fixture, so each test uses
:class:`unittest.mock.MagicMock` for any DB session it needs.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.models import UserRole
from app.utils import capabilities as caps

pytestmark = pytest.mark.unit


# ----------------------------------------------------------------
# Fixtures — mirror tests/unit/test_capabilities.py
# ----------------------------------------------------------------
def _user(role: UserRole, user_id: uuid.UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(userId=user_id or uuid.uuid4(), role=role)


def _app(owner_id: uuid.UUID, is_private: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        appId=uuid.uuid4(),
        userId=owner_id,
        is_private=is_private,
    )


def _deployment(owner_id: uuid.UUID, course_id: uuid.UUID | None = None,
                owner_role: UserRole = UserRole.STUDENT) -> SimpleNamespace:
    return SimpleNamespace(
        deploymentId=uuid.uuid4(),
        userId=owner_id,
        user=SimpleNamespace(courseId=course_id, role=owner_role),
    )


def _course() -> SimpleNamespace:
    return SimpleNamespace(courseId=uuid.uuid4(), name="X")


@pytest.fixture
def student():
    return _user(UserRole.STUDENT)


@pytest.fixture
def teacher():
    return _user(UserRole.TEACHER)


@pytest.fixture
def admin():
    return _user(UserRole.ADMIN)


@pytest.fixture
def db_mock():
    return MagicMock()


# ================================================================
# APPS — ensure_edit_app / ensure_delete_app / ensure_submit_app_version
# Bug #2 regression: teacher acting on a foreign app must be rejected.
# ================================================================
def test_ensure_edit_app_raises_for_foreign_teacher(teacher):
    """Bug #2 regression: a teacher must not be able to edit a foreign app."""
    app = _app(uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        caps.ensure_edit_app(teacher, app)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "app_edit_forbidden"


def test_ensure_edit_app_passes_for_owner(teacher):
    """The owner of the app may always edit — no raise."""
    app = _app(teacher.userId)
    # Must not raise.
    caps.ensure_edit_app(teacher, app)


def test_ensure_edit_app_passes_for_admin(admin):
    """Admins keep their platform-wide override — no raise even on foreign apps."""
    app = _app(uuid.uuid4())
    caps.ensure_edit_app(admin, app)


def test_ensure_delete_app_raises_for_student(student):
    """A student who is not the owner cannot delete an app."""
    app = _app(uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        caps.ensure_delete_app(student, app)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "app_delete_forbidden"


def test_ensure_submit_app_version_raises_for_foreign_teacher(teacher):
    """Bug #2 regression: teacher cannot submit a version on a foreign app."""
    app = _app(uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        caps.ensure_submit_app_version(teacher, app)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "app_submit_forbidden"


# ================================================================
# DEPLOYMENTS — ensure_view_deployment_member
# ================================================================
def test_ensure_view_deployment_member_raises_for_unrelated_student(student):
    """A student that is not owner / team-member / direct-mapped is rejected."""
    dep = _deployment(uuid.uuid4())
    db = MagicMock()
    # has_deployment_access falls through to the team/direct lookups,
    # both return None → False → ensure_* raises.
    db.query.return_value.join.return_value.filter.return_value.first.return_value = None
    db.query.return_value.filter.return_value.first.return_value = None
    with pytest.raises(HTTPException) as exc:
        caps.ensure_view_deployment_member(student, dep, db)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "deployment_view_forbidden"


def test_ensure_view_deployment_member_passes_for_team_member(student, db_mock):
    """The deployment owner counts as team-member — no raise."""
    dep = _deployment(student.userId)
    # Owner of the deployment short-circuits has_deployment_access.
    caps.ensure_view_deployment_member(student, dep, db_mock)


# ================================================================
# DEPLOYMENTS — ensure_view_deployment_owner
# Bug #1 regression: only owner / admin may inspect.
# ================================================================
def test_ensure_view_deployment_owner_raises_for_non_owner_student(student):
    """A student who is not the owner cannot reach the owner-view."""
    dep = _deployment(uuid.uuid4(), course_id=uuid.uuid4())
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    with pytest.raises(HTTPException) as exc:
        caps.ensure_view_deployment_owner(student, dep, db)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "deployment_owner_view_forbidden"


@pytest.mark.parametrize("owner_role", [UserRole.STUDENT, UserRole.TEACHER])
def test_ensure_view_deployment_owner_refuses_a_course_teacher(teacher, owner_role):
    """Course-teachers have no inspect right (only teachers and admins
    deploy; .github#7 F): a matching course_teachers row opens nothing."""
    course_id = uuid.uuid4()
    dep = _deployment(uuid.uuid4(), course_id=course_id, owner_role=owner_role)
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = (
        SimpleNamespace(courseId=course_id, userId=teacher.userId)
    )
    with pytest.raises(HTTPException) as exc:
        caps.ensure_view_deployment_owner(teacher, dep, db)
    assert exc.value.status_code == 403


# ================================================================
# DEPLOYMENTS — ensure_operate_deployment
# Bug #1 regression: team-membership does NOT confer operate rights.
# ================================================================
def test_ensure_operate_deployment_raises_for_member_non_owner(student, db_mock):
    """Bug #1 regression: a team-member who is not the owner cannot
    pause / destroy / redeploy the deployment.
    """
    # Student is not the owner — the can_operate_deployment gate is
    # strictly owner-or-admin, so even a happy DB doesn't help.
    dep = _deployment(uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        caps.ensure_operate_deployment(student, dep, db_mock)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "deployment_operate_forbidden"


def test_ensure_operate_deployment_passes_for_owner(student, db_mock):
    """The owner may operate the deployment — no raise."""
    dep = _deployment(student.userId)
    caps.ensure_operate_deployment(student, dep, db_mock)


# ================================================================
# COURSES — ensure_edit_course
# ================================================================
def test_ensure_edit_course_raises_for_non_assigned_teacher(teacher):
    """Phase 3 regression: a teacher who is NOT a designated teacher of
    THIS course gets rejected — the staff-blanket bypass is gone.
    """
    course = _course()
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    with pytest.raises(HTTPException) as exc:
        caps.ensure_edit_course(teacher, course, db)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "course_edit_forbidden"


def test_ensure_edit_course_passes_for_assigned_teacher(teacher):
    """A teacher with a matching ``course_teachers`` row may edit — no raise."""
    course = _course()
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = (
        SimpleNamespace(courseId=course.courseId, userId=teacher.userId)
    )
    caps.ensure_edit_course(teacher, course, db)


# ================================================================
# USERS — ensure_view_user
# ================================================================
def test_ensure_view_user_raises_for_student_viewing_other(student):
    """A student may only read their own profile via this gate."""
    other_id = uuid.uuid4()
    with pytest.raises(HTTPException) as exc:
        caps.ensure_view_user(student, other_id)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "user_view_forbidden"
