from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.config import settings
from app.utils.keycloak_auth import get_current_user_keycloak

pytestmark = pytest.mark.unit


def _credentials(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_rejects_missing_credentials():
    db = MagicMock()
    with pytest.raises(HTTPException) as exc_info:
        get_current_user_keycloak(credentials=None, db=db)
    assert exc_info.value.status_code == 401


def test_lti_token_valid_but_no_matching_user_falls_through_to_keycloak():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None

    with (
        patch.object(settings, "LTI13_SESSION_SECRET", "test-secret"),
        patch(
            "app.utils.keycloak_auth.lti13_service.verify_session_token",
            return_value="ghost@example.com",
        ),
        patch(
            "app.utils.keycloak_auth.verify_keycloak_token_offline",
            return_value={"sub": "kc-1", "email": "kc@example.com", "realm_access": {"roles": []}},
        ),
        patch("app.utils.keycloak_auth.sync_user_from_keycloak") as mock_sync,
    ):
        mock_sync.return_value = MagicMock(email="kc@example.com")
        result = get_current_user_keycloak(
            credentials=_credentials("lti-token-for-unknown-user"), db=db
        )

    assert result.email == "kc@example.com"


def test_skips_lti_check_when_secret_is_default():
    db = MagicMock()
    with (
        patch.object(settings, "LTI13_SESSION_SECRET", "change-me-in-production"),
        patch(
            "app.utils.keycloak_auth.verify_keycloak_token_offline",
            return_value={"sub": "kc-1", "email": "kc@example.com", "realm_access": {"roles": []}},
        ),
        patch("app.utils.keycloak_auth.sync_user_from_keycloak") as mock_sync,
    ):
        mock_sync.return_value = MagicMock(email="kc@example.com")
        result = get_current_user_keycloak(credentials=_credentials("some-token"), db=db)

    assert result.email == "kc@example.com"


def test_rejects_keycloak_token_missing_sub():
    db = MagicMock()
    with (
        patch.object(settings, "LTI13_SESSION_SECRET", "change-me-in-production"),
        patch(
            "app.utils.keycloak_auth.verify_keycloak_token_offline",
            return_value={"email": "no-sub@example.com"},
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        get_current_user_keycloak(credentials=_credentials("some-token"), db=db)

    assert exc_info.value.status_code == 401
