"""LTI 1.3 helpers: platform JWKS fetching, id_token validation, session-token lifecycle."""

import time
import threading
import urllib.parse
from datetime import datetime, timezone

import requests as http_requests
from jose import jwt, JWTError, jwk
from jose.utils import base64url_decode

_jwks_cache: dict[str, tuple[list, float]] = {}
_jwks_lock = threading.Lock()
JWKS_TTL = 3600  # seconds


def fetch_platform_jwks(jwks_url: str) -> list[dict]:
    """Return the platform's public JWKS, cached for JWKS_TTL seconds."""
    now = time.time()
    with _jwks_lock:
        cached = _jwks_cache.get(jwks_url)
        if cached and now - cached[1] < JWKS_TTL:
            return cached[0]

    resp = http_requests.get(jwks_url, timeout=10)
    resp.raise_for_status()
    keys = resp.json().get("keys", [])

    with _jwks_lock:
        _jwks_cache[jwks_url] = (keys, now)

    return keys


def validate_id_token(
    id_token: str,
    client_id: str,
    expected_nonce: str,
    platform_issuer: str,
    jwks_url: str,
) -> dict:
    """Validate a LTI 1.3 id_token JWT.

    Verifies: signature (RS256 from platform JWKS), issuer, audience,
    nonce, expiry, and LTI message type.

    Raises ValueError with a human-readable message on any failure.
    Returns the decoded claims dict on success.
    """
    keys = fetch_platform_jwks(jwks_url)

    # Decode header to find the right key
    try:
        header = jwt.get_unverified_header(id_token)
    except JWTError as exc:
        raise ValueError(f"Cannot read JWT header: {exc}") from exc

    kid = header.get("kid")
    matching = [k for k in keys if k.get("kid") == kid] if kid else keys
    if not matching:
        # Invalidate cache and retry once — the platform may have rotated keys
        with _jwks_lock:
            _jwks_cache.pop(jwks_url, None)
        keys = fetch_platform_jwks(jwks_url)
        matching = [k for k in keys if k.get("kid") == kid] if kid else keys
        if not matching:
            raise ValueError(f"No matching JWK found for kid={kid!r}")

    last_exc: Exception = ValueError("No key attempted")
    for jwk_key in matching:
        try:
            claims = jwt.decode(
                id_token,
                jwk_key,
                algorithms=["RS256"],
                audience=client_id,
                issuer=platform_issuer,
                options={"verify_at_hash": False},
            )
            break
        except JWTError as exc:
            last_exc = exc
    else:
        raise ValueError(f"JWT validation failed: {last_exc}") from last_exc

    # Nonce must match to prevent replay
    if claims.get("nonce") != expected_nonce:
        raise ValueError("Nonce mismatch — possible replay attack")

    # Must be an LTI Resource Link launch
    msg_type = claims.get("https://purl.imsglobal.org/spec/lti/claim/message_type")
    if msg_type != "LtiResourceLinkRequest":
        raise ValueError(f"Unexpected LTI message type: {msg_type!r}")

    return claims


def _extract_role(claims: dict) -> str:
    """Map IMS LTI role URNs from id_token claims to 'instructor' or 'student'."""
    roles: list[str] = claims.get(
        "https://purl.imsglobal.org/spec/lti/claim/roles", []
    )
    joined = ",".join(roles).lower()
    if any(m in joined for m in ("instructor", "teacher", "administrator")):
        return "instructor"
    return "student"


def extract_user_info(claims: dict) -> dict:
    """Return a plain dict with email, name, and role from id_token claims."""
    return {
        "email": claims.get("email", ""),
        "name": claims.get("name", claims.get("email", "")),
        "role": _extract_role(claims),
        "course": (
            claims.get("https://purl.imsglobal.org/spec/lti/claim/context", {})
            .get("title", "")
        ),
    }


# ── Session tokens ────────────────────────────────────────────────────────────
# After a successful LTI 1.3 launch the backend issues a short-lived app-internal
# JWT so the frontend can authenticate subsequent API calls without Keycloak.
# The token type claim ("lti_session") lets keycloak_auth.py route it separately.

LTI_SESSION_TTL_HOURS = 8


def issue_session_token(email: str, secret: str, ttl_hours: int = LTI_SESSION_TTL_HOURS) -> str:
    """Issue a signed LTI session JWT for the given email."""
    now = int(datetime.now(timezone.utc).timestamp())
    payload = {
        "sub": email,
        "type": "lti_session",
        "iat": now,
        "exp": now + ttl_hours * 3600,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def verify_session_token(token: str, secret: str) -> str:
    """Verify an LTI session JWT and return the email (sub). Raises ValueError on failure."""
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"])
    except JWTError as exc:
        raise ValueError(f"Invalid LTI session token: {exc}") from exc
    if claims.get("type") != "lti_session":
        raise ValueError("Token type is not lti_session")
    return claims["sub"]
