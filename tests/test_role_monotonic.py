"""Role monotonicity (DB-backed): no login path demotes a user's role.

Real (non-demo) users carry a role only in Moodle — Keycloak federates them
from bwIDM without a realm role. So a self-service/Keycloak login maps them to
STUDENT, and without the monotonic guard it would undo the TEACHER role Moodle
assigned via LTI. Moodle is the authoritative source; every other path may
only raise the role.
"""
import uuid

import pytest

from app.models import User, UserRole
from app.utils import keycloak_auth


@pytest.mark.integration
def test_keycloak_sync_does_not_demote_teacher(db):
    """A federated user with no realm role must not lose a Moodle-set TEACHER."""
    user = User(
        userId=uuid.uuid4(),
        keycloak_id="kc-teacher",
        email="dennis.pfisterer@dhbw.de",
        username="dennis.pfisterer",
        role=UserRole.TEACHER,
    )
    db.add(user)
    db.commit()

    # Self-service login: Keycloak claims carry no realm role → maps to STUDENT.
    keycloak_auth.sync_user_from_keycloak(
        db,
        {
            "id": "kc-teacher",
            "email": "dennis.pfisterer@dhbw.de",
            "username": "dennis.pfisterer",
            "roles": [],
        },
    )

    db.refresh(user)
    assert user.role == UserRole.TEACHER


@pytest.mark.integration
def test_keycloak_sync_promotes_to_admin(db):
    """An explicit admin realm role must still raise the role."""
    user = User(
        userId=uuid.uuid4(),
        keycloak_id="kc-promote",
        email="promote@dhbw.de",
        username="promote",
        role=UserRole.TEACHER,
    )
    db.add(user)
    db.commit()

    keycloak_auth.sync_user_from_keycloak(
        db,
        {
            "id": "kc-promote",
            "email": "promote@dhbw.de",
            "username": "promote",
            "roles": ["admin"],
        },
    )

    db.refresh(user)
    assert user.role == UserRole.ADMIN
