"""LTI 1.3 (IMS LTI Advantage) launch endpoints.

Flow:
  1.  Moodle → GET or POST /lti13/login   (3rd-party OIDC initiation)
  2.  Backend → 302 to Moodle auth.php with state + nonce
  3.  Moodle → POST /lti13/launch  (id_token + state)
  4.  Backend validates JWT → JIT-provisions user → issues session token
  5.  Backend → 302 to frontend with ?lti=1&email=…&session_token=…
"""

import logging
import secrets
import time
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Course, User, UserRole
from app.services import lti13_service
from app.utils.keycloak_auth import higher_role

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/lti13", tags=["LTI 1.3"])

# In-memory nonce store: state → (nonce, created_at).
# A background sweep is not needed for the demo — entries expire naturally
# after NONCE_TTL seconds, and the dict stays small (one entry per launch).
_nonce_store: dict[str, tuple[str, float]] = {}
NONCE_TTL = 300  # 5 minutes


def _store_nonce(state: str, nonce: str) -> None:
    _nonce_store[state] = (nonce, time.time())


def _pop_nonce(state: str) -> str | None:
    entry = _nonce_store.pop(state, None)
    if entry is None:
        return None
    nonce, created = entry
    if time.time() - created > NONCE_TTL:
        return None
    return nonce


def _purge_expired_nonces() -> None:
    now = time.time()
    expired = [s for s, (_, t) in _nonce_store.items() if now - t > NONCE_TTL]
    for s in expired:
        _nonce_store.pop(s, None)


# ── 1. OIDC 3rd-party login initiation ───────────────────────────────────────


@router.api_route("/login", methods=["GET", "POST"])
async def lti13_login(request: Request) -> RedirectResponse:
    """Step 1: receive the OIDC initiation request from Moodle and redirect
    back to Moodle's auth endpoint with a fresh state/nonce pair.

    Moodle sends this as a POST (observed against a real Moodle 5.x
    instance, 2026-09-25) even though the IMS spec allows either GET or
    POST for the 3rd-party-initiated login — so both are accepted here,
    reading params from the query string or form body as appropriate.
    """
    if request.method == "POST":
        params = await request.form()
    else:
        params = request.query_params

    iss = params.get("iss")
    login_hint = params.get("login_hint")
    client_id = params.get("client_id")
    lti_message_hint = params.get("lti_message_hint")

    if not iss:
        raise HTTPException(status_code=400, detail="Missing iss parameter")
    if iss != settings.LTI13_PLATFORM_ISSUER:
        raise HTTPException(status_code=400, detail=f"Unknown issuer: {iss!r}")

    if client_id and client_id != settings.LTI13_CLIENT_ID:
        logger.warning(
            "LTI 1.3 login: client_id %r != configured %r", client_id, settings.LTI13_CLIENT_ID
        )

    _purge_expired_nonces()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    _store_nonce(state, nonce)

    # Set state in a short-lived cookie so we can verify it on the POST
    auth_params: dict[str, str] = {
        "scope": "openid",
        "response_type": "id_token",
        "client_id": settings.LTI13_CLIENT_ID or (client_id or ""),
        "redirect_uri": settings.LTI13_REDIRECT_URI,
        "login_hint": login_hint or "",
        "state": state,
        "nonce": nonce,
        "response_mode": "form_post",
        "prompt": "none",
    }
    if lti_message_hint:
        auth_params["lti_message_hint"] = lti_message_hint

    auth_url = f"{settings.LTI13_PLATFORM_ISSUER}/mod/lti/auth.php?" + urllib.parse.urlencode(
        auth_params
    )

    redirect = RedirectResponse(url=auth_url, status_code=302)
    # Store state in a SameSite=None cookie so the browser sends it back on
    # the POST (the POST comes from Moodle's domain via form_post).
    # SameSite=None requires Secure in every modern browser — without it the
    # cookie is silently dropped and /launch always fails with "State
    # mismatch", not just in production. DEV_MODE is the one carve-out, for
    # plain-HTTP localhost development.
    redirect.set_cookie(
        key="lti13_state",
        value=state,
        max_age=NONCE_TTL,
        httponly=True,
        samesite="none",
        secure=not settings.DEV_MODE,
    )
    return redirect


# ── 2. Receive id_token from Moodle ──────────────────────────────────────────


@router.post("/launch", response_class=HTMLResponse)
async def lti13_launch(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Step 3: Moodle POSTs the id_token here after the OIDC dance."""

    form = await request.form()
    id_token = form.get("id_token", "")
    state = form.get("state", "")

    if not id_token:
        raise HTTPException(status_code=400, detail="Missing id_token")

    # Verify state matches the cookie we set in step 1
    cookie_state = request.cookies.get("lti13_state", "")
    if not cookie_state or cookie_state != state:
        raise HTTPException(status_code=400, detail="State mismatch — possible CSRF")

    nonce = _pop_nonce(state)
    if nonce is None:
        raise HTTPException(status_code=400, detail="Unknown or expired state")

    if not settings.LTI13_CLIENT_ID:
        raise HTTPException(
            status_code=503,
            detail="LTI 1.3 not configured: LTI13_CLIENT_ID is empty. "
            "Register the External Tool in Moodle and set the env var.",
        )

    try:
        claims = lti13_service.validate_id_token(
            id_token=str(id_token),
            client_id=settings.LTI13_CLIENT_ID,
            expected_nonce=nonce,
            platform_issuer=settings.LTI13_PLATFORM_ISSUER,
            jwks_url=settings.LTI13_PLATFORM_JWKS_URL,
        )
    except ValueError as exc:
        logger.warning("LTI 1.3 launch rejected: %s", exc)
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    info = lti13_service.extract_user_info(claims)
    email = info["email"]
    if not email:
        raise HTTPException(status_code=400, detail="id_token contains no email claim")

    # JIT-provision course
    course_id = ""
    if info["course"]:
        course = db.query(Course).filter(Course.name == info["course"]).first()
        if not course:
            course = Course(name=info["course"])
            db.add(course)
            db.commit()
            db.refresh(course)
        course_id = str(course.courseId)

    # JIT-provision user. Moodle is the authoritative role source (real users
    # carry no role in Keycloak). Role is monotonic — only promote an existing
    # user, never demote, so a later self-service login can't reset it.
    role = UserRole.TEACHER if info["role"] == "instructor" else UserRole.STUDENT
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        user = User(
            email=email,
            username=email,
            firstName=info["name"].split()[0] if info["name"] else None,
            lastName=" ".join(info["name"].split()[1:]) if len(info["name"].split()) > 1 else None,
            role=role,
            courseId=course_id or None,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        updated = False
        promoted = higher_role(user.role, role)
        if user.role != promoted:
            user.role = promoted
            updated = True
        if course_id:
            user.courseId = course_id
            updated = True
        if updated:
            db.commit()

    # Issue an app-internal session token — lets the frontend call the API
    # without a Keycloak PKCE flow.
    session_token = lti13_service.issue_session_token(email, settings.LTI13_SESSION_SECRET)

    query = urllib.parse.urlencode(
        {
            "lti": "1",
            "email": email,
            "name": info["name"],
            "role": info["role"],
            "course": info["course"],
            "courseId": course_id,
            "session_token": session_token,
        }
    )
    target = f"{settings.APP_BASE_URL}?{query}"

    # Use a JS redirect so the browser also clears the state cookie (a Set-Cookie
    # on a 302 to a cross-origin URL is unreliable in practice).
    return HTMLResponse(
        content=f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<script>
  document.cookie = "lti13_state=; Max-Age=0; path=/";
  window.location.href = "{target}";
</script>
</head><body>Weiterleitung zum App-Store…</body></html>
"""
    )


# ── 3. Tool JWKS (optional) ───────────────────────────────────────────────────


@router.get("/jwks")
async def lti13_jwks() -> JSONResponse:
    """Expose this tool's public JWKS.

    For the demo the tool does not sign messages back to the platform, so
    this returns an empty key set. Replace with actual RSA keys if Assignment
    & Grade Services (AGS) or deep linking is needed.
    """
    return JSONResponse(content={"keys": []})
