"""
Keycloak Authentication & Authorization
Handles token validation and user management with Keycloak.

Access tokens are verified with PyJWT against the realm's JWKS, issuer and
client (``app.utils.oidc``, .github#7 P). The Keycloak admin client is used
only for the user search.
"""

import logging
import threading

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from keycloak import KeycloakAdmin
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import User, UserRole
from app.services import lti13_service
from app.utils import roles
from app.utils.oidc import IdentityProviderUnavailable, InvalidToken, OidcVerifier

logger = logging.getLogger(__name__)

security = HTTPBearer()
optional_security = HTTPBearer(auto_error=False)


# ----------------------------------------------------------------
# KEYCLOAK ADMIN CLIENT (user search)
# ----------------------------------------------------------------
def get_keycloak_admin() -> KeycloakAdmin:
    """Get Keycloak Admin client (uses service-account client_credentials grant)."""
    return KeycloakAdmin(
        server_url=settings.KEYCLOAK_SERVER_URL,
        realm_name=settings.KEYCLOAK_REALM,
        client_id=settings.KEYCLOAK_CLIENT_ID,
        client_secret_key=settings.KEYCLOAK_CLIENT_SECRET,
        verify=True,
    )


# ----------------------------------------------------------------
# TOKEN VALIDATION
# ----------------------------------------------------------------
_verifier: OidcVerifier | None = None
_verifier_lock = threading.Lock()


def get_verifier() -> OidcVerifier:
    """The process-wide verifier for the realm's access tokens."""
    global _verifier
    with _verifier_lock:
        if _verifier is None:
            _verifier = OidcVerifier(
                issuer=settings.keycloak_issuer,
                jwks_url=settings.keycloak_jwks_url,
                allowed_clients=settings.oidc_allowed_clients,
                audience=settings.OIDC_AUDIENCE or None,
            )
        return _verifier


def reset_verifier() -> None:
    """Forget the verifier (tests, settings changes)."""
    global _verifier
    with _verifier_lock:
        _verifier = None


def verify_keycloak_token_offline(token: str) -> dict:
    """Verify an access token of the realm and return its claims.

    401 for an invalid token, 503 when Keycloak cannot be reached to fetch
    the keys (a 401 would send the browser into a login that cannot work).
    """
    try:
        return get_verifier().verify(token)
    except IdentityProviderUnavailable as e:
        logger.warning("Cannot verify a token: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The sign-in service is not reachable; try again shortly",
        ) from e
    except InvalidToken as e:
        logger.info("Rejected a token: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


# ----------------------------------------------------------------
# USER SYNC (Just-in-Time Provisioning)
# ----------------------------------------------------------------
def account_by_email(db: Session, email: str, *, other_than=None) -> User | None:
    """The account with ``email`` (ignoring case), optionally not ``other_than``'s."""
    query = db.query(User).filter(func.lower(User.email) == email.strip().lower())
    if other_than is not None:
        query = query.filter(User.userId != other_than)
    return query.first()


def sync_user_from_keycloak(db: Session, keycloak_user_data: dict, *, _retry: bool = True) -> User:
    """
    Synchronize a Keycloak user into the local DB.
    Creates the user if missing, updates names and the Keycloak role if changed.
    Expects keys: id (or sub), username, email, roles (or realm_access.roles),
    firstName/given_name, lastName/family_name.

    The token's realm roles are the Keycloak source of the role, and only
    that source is overwritten; the effective role is recomputed from all
    sources (:mod:`app.utils.roles`, review S8). Data without ``roles`` and
    ``realm_access`` (a user search result) says nothing about the role and
    leaves it as it is: a search must not demote whoever it finds.

    A Keycloak account seen for the first time is linked to the account
    with its e-mail address if there is one, e.g. the one a Moodle launch
    created (F3: a second row with the same address was a 500). That is
    safe on the Keycloak side: the realm allows each address once, and
    people cannot change their own (no ``manage-account``).
    """
    keycloak_id = keycloak_user_data.get("id") or keycloak_user_data.get("sub")
    email = keycloak_user_data.get("email")
    username = keycloak_user_data.get("username") or keycloak_id
    keycloak_roles = keycloak_user_data.get("roles")
    if keycloak_roles is None and "realm_access" in keycloak_user_data:
        keycloak_roles = keycloak_user_data["realm_access"].get("roles", [])
    keycloak_role = roles.from_keycloak(keycloak_roles) if keycloak_roles is not None else None
    first_name = keycloak_user_data.get("firstName") or keycloak_user_data.get("given_name")
    last_name = keycloak_user_data.get("lastName") or keycloak_user_data.get("family_name")

    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Keycloak user has no email; refusing to provision.",
        )

    updated = False
    user = db.query(User).filter(User.keycloak_id == keycloak_id).first()
    if user is None:
        user = account_by_email(db, email)
        if user is not None:
            logger.info(
                "Linking Keycloak account %s to the existing account %s", keycloak_id, user.userId
            )
            user.keycloak_id = keycloak_id
        else:
            user = User(
                keycloak_id=keycloak_id, email=email, username=username, role=UserRole.STUDENT
            )
            db.add(user)
        updated = True

    # Email is the identifier used for display, search, and
    # notifications; follow the Keycloak record when it changes (unless
    # another account holds the new address: then both keep theirs).
    if user.email != email:
        if account_by_email(db, email, other_than=user.userId) is None:
            user.email = email
            updated = True
        else:
            logger.warning(
                "Keycloak account %s changed its e-mail to one another account holds", keycloak_id
            )
    # Username is the login identifier shown in the UI and in
    # issued credentials; keep it in sync too.
    if username and user.username != username:
        user.username = username
        updated = True
    if keycloak_role is not None and user.keycloak_role != keycloak_role:
        user.keycloak_role = keycloak_role
        updated = True
    updated = roles.refresh(user) or updated
    if first_name and user.firstName != first_name:
        user.firstName = first_name
        updated = True
    if last_name and user.lastName != last_name:
        user.lastName = last_name
        updated = True
    if not updated:
        return user
    try:
        db.commit()
    except IntegrityError:
        # A parallel request of the same first sign-in created the row first.
        db.rollback()
        if not _retry:
            raise
        return sync_user_from_keycloak(db, keycloak_user_data, _retry=False)
    db.refresh(user)
    return user


# ----------------------------------------------------------------
# AUTH DEPENDENCY
# ----------------------------------------------------------------
def get_current_user_keycloak(
    credentials: HTTPAuthorizationCredentials | None = Depends(optional_security),
    x_dev_user: str | None = Header(default=None, alias="X-Dev-User"),
    db: Session = Depends(get_db),
) -> User:
    """
    Validate the bearer token and return the local User record.
    JIT-provisions the user from Keycloak claims on first sight.

    In development (APP_ENV=development), the X-Dev-User header may carry an
    email address to bypass Keycloak entirely — the matching seed user is
    returned directly. This path is refused in production.

    Tries the LTI 1.3 session token first (HS256, issued by
    ``/lti13/launch``) so Moodle launches authenticate without a Keycloak
    round-trip; falls through to Keycloak on any other token.
    """
    if settings.is_development and x_dev_user:
        user = db.query(User).filter(User.email == x_dev_user).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Dev user '{x_dev_user}' not found in database (run make seed-dev)",
            )
        return user

    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if settings.lti13_enabled:
        try:
            user_id = lti13_service.verify_session_token(
                credentials.credentials, settings.LTI13_SESSION_SECRET
            )
        except ValueError:
            pass  # not an LTI session token — fall through to Keycloak
        else:
            user = db.get(User, user_id)
            if user:
                return user

    token_info = verify_keycloak_token_offline(credentials.credentials)

    keycloak_id = token_info.get("sub")
    if not keycloak_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing user ID (sub)",
        )

    return sync_user_from_keycloak(
        db,
        {
            "id": keycloak_id,
            "email": token_info.get("email"),
            "username": token_info.get("preferred_username"),
            "roles": token_info.get("realm_access", {}).get("roles", []),
            "firstName": token_info.get("given_name"),
            "lastName": token_info.get("family_name"),
        },
    )


# ----------------------------------------------------------------
# KEYCLOAK USER LOOKUP (admin API)
# ----------------------------------------------------------------
def _project_keycloak_user(u: dict, *, include_enabled: bool = False) -> dict:
    """Project a raw Keycloak user record into our simplified dict shape.

    Includes the ``enabled`` flag only when ``include_enabled`` is set,
    preserving the differing output shapes of the two call sites.
    """
    projected = {
        "id": u.get("id"),
        "username": u.get("username"),
        "email": u.get("email"),
        "firstName": u.get("firstName", ""),
        "lastName": u.get("lastName", ""),
    }
    if include_enabled:
        projected["enabled"] = u.get("enabled", True)
    return projected


def search_keycloak_users(search_query: str, max_results: int = 10) -> list[dict]:
    """Search Keycloak users by username/email/name (uses service account)."""
    try:
        keycloak_admin = get_keycloak_admin()
        users = keycloak_admin.get_users({"search": search_query, "max": max_results})
        return [_project_keycloak_user(u, include_enabled=True) for u in users]
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to search Keycloak users",
        )


def get_keycloak_users_by_ids(ids: list[str]) -> dict:
    """Resolve a list of Keycloak IDs to simplified user dicts."""
    result: dict = {}
    if not ids:
        return result
    try:
        kc = get_keycloak_admin()
        for kid in ids:
            try:
                user = kc.get_user(kid)
            except Exception:
                try:
                    users = kc.get_users({"search": kid, "max": 1})
                    user = users[0] if users else None
                except Exception:
                    user = None
            if not user:
                continue
            projected = _project_keycloak_user(user)
            projected["id"] = user.get("id") or kid
            result[user.get("id") or kid] = projected
        return result
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch Keycloak users",
        )
