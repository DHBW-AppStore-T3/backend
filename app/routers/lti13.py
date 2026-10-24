"""LTI 1.3 (IMS LTI Advantage) launch endpoints.

Flow:
  1.  Moodle → GET or POST /lti13/login   (3rd-party OIDC initiation)
  2.  Backend → 302 to Moodle auth.php with state + nonce
  3.  Moodle → POST /lti13/launch  (id_token + state)
  4.  Backend validates JWT → JIT-provisions user (by iss + sub) → one-time code
  5.  Backend → frontend with #lti=1&code=… (fragment) and a binding cookie
  6.  Frontend → POST /lti13/session {code} (+ cookie) → session token
"""

import logging
import secrets
import urllib.parse
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Course, LtiNonce, User, UserRole
from app.services import lti13_service
from app.utils import roles
from app.utils.keycloak_auth import account_by_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/lti13", tags=["LTI 1.3"])

# A login's state/nonce pair lives in ``lti_nonces`` until its launch
# consumes it, at most NONCE_TTL seconds. In the database because login and
# launch are two requests that can reach different API processes
# (``uvicorn --workers``) or replicas.
NONCE_TTL = 300  # 5 minutes
# The cookie that binds a launch's one-time code to this browser (review S19).
BINDING_COOKIE = "lti13_binding"


def _store_nonce(db: Session, state: str, nonce: str) -> None:
    """Remember ``nonce`` for ``state`` (and drop pairs whose launch never came)."""
    _purge_expired_nonces(db)
    db.add(LtiNonce(state=state, nonce=nonce))
    db.commit()


def _pop_nonce(db: Session, state: str) -> str | None:
    """The nonce of ``state``, removed so it serves one launch only; None if unknown or expired."""
    row = db.execute(
        delete(LtiNonce).where(LtiNonce.state == state).returning(LtiNonce.nonce, LtiNonce.created_at)
    ).first()
    db.commit()
    if row is None:
        return None
    nonce, created = row
    if datetime.now(UTC) - created > timedelta(seconds=NONCE_TTL):
        return None
    return nonce


def _purge_expired_nonces(db: Session) -> None:
    db.execute(delete(LtiNonce).where(LtiNonce.created_at < datetime.now(UTC) - timedelta(seconds=NONCE_TTL)))


# ── 1. OIDC 3rd-party login initiation ───────────────────────────────────────


@router.get("/login", operation_id="lti13LoginGet")
async def lti13_login_get(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    """Step 1 (GET): the OIDC login initiation from Moodle, parameters in the query string."""
    return _login(request.query_params, db)


@router.post("/login", operation_id="lti13LoginPost")
async def lti13_login_post(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    """Step 1 (POST): the same initiation with form-encoded parameters.

    Moodle sends this as a POST (observed against a real Moodle 5.x
    instance, 2026-09-25) even though the IMS spec allows either GET or
    POST for the 3rd-party-initiated login, so both are accepted. Two
    routes rather than one for both methods: each gets its own OpenAPI
    operation id (one shared id broke the generated frontend types).
    """
    return _login(await request.form(), db)


def _login(params, db: Session) -> RedirectResponse:
    """Check the initiation, store a fresh state/nonce pair and redirect to Moodle's auth endpoint."""
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

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    _store_nonce(db, state, nonce)

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
    # mismatch", not just in production. APP_ENV=development is the one
    # carve-out, for plain-HTTP localhost development.
    redirect.set_cookie(
        key="lti13_state",
        value=state,
        max_age=NONCE_TTL,
        httponly=True,
        samesite="none",
        secure=not settings.is_development,
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

    nonce = _pop_nonce(db, state)
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
            deployment_id=settings.LTI13_DEPLOYMENT_ID,
        )
    except ValueError as exc:
        logger.warning("LTI 1.3 launch rejected: %s", exc)
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    info = lti13_service.extract_user_info(claims)
    if not info["email"]:
        raise HTTPException(status_code=400, detail="id_token contains no email claim")
    if not info["subject"]:
        raise HTTPException(status_code=400, detail="id_token contains no sub claim (anonymous launch)")
    issuer = info["issuer"] or settings.LTI13_PLATFORM_ISSUER

    user = _provision(db, issuer, info)
    code, binding = lti13_service.issue_login_code(db, user.userId)
    db.commit()

    # Only a one-time code goes to the browser, in the URL fragment (never
    # sent to a server, so out of access logs and Referer headers). The
    # frontend redeems it with the binding cookie set here; a link with
    # somebody else's code thus signs nobody in (review S19).
    fragment = urllib.parse.urlencode({"lti": "1", "code": code})
    target = f"{settings.APP_BASE_URL}#{fragment}"

    # Use a JS redirect so the browser also clears the state cookie (a Set-Cookie
    # on a 302 to a cross-origin URL is unreliable in practice).
    response = HTMLResponse(
        content=f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<script>
  document.cookie = "lti13_state=; Max-Age=0; path=/";
  window.location.href = "{target}";
</script>
</head><body>Weiterleitung zum App-Store…</body></html>
"""
    )
    # No Path: the browser takes this response's directory (``/api/lti13``
    # behind the gateway), where /session is. Lax: the redemption is a
    # same-origin request.
    response.set_cookie(
        key=BINDING_COOKIE,
        value=binding,
        max_age=lti13_service.LTI_CODE_TTL,
        path=None,
        httponly=True,
        samesite="lax",
        secure=not settings.is_development,
    )
    return response


def _provision(db: Session, issuer: str, info: dict, *, retry: bool = True) -> User:
    """The launch's course and user, created or updated; committed.

    A parallel launch of the same new person can win the insert: then once more.
    """
    try:
        course = _course_of_launch(db, issuer, info)
        user = _user_of_launch(db, issuer, info)
        # Re-evaluated on every launch (.github#7 F.5): the role this launch
        # states replaces the one the previous launch stated (app.utils.roles).
        user.lti_role = roles.from_lti(info["role"])
        roles.refresh(user)
        if course is not None:
            db.flush()
            user.courseId = course.courseId
        db.commit()
    except IntegrityError:
        db.rollback()
        if not retry:
            raise
        return _provision(db, issuer, info, retry=False)
    db.refresh(user)
    return user


def _course_of_launch(db: Session, issuer: str, info: dict) -> Course | None:
    """The course of the launch's context, by ``context.id``; the title is its name.

    A course of before (matched by its name then) or one a teacher created by
    hand is adopted once by its name, while it has no context yet. Two
    contexts with the same title thus stay two courses, and a course renamed
    in Moodle keeps its members (review S9).
    """
    context_id, title = info["context_id"], info["course"]
    if not context_id:
        return None
    course = db.query(Course).filter(Course.lti_issuer == issuer, Course.lti_context_id == context_id).first()
    if course is None and title:
        course = db.query(Course).filter(Course.name == title, Course.lti_context_id.is_(None)).first()
        if course is not None:
            course.lti_issuer, course.lti_context_id = issuer, context_id
    if course is None:
        course = Course(name=title or context_id, lti_issuer=issuer, lti_context_id=context_id)
        db.add(course)
    elif title and course.name != title:
        course.name = title
    return course


def _user_of_launch(db: Session, issuer: str, info: dict) -> User:
    """The account of the launch's LTI identity (``iss`` + ``sub``).

    The first launch of an identity is linked to the account with its e-mail
    address (so a Moodle launch and the SSO login meet the same person), and
    from then on the identity decides, not the address: a platform account
    that later carries somebody else's address does not get that person's
    account (review S9). Never linked by address: an account that already
    has another LTI identity, and an admin's account.
    """
    email, subject = info["email"], info["subject"]
    user = db.query(User).filter(User.lti_issuer == issuer, User.lti_subject == subject).first()
    if user is not None:
        # Keycloak keeps the address of an account it knows; otherwise follow
        # the platform, unless another account holds the new address.
        if user.keycloak_id is None and user.email != email and account_by_email(db, email, other_than=user.userId) is None:
            user.email = email
        return user
    user = account_by_email(db, email)
    if user is None:
        names = info["name"].split() if info["name"] else []
        user = User(
            email=email,
            username=email,
            firstName=names[0] if names else None,
            lastName=" ".join(names[1:]) or None,
            role=UserRole.STUDENT,
            lti_issuer=issuer,
            lti_subject=subject,
        )
        db.add(user)
        return user
    if user.lti_subject is not None:
        logger.warning("LTI launch of %s/%s: the address belongs to an account of another LTI identity", issuer, subject)
        raise HTTPException(
            status_code=403,
            detail={"code": "lti_identity_mismatch", "message": "This e-mail address belongs to another person's account."},
        )
    if user.role == UserRole.ADMIN:
        logger.warning("LTI launch of %s/%s: not linking an admin account by e-mail", issuer, subject)
        raise HTTPException(
            status_code=403,
            detail={"code": "lti_admin_not_linked", "message": "Admin accounts sign in with SSO, not through the LMS."},
        )
    user.lti_issuer, user.lti_subject = issuer, subject
    return user


# ── 3. Redeem the launch's one-time code ─────────────────────────────────────


class LtiSessionRequest(BaseModel):
    code: str = Field(min_length=16, max_length=256)


@router.post("/session")
def lti13_session(body: LtiSessionRequest, request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    """Step 6: the frontend trades the launch's code (+ the binding cookie) for a session token.

    400 ``lti_code_invalid`` for an unknown, used, expired or foreign code;
    the code is gone after the first attempt either way.
    """
    user_id = lti13_service.redeem_login_code(db, body.code, request.cookies.get(BINDING_COOKIE, ""))
    user = db.get(User, user_id) if user_id is not None else None
    if user is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "lti_code_invalid", "message": "The sign-in link is invalid or expired; start again from the course."},
        )
    response = JSONResponse(
        content={
            "session_token": lti13_service.issue_session_token(str(user.userId), settings.LTI13_SESSION_SECRET),
            "expires_in": lti13_service.LTI_SESSION_TTL_MINUTES * 60,
            "email": user.email,
            "role": user.role.value,
        }
    )
    response.delete_cookie(BINDING_COOKIE, path=None, secure=not settings.is_development, httponly=True, samesite="lax")
    return response


# ── 4. Tool JWKS (optional) ───────────────────────────────────────────────────


@router.get("/jwks")
async def lti13_jwks() -> JSONResponse:
    """Expose this tool's public JWKS.

    For the demo the tool does not sign messages back to the platform, so
    this returns an empty key set. Replace with actual RSA keys if Assignment
    & Grade Services (AGS) or deep linking is needed.
    """
    return JSONResponse(content={"keys": []})
