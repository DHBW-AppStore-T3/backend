"""LTI 1.3 helpers: platform JWKS fetching, id_token validation, session-token lifecycle."""

import hashlib
import hmac
import secrets
import threading
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
import requests as http_requests
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models import LtiLoginCode

_jwks_cache: dict[str, tuple[list, float]] = {}
_jwks_lock = threading.Lock()
JWKS_TTL = 3600  # seconds

DEPLOYMENT_ID_CLAIM = "https://purl.imsglobal.org/spec/lti/claim/deployment_id"
CONTEXT_CLAIM = "https://purl.imsglobal.org/spec/lti/claim/context"


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
    deployment_id: str = "",
) -> dict:
    """Validate a LTI 1.3 id_token JWT.

    Verifies: signature (RS256 from platform JWKS), issuer, audience,
    nonce, expiry, LTI message type and, when ``deployment_id`` is set, that
    the launch comes from exactly that tool deployment (review S9).

    Raises ValueError with a human-readable message on any failure.
    Returns the decoded claims dict on success.
    """
    keys = fetch_platform_jwks(jwks_url)

    # Decode header to find the right key
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
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
                jwt.PyJWK(jwk_key, algorithm="RS256").key,
                algorithms=["RS256"],
                audience=client_id,
                issuer=platform_issuer,
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
            break
        except (jwt.PyJWTError, ValueError) as exc:
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

    # One tool registration per platform: a launch of another deployment of
    # the tool (another site of a shared platform) is not ours.
    if deployment_id and claims.get(DEPLOYMENT_ID_CLAIM) != deployment_id:
        raise ValueError(f"Unexpected LTI deployment_id: {claims.get(DEPLOYMENT_ID_CLAIM)!r}")

    return claims


def _extract_role(claims: dict) -> str:
    """Map IMS LTI role URNs from id_token claims to 'instructor' or 'student'."""
    roles: list[str] = claims.get("https://purl.imsglobal.org/spec/lti/claim/roles", [])
    joined = ",".join(roles).lower()
    if any(m in joined for m in ("instructor", "teacher", "administrator")):
        return "instructor"
    return "student"


def extract_user_info(claims: dict) -> dict:
    """Return a plain dict with the LTI identity, email, name, role and course from id_token claims.

    ``issuer`` + ``subject`` identify the person, ``context_id`` the course;
    e-mail and title are attributes (review S9).
    """
    context = claims.get(CONTEXT_CLAIM) or {}
    return {
        "issuer": claims.get("iss", ""),
        "subject": str(claims.get("sub") or ""),
        "email": claims.get("email", ""),
        "name": claims.get("name", claims.get("email", "")),
        "role": _extract_role(claims),
        "context_id": str(context.get("id") or ""),
        "course": context.get("title") or context.get("label") or "",
    }


# ── Session tokens ────────────────────────────────────────────────────────────
# After a successful LTI 1.3 launch the frontend redeems a one-time code for a
# short-lived app-internal JWT, so it can call the API without Keycloak. The
# token type claim ("lti_session") lets keycloak_auth.py route it separately.

LTI_SESSION_TTL_MINUTES = 240


def issue_session_token(user_id: str, secret: str, ttl_minutes: int = LTI_SESSION_TTL_MINUTES) -> str:
    """Issue a signed LTI session JWT for the user with ``user_id``."""
    now = int(datetime.now(UTC).timestamp())
    payload = {
        "sub": str(user_id),
        "type": "lti_session",
        "iat": now,
        "exp": now + ttl_minutes * 60,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def verify_session_token(token: str, secret: str) -> UUID:
    """Verify an LTI session JWT and return the user id (sub). Raises ValueError on failure.

    Tokens of before (``sub`` = an e-mail address) are refused: the address is
    not what identifies a person.
    """
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["exp", "sub"]})
    except jwt.PyJWTError as exc:
        raise ValueError(f"Invalid LTI session token: {exc}") from exc
    if claims.get("type") != "lti_session":
        raise ValueError("Token type is not lti_session")
    try:
        return UUID(str(claims["sub"]))
    except ValueError as exc:
        raise ValueError("LTI session token does not name a user id") from exc


# ── One-time login codes ──────────────────────────────────────────────────────
# The launch does not hand out the session token: it gives the browser a code
# (in the URL fragment) and a binding (in an HttpOnly cookie). Only both
# together get the token, so a crafted link with somebody else's code signs
# nobody in (login CSRF, review S19).

LTI_CODE_TTL = 120  # seconds between the launch and the frontend's redemption


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def issue_login_code(db: Session, user_id: UUID) -> tuple[str, str]:
    """A fresh ``(code, binding)`` for ``user_id``; only their hashes are stored (not committed)."""
    db.execute(
        delete(LtiLoginCode).where(LtiLoginCode.created_at < datetime.now(UTC) - timedelta(seconds=LTI_CODE_TTL))
    )
    code, binding = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    db.add(LtiLoginCode(code_hash=_sha256(code), binding_hash=_sha256(binding), userId=user_id))
    return code, binding


def redeem_login_code(db: Session, code: str, binding: str) -> UUID | None:
    """The user id of ``code`` if ``binding`` is its own and it is fresh, else None.

    A code is gone after the first attempt, matching or not.
    """
    row = db.execute(
        delete(LtiLoginCode)
        .where(LtiLoginCode.code_hash == _sha256(code))
        .returning(LtiLoginCode.binding_hash, LtiLoginCode.userId, LtiLoginCode.created_at)
    ).first()
    db.commit()
    if row is None or not binding:
        return None
    binding_hash, user_id, created = row
    if datetime.now(UTC) - created > timedelta(seconds=LTI_CODE_TTL):
        return None
    if not hmac.compare_digest(binding_hash, _sha256(binding)):
        return None
    return user_id
