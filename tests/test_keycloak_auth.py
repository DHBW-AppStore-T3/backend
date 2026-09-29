from unittest.mock import patch

from fastapi.security import HTTPAuthorizationCredentials

from app.config import settings
from app.models import User, UserRole
from app.services import lti13_service
from app.utils.keycloak_auth import get_current_user_keycloak


def _credentials(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_get_current_user_accepts_lti_session_token(db):
    user = User(
        email="lti-user@example.com",
        username="lti-user@example.com",
        role=UserRole.STUDENT,
    )
    db.add(user)
    db.commit()

    with patch.object(settings, "LTI13_SESSION_SECRET", "test-secret"):
        token = lti13_service.issue_session_token("lti-user@example.com", "test-secret")
        result = get_current_user_keycloak(credentials=_credentials(token), db=db)

    assert result.email == "lti-user@example.com"


def test_get_current_user_falls_through_to_keycloak_on_invalid_lti_token(db):
    with (
        patch.object(settings, "LTI13_SESSION_SECRET", "test-secret"),
        patch(
            "app.utils.keycloak_auth.verify_keycloak_token_offline",
            return_value={
                "sub": "keycloak-sub-1",
                "email": "kc-user@example.com",
                "preferred_username": "kc-user",
                "realm_access": {"roles": []},
            },
        ),
    ):
        result = get_current_user_keycloak(credentials=_credentials("not-a-valid-lti-token"), db=db)

    assert result.email == "kc-user@example.com"
