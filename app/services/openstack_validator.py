"""Authorize an OpenStack credential payload against the target Keystone.

Used by the upsert and `/test` endpoints. The 15-second socket boundary
keeps a stuck Keystone from blocking the FastAPI worker; openstacksdk
otherwise has no consistent timeout knob across releases.
"""
from __future__ import annotations

import socket

import openstack
from openstack import exceptions as os_exc

from app.config import settings
from app.models import OpenStackAuthType
from app.schemas import OpenStackCredentialUpsert

_TIMEOUT_SECONDS = 15


def _build_connect_kwargs(payload: OpenStackCredentialUpsert) -> dict:
    base = {
        "auth_url": payload.auth_url,
        "region_name": payload.region_name,
        "interface": payload.interface or "public",
        "identity_api_version": payload.identity_api_version or "3",
    }
    if payload.auth_type == OpenStackAuthType.APPLICATION_CREDENTIAL:
        base.update({
            "auth_type": "v3applicationcredential",
            "application_credential_id": payload.identifier,
            "application_credential_secret": payload.secret,
        })
    else:
        base.update({
            "auth_type": "password",
            "username": payload.identifier,
            "password": payload.secret,
            "project_id": payload.project_id,
            "project_name": payload.project_name,
            "user_domain_name": payload.user_domain_name,
            "project_domain_name": payload.project_domain_name or payload.user_domain_name,
        })
    # openstack.connect tolerates None values for keys it doesn't need; keep them.
    return base


def validate(payload: OpenStackCredentialUpsert) -> tuple[bool, str | None]:
    """Try to authorize. Returns (ok, error_message).

    The error message is short and human-readable — safe to surface in the
    UI. Never echoes the secret back.
    """
    if settings.OPENSTACK_SIMULATE:
        return True, None
    prev_default_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(_TIMEOUT_SECONDS)
    try:
        conn = openstack.connect(**_build_connect_kwargs(payload))
        # Force a token round-trip; .authorize() returns the token string.
        conn.authorize()
        return True, None
    except os_exc.HttpException as e:
        status = getattr(e, "status_code", None) or getattr(e, "http_status", None)
        if status in (401, 403):
            return False, "Invalid credentials"
        if status == 404:
            return False, "Project or domain not found"
        return False, f"OpenStack rejected request (HTTP {status or '?'})"
    except (TimeoutError, socket.gaierror) as e:
        return False, f"Could not reach auth_url: {e}"
    except os_exc.SDKException as e:
        return False, f"OpenStack SDK error: {type(e).__name__}"
    except Exception as e:
        # Unknown error — return the type only, never the message (might
        # contain the request body).
        return False, f"Unexpected error: {type(e).__name__}"
    finally:
        socket.setdefaulttimeout(prev_default_timeout)
