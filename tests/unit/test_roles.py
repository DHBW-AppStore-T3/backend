"""The effective role is the highest of what Keycloak, LTI and an admin said last (app/utils/roles.py, review S8)."""

import pytest

from app.models import User, UserRole
from app.utils import roles

pytestmark = pytest.mark.unit

S, T, A = UserRole.STUDENT, UserRole.TEACHER, UserRole.ADMIN


def _user(keycloak=None, lti=None, granted=None, role=S):
    return User(
        email="x@dhbw.de",
        username="x",
        role=role,
        keycloak_role=keycloak,
        lti_role=lti,
        granted_role=granted,
    )


@pytest.mark.parametrize(
    ("keycloak", "lti", "granted", "expected"),
    [
        (None, None, None, S),
        (S, None, None, S),
        (S, T, None, T),  # a real teacher: no realm role, teacher by their Moodle launch
        (A, S, None, A),
        (S, S, T, T),
        (None, None, A, A),
    ],
)
def test_the_effective_role_is_the_highest_statement(keycloak, lti, granted, expected):
    user = _user(keycloak, lti, granted)
    roles.refresh(user)
    assert user.role == expected


def test_a_source_that_stops_asserting_takes_its_role_away():
    user = _user(keycloak=A, lti=S, role=A)

    user.keycloak_role = roles.from_keycloak([])  # admin removed in Keycloak

    assert roles.refresh(user) is True
    assert user.role == S


def test_keycloak_realm_roles():
    assert roles.from_keycloak(["admin", "teacher"]) == A
    assert roles.from_keycloak(["teacher", "offline_access"]) == T
    assert roles.from_keycloak(["default-roles-dhbw"]) == S


@pytest.mark.parametrize(
    ("lti_roles", "expected"),
    [
        (["http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor"], T),
        (["http://purl.imsglobal.org/vocab/lis/v2/institution/person#Administrator"], T),
        (["http://purl.imsglobal.org/vocab/lis/v2/membership#Learner"], S),
        ([], S),
    ],
)
def test_lti_never_makes_an_admin(lti_roles, expected):
    from app.services import lti13_service

    info = lti13_service.extract_user_info(
        {"https://purl.imsglobal.org/spec/lti/claim/roles": lti_roles}
    )
    assert roles.from_lti(info["role"]) == expected


def test_an_admin_lowering_a_role_takes_effect_now_and_caps_the_other_sources():
    user = _user(keycloak=A, lti=T, role=A)

    roles.grant(user, S)

    assert user.role == S
    assert (user.keycloak_role, user.lti_role, user.granted_role) == (S, S, S)


def test_an_admin_grant_stays_when_the_other_sources_say_less():
    user = _user(keycloak=S, lti=S)

    roles.grant(user, T)
    user.keycloak_role = roles.from_keycloak([])
    roles.refresh(user)

    assert user.role == T
