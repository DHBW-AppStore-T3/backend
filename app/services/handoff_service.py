"""Self-service-ui handoff token lifecycle.

Structurally identical to lti13_service's session-token functions (HS256,
short-lived), but with its own secret (HANDOFF_SESSION_SECRET) — a
compromised LTI secret must not also open the self-service handoff path.
"""

from datetime import UTC, datetime

from jose import JWTError, jwt

HANDOFF_TOKEN_TTL_SECONDS = 300


def issue_handoff_token(
    email: str, secret: str, ttl_seconds: int = HANDOFF_TOKEN_TTL_SECONDS
) -> str:
    """Issue a signed handoff JWT for the given email."""
    now = int(datetime.now(UTC).timestamp())
    payload = {
        "sub": email,
        "type": "handoff",
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def verify_handoff_token(token: str, secret: str) -> str:
    """Verify a handoff JWT and return the email (sub). Raises ValueError on failure."""
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"])
    except JWTError as exc:
        raise ValueError(f"Invalid handoff token: {exc}") from exc
    if claims.get("type") != "handoff":
        raise ValueError("Token type is not handoff")
    return claims["sub"]
