"""Shared OpenStack client layer for FastAPI endpoints.

Three responsibilities:

1. Open one connection per request with *the caller's own* credential —
   a chosen one (wizard, quota), or the one for a deployment's project
   (live view, lifecycle): another person's credential is never used, so an
   admin or a project peer acts with their own (security review L-6).
   Exposed as context managers so endpoints can close it cleanly.
2. Refuse Keystone URLs outside the allowlist before connecting
   (``openstack_validator.auth_url_allowed``, .github#7 C), also for
   credentials stored before the allowlist existed.
3. A process-local TTL cache for list responses: the wizard fires several
   GETs in quick succession, and a 60s cache avoids a Keystone token
   refresh per click. A frontend refresh button triggers ``invalidate``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from typing import Any
from uuid import UUID

import openstack
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.crud import openstack_credentials as crud_creds
from app.models import User, UserOpenStackCredential
from app.services.openstack_validator import auth_url_allowed, connect_kwargs

logger = logging.getLogger(__name__)

API_TIMEOUT_SECONDS = 30


@contextmanager
def _connection(creds: dict, owner: str) -> Iterator[Any]:
    """Open a connection and close it afterwards.

    422 ``auth_url_not_allowed`` for a Keystone URL outside the allowlist.
    Any non-HTTPException raised while connecting *or* inside the ``with``
    body becomes 502 ``openstack_unavailable`` (500s are reserved for bugs;
    the SDK's message is logged, not returned).
    """
    if not auth_url_allowed(creds.get("auth_url")):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"reason": "auth_url_not_allowed"},
        )
    conn: Any = None
    try:
        conn = openstack.connect(api_timeout=API_TIMEOUT_SECONDS, **connect_kwargs(creds))
        yield conn
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — SDK raises many types
        logger.warning("OpenStack call failed for %s: %s", owner, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"reason": "openstack_unavailable"},
        ) from None
    finally:
        if conn is not None:
            with suppress(Exception):
                conn.close()


def owned_credential(db: Session, user: User, credential_id: UUID) -> UserOpenStackCredential:
    """The caller's credential ``credential_id``; 404 if missing or someone else's."""
    row = crud_creds.get_owned(db, user.userId, credential_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"reason": "openstack_credential_not_found"},
        )
    return row


def deploy_credential(db: Session, user: User, credential_id: UUID | None) -> UserOpenStackCredential:
    """The credential a new VM deployment is made with: the caller's, verified.

    412 ``openstack_credentials_missing`` without ``credential_id``, 404 for
    a credential that is not the caller's, 409
    ``openstack_credentials_unverified`` for one whose project Keystone has
    not confirmed yet (re-check it first): the deployment's project decides
    who its peers are.
    """
    if credential_id is None:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail={"reason": "openstack_credentials_missing"},
        )
    row = owned_credential(db, user, credential_id)
    if not row.project_verified or not row.project_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": "openstack_credentials_unverified"},
        )
    return row


def project_credential(db: Session, user: User, project_id: str | None) -> UserOpenStackCredential:
    """The caller's own verified credential for ``project_id``.

    409 ``deployment_project_unknown`` when ``project_id`` is None (a VM
    deployment from before projects were verified, until its owner
    re-checks their old credential); 403
    ``openstack_credentials_missing_for_project`` if the caller has no
    credential for it: a peer's or the owner's credential is never used.
    """
    if not project_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": "deployment_project_unknown"},
        )
    row = crud_creds.get_for_project(db, user.userId, project_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"reason": "openstack_credentials_missing_for_project", "project_id": project_id},
        )
    return row


@contextmanager
def credential_connection(row: UserOpenStackCredential) -> Iterator[Any]:
    """A connection with the given credential; the caller checked it is theirs."""
    with _connection(crud_creds.decrypted(row), f"credential {row.credentialId}") as conn:
        yield conn


@contextmanager
def project_connection(db: Session, user: User, project_id: str | None) -> Iterator[Any]:
    """A connection with the caller's own credential for ``project_id`` (403 without one)."""
    with credential_connection(project_credential(db, user, project_id)) as conn:
        yield conn


# ----------------------------------------------------------------
# TTL cache for resource lists
# ----------------------------------------------------------------
# Key = (credential_id, resource_kind, frozenset of filter items)
# Value = (expiry_epoch, data)
_CacheKey = tuple[str, str, frozenset]
_cache: dict[_CacheKey, tuple[float, list[dict]]] = {}
_cache_lock = threading.Lock()
_TTL_SECONDS = 60.0


def _make_key(credential_id: UUID, kind: str, filters: dict | None) -> _CacheKey:
    items: frozenset = frozenset((filters or {}).items())
    return (str(credential_id), kind, items)


def cached_list(
    credential_id: UUID,
    kind: str,
    filters: dict | None,
    fetch: Callable[[], list[dict]],
) -> list[dict]:
    """TTL-cache wrapper. ``fetch`` is called only when no valid entry
    exists. Process-local; data may be up to 60s stale.
    """
    key = _make_key(credential_id, kind, filters)
    now = time.monotonic()

    with _cache_lock:
        cached = _cache.get(key)
        if cached and cached[0] > now:
            return cached[1]

    data = fetch()

    with _cache_lock:
        _cache[key] = (now + _TTL_SECONDS, data)

    return data


def invalidate(credential_id: UUID, kind: str | None = None) -> int:
    """Invalidate the cache of one credential (refresh button, credential
    replaced or deleted). Without ``kind`` every resource type; ``kind``
    also matches kinds cached per variant (``availability_zones`` removes
    ``availability_zones_compute`` …). Returns the number of removed entries.
    """
    cred = str(credential_id)
    removed = 0
    with _cache_lock:
        for key in list(_cache.keys()):
            if key[0] != cred:
                continue
            if kind is not None and key[1] != kind and not key[1].startswith(kind + "_"):
                continue
            del _cache[key]
            removed += 1
    return removed
