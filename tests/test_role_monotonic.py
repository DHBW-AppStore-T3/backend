from unittest.mock import patch

from app.config import settings
from app.models import User, UserRole
from app.utils.keycloak_auth import sync_user_from_keycloak


def test_moodle_teacher_role_survives_later_keycloak_login(db):
    """A TEACHER role assigned via LTI must not be reset to STUDENT by a
    later Keycloak login — Keycloak-federated users carry no realm role,
    so a naive sync would otherwise demote them on every visit."""
    user = User(
        keycloak_id="kc-1",
        email="teacher@example.com",
        username="teacher@example.com",
        role=UserRole.TEACHER,
    )
    db.add(user)
    db.commit()

    with patch.object(settings, "LTI13_SESSION_SECRET", "test-secret"):
        updated = sync_user_from_keycloak(
            db,
            {
                "id": "kc-1",
                "email": "teacher@example.com",
                "username": "teacher@example.com",
                "roles": [],
            },
        )

    assert updated.role == UserRole.TEACHER
