import pytest

from app.models import UserRole
from app.utils.keycloak_auth import higher_role

pytestmark = pytest.mark.unit


def test_higher_role_never_demotes():
    assert higher_role(UserRole.TEACHER, UserRole.STUDENT) == UserRole.TEACHER
    assert higher_role(UserRole.ADMIN, UserRole.STUDENT) == UserRole.ADMIN
    assert higher_role(UserRole.ADMIN, UserRole.TEACHER) == UserRole.ADMIN


def test_higher_role_promotes_student_to_teacher():
    assert higher_role(UserRole.STUDENT, UserRole.TEACHER) == UserRole.TEACHER


def test_higher_role_same_role_is_a_no_op():
    assert higher_role(UserRole.STUDENT, UserRole.STUDENT) == UserRole.STUDENT
