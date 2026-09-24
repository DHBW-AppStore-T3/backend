"""Production-safe identity handoff for self-service-ui.

self-service-ui's oauth2-proxy sidecar injects a Keycloak bearer on
requests it proxies to us. POST /handoff/mint trades that bearer for a
short-lived, app-scoped handoff token so self-service-ui never has to pass
a raw Keycloak access token to the App-Store frontend (leak risk in
browser history/logs).
"""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings
from app.services import handoff_service
from app.utils.keycloak_auth import verify_keycloak_token

router = APIRouter()

security = HTTPBearer()


@router.post("/mint")
async def mint_handoff_token(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    token_info = verify_keycloak_token(credentials.credentials)
    email = token_info.get("email")
    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has no email claim",
        )

    handoff_token = handoff_service.issue_handoff_token(email, settings.HANDOFF_SESSION_SECRET)
    return {"email": email, "handoff_token": handoff_token}
