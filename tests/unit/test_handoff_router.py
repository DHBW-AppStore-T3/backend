from unittest.mock import patch

import pytest
from fastapi import HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials

from app.config import settings
from app.routers import handoff

pytestmark = pytest.mark.unit


def _credentials(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


async def test_mint_returns_token_and_email():
    with (
        patch.object(settings, "HANDOFF_SESSION_SECRET", "test-secret"),
        patch.object(
            handoff,
            "verify_keycloak_token",
            return_value={"active": True, "email": "a@example.com"},
        ),
    ):
        result = await handoff.mint_handoff_token(credentials=_credentials("valid-token"))

    assert result["email"] == "a@example.com"
    assert result["handoff_token"]


async def test_mint_raises_when_token_has_no_email():
    with (
        patch.object(handoff, "verify_keycloak_token", return_value={"active": True}),
        pytest.raises(HTTPException) as exc_info,
    ):
        await handoff.mint_handoff_token(credentials=_credentials("valid-token"))
    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED


async def test_mint_propagates_invalid_bearer_rejection():
    with (
        patch.object(
            handoff,
            "verify_keycloak_token",
            side_effect=HTTPException(status_code=401, detail="Authentication failed"),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await handoff.mint_handoff_token(credentials=_credentials("invalid-token"))
    assert exc_info.value.status_code == 401
