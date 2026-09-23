"""Role monotonicity (DB-less part): the privilege ordering helper.

The DB-backed behaviour (Keycloak sync / LTI launch never demoting) lives in
``tests/test_role_monotonic.py`` — the unit suite here is deliberately DB-less.
"""
import pytest

from app.models import UserRole
from app.utils.keycloak_auth import higher_role


@pytest.mark.unit
def test_higher_role_ordering():
    assert higher_role(UserRole.STUDENT, UserRole.TEACHER) == UserRole.TEACHER
    assert higher_role(UserRole.TEACHER, UserRole.STUDENT) == UserRole.TEACHER
    assert higher_role(UserRole.TEACHER, UserRole.ADMIN) == UserRole.ADMIN
    assert higher_role(UserRole.ADMIN, UserRole.STUDENT) == UserRole.ADMIN
    assert higher_role(UserRole.STUDENT, UserRole.STUDENT) == UserRole.STUDENT
