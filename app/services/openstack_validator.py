"""Check an OpenStack credential against Keystone and learn its project.

Ported from dhbw-appstore. A credential is stored only after Keystone issued
a project-scoped token for it; the project id and name are taken from that
token. They decide who is a peer in a project, so a value the user typed
must never count.

Before anything is sent, the Keystone URL must be one of
``OPENSTACK_ALLOWED_AUTH_URLS`` (.github#7 C, security review M-4): the URL
is user input, and without the allowlist API and worker would send login
requests wherever it points, internal addresses and the metadata service
included, and a fake Keystone could hand them a service catalogue of
internal endpoints.

The timeout is the session's (``api_timeout``), not the process-wide socket
default this module used to set and reset around the call, which raced
with every other request thread.
"""

from __future__ import annotations

from dataclasses import dataclass

import openstack
from keystoneauth1 import exceptions as ks_exc
from keystoneauth1.identity.base import BaseIdentityPlugin
from openstack import exceptions as os_exc

from app import keystone_urls
from app.config import settings
from app.schemas import OpenStackCredentialUpsert

TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class Scope:
    """The project a validated credential is scoped to, as Keystone reports it."""

    project_id: str
    project_name: str | None


class CredentialRejected(Exception):
    """The credential is not accepted; ``reason`` is safe to show.

    One of ``auth_url_not_allowed``, ``invalid_credentials``,
    ``project_not_found``, ``unreachable``, ``rejected``,
    ``not_project_scoped``, ``error``; the UI translates it. The secret is
    never part of it.
    """

    def __init__(self, reason: str, status: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def auth_url_allowed(url: str | None) -> bool:
    """Whether the backend and the worker may send credentials to ``url`` (``app.keystone_urls``).

    In development an empty allowlist allows any https URL; in production
    an empty allowlist allows none (fail closed).
    """
    return keystone_urls.auth_url_allowed(
        url, settings.OPENSTACK_ALLOWED_AUTH_URLS, development=settings.is_development
    )


def ensure_auth_url_allowed(url: str | None) -> None:
    """Raise ``CredentialRejected("auth_url_not_allowed")`` unless :func:`auth_url_allowed`."""
    if not auth_url_allowed(url):
        raise CredentialRejected("auth_url_not_allowed")


def connect_kwargs(creds: dict) -> dict:
    """``openstack.connect`` kwargs from a plaintext credential dict.

    Supports password and application-credential (v3applicationcredential)
    auth. Shared with ``openstack_client``.
    """
    base = {
        "auth_url": creds["auth_url"],
        "region_name": creds.get("region_name"),
        "interface": creds.get("interface") or "public",
        "identity_api_version": creds.get("identity_api_version") or "3",
    }
    if creds["auth_type"] == "v3applicationcredential":
        base.update(
            {
                "auth_type": "v3applicationcredential",
                "application_credential_id": creds["identifier"],
                "application_credential_secret": creds["secret"],
            }
        )
    else:
        base.update(
            {
                "auth_type": "password",
                "username": creds["identifier"],
                "password": creds["secret"],
                "project_id": creds.get("project_id"),
                "project_name": creds.get("project_name"),
                "user_domain_name": creds.get("user_domain_name"),
                "project_domain_name": creds.get("project_domain_name") or creds.get("user_domain_name"),
            }
        )
    return base


def payload_to_creds(payload: OpenStackCredentialUpsert) -> dict:
    """The plain dict ``connect_kwargs`` expects, from an upsert payload."""
    return {
        "auth_type": payload.auth_type.value,
        "auth_url": payload.auth_url,
        "region_name": payload.region_name,
        "interface": payload.interface,
        "identity_api_version": payload.identity_api_version,
        "project_id": payload.project_id,
        "project_name": payload.project_name,
        "user_domain_name": payload.user_domain_name,
        "project_domain_name": payload.project_domain_name,
        "identifier": payload.identifier,
        "secret": payload.secret,
    }


def validate(payload: OpenStackCredentialUpsert) -> Scope:
    """Get a project-scoped token; return its project or raise ``CredentialRejected``.

    Blocking network call (up to ``TIMEOUT_SECONDS`` per request). Nothing is
    sent unless the Keystone URL is allowed.
    """
    if settings.OPENSTACK_SIMULATE:
        return Scope(project_id="sim-project-001", project_name="Simulated Project")
    ensure_auth_url_allowed(payload.auth_url)
    try:
        conn = openstack.connect(api_timeout=TIMEOUT_SECONDS, **connect_kwargs(payload_to_creds(payload)))
        try:
            auth = conn.session.auth
            if not isinstance(auth, BaseIdentityPlugin):
                raise CredentialRejected("error")
            access = auth.get_access(conn.session)
        finally:
            conn.close()
    except CredentialRejected:
        raise
    except (os_exc.HttpException, ks_exc.HttpError) as e:
        status = getattr(e, "status_code", None) or getattr(e, "http_status", None)
        if status in (401, 403):
            raise CredentialRejected("invalid_credentials", status) from None
        if status == 404:
            raise CredentialRejected("project_not_found", status) from None
        raise CredentialRejected("rejected", status) from None
    except (ks_exc.ConnectionError, ks_exc.ConnectTimeout, TimeoutError, OSError):
        raise CredentialRejected("unreachable") from None
    except Exception:
        # Unknown SDK errors may quote the request body; say nothing more.
        raise CredentialRejected("error") from None
    project_id = getattr(access, "project_id", None)
    if not project_id:
        raise CredentialRejected("not_project_scoped")
    return Scope(project_id=project_id, project_name=getattr(access, "project_name", None))
