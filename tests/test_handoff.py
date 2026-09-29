from unittest.mock import patch

from fastapi import HTTPException, status
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


def test_mint_returns_token_for_valid_bearer():
    client = TestClient(app)
    with (
        patch.object(settings, "HANDOFF_SESSION_SECRET", "test-secret"),
        patch(
            "app.routers.handoff.verify_keycloak_token",
            return_value={"active": True, "email": "student@example.com"},
        ),
    ):
        response = client.post("/handoff/mint", headers={"Authorization": "Bearer valid-token"})

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "student@example.com"
    assert body["handoff_token"]


def test_mint_rejects_invalid_bearer():
    client = TestClient(app)
    with patch(
        "app.routers.handoff.verify_keycloak_token",
        side_effect=HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication failed"
        ),
    ):
        response = client.post("/handoff/mint", headers={"Authorization": "Bearer invalid-token"})

    assert response.status_code == 401
