"""Where a user's role comes from (review S8, .github#7 F.5).

Three sources assert a role, and each one's latest statement is kept:

  ``keycloak_role``  the realm roles of the latest Keycloak token
                     (``admin`` > ``teacher`` > anything else: student)
  ``lti_role``       the role of the latest LTI launch (an Instructor is a
                     teacher, everyone else a student; LTI never makes an admin)
  ``granted_role``   an admin's grant (``PUT /users/{id}``)

The effective ``users.role`` is the highest of them. Each source overwrites
only its own value, so a source that stops asserting a role takes it away the
next time it speaks: remove ``admin`` in Keycloak and the user's next request
is no admin's any more; a launch as a Learner ends the teacher role an earlier
launch gave. The stored role used to be a high-water mark that only went up.

Real users carry no realm role (Keycloak federates them from bwIDM), so a
Keycloak token says "student" for them; their teacher role comes from their
latest Moodle launch, which is why the sources are kept apart.
"""

from __future__ import annotations

from app.models import User, UserRole

RANK = {UserRole.STUDENT: 0, UserRole.TEACHER: 1, UserRole.ADMIN: 2}


def highest(*roles: UserRole | None) -> UserRole:
    """The most privileged of ``roles`` (None: no statement), student without any."""
    present = [r for r in roles if r is not None]
    return max(present, key=RANK.__getitem__) if present else UserRole.STUDENT


def from_keycloak(realm_roles: list[str]) -> UserRole:
    """The role a Keycloak token's realm roles state: admin > teacher > student."""
    if "admin" in realm_roles:
        return UserRole.ADMIN
    if "teacher" in realm_roles:
        return UserRole.TEACHER
    return UserRole.STUDENT


def from_lti(lti_role: str) -> UserRole:
    """The role an LTI launch states: ``instructor`` is a teacher, nothing makes an admin."""
    return UserRole.TEACHER if lti_role == "instructor" else UserRole.STUDENT


def effective(user: User) -> UserRole:
    return highest(user.keycloak_role, user.lti_role, user.granted_role)


def refresh(user: User) -> bool:
    """Set ``user.role`` from its sources; whether it changed."""
    role = effective(user)
    if user.role == role:
        return False
    user.role = role
    return True


def grant(user: User, role: UserRole) -> None:
    """An admin sets ``user``'s role to ``role`` now (``PUT /users/{id}``).

    The grant is kept as a source of its own. What Keycloak and Moodle said
    is capped at ``role``, so lowering a role takes effect at once; each of
    them raises it again only when it next says so.
    """

    def capped(stated: UserRole | None) -> UserRole | None:
        return stated if stated is None or RANK[stated] <= RANK[role] else role

    user.keycloak_role, user.lti_role = capped(user.keycloak_role), capped(user.lti_role)
    user.granted_role = role
    refresh(user)
