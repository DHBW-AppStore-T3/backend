"""Parse a raw `clouds.yaml` blob into an `OpenStackCredentialUpsert`.

Used by the `/me/openstack-credentials/from-yaml` convenience endpoint so
users can paste/upload the file Horizon hands them instead of filling
the form manually. The parser is strict: anything weird gets a 422 with
a message the UI can render verbatim.
"""
from __future__ import annotations

import yaml
from fastapi import HTTPException, status

from app.models import OpenStackAuthType
from app.schemas import OpenStackCredentialUpsert
from app.utils import safe_yaml

_SUPPORTED_AUTH_TYPES = {
    "v3applicationcredential": OpenStackAuthType.APPLICATION_CREDENTIAL,
    "password": OpenStackAuthType.PASSWORD,
    # `clouds.yaml` from Horizon often omits `auth_type` for password;
    # treat the absence as password.
    None: OpenStackAuthType.PASSWORD,
}


def _bad(msg: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=msg)


def parse(clouds_yaml: str, cloud_name: str | None = None) -> OpenStackCredentialUpsert:
    try:
        doc = safe_yaml.load(clouds_yaml)
    except safe_yaml.UnsafeYaml as e:
        raise _bad(str(e))
    except yaml.YAMLError as e:
        raise _bad(f"Invalid YAML: {e}")

    if not isinstance(doc, dict) or "clouds" not in doc or not isinstance(doc["clouds"], dict):
        raise _bad("clouds.yaml must contain a top-level 'clouds:' mapping")

    clouds = doc["clouds"]
    if not clouds:
        raise _bad("clouds.yaml has no clouds defined")

    if cloud_name is None:
        if len(clouds) == 1:
            cloud_name = next(iter(clouds.keys()))
        else:
            names = ", ".join(sorted(clouds.keys()))
            raise _bad(f"Multiple clouds in YAML; specify cloud_name (one of: {names})")

    if cloud_name not in clouds:
        names = ", ".join(sorted(clouds.keys()))
        raise _bad(f"Cloud '{cloud_name}' not found in YAML (available: {names})")

    cloud = clouds[cloud_name]
    if not isinstance(cloud, dict):
        raise _bad(f"Cloud '{cloud_name}' is not a mapping")

    auth = cloud.get("auth")
    if not isinstance(auth, dict):
        raise _bad(f"Cloud '{cloud_name}' is missing 'auth:' block")

    raw_auth_type = cloud.get("auth_type")
    if raw_auth_type not in _SUPPORTED_AUTH_TYPES:
        raise _bad(
            f"Unsupported auth_type '{raw_auth_type}'. "
            f"Supported: v3applicationcredential, password"
        )
    auth_type = _SUPPORTED_AUTH_TYPES[raw_auth_type]

    auth_url = auth.get("auth_url")
    if not auth_url:
        raise _bad("auth.auth_url is required")

    common = {
        "auth_url": auth_url,
        "region_name": cloud.get("region_name"),
        "interface": cloud.get("interface", "public"),
        "identity_api_version": str(cloud.get("identity_api_version", "3")),
        "project_id": auth.get("project_id"),
        "project_name": auth.get("project_name"),
        "user_domain_name": auth.get("user_domain_name"),
        "project_domain_name": auth.get("project_domain_name"),
    }

    if auth_type == OpenStackAuthType.APPLICATION_CREDENTIAL:
        identifier = auth.get("application_credential_id")
        secret = auth.get("application_credential_secret")
        if not identifier or not secret:
            raise _bad(
                "Application credential requires application_credential_id "
                "and application_credential_secret"
            )
    else:
        identifier = auth.get("username")
        secret = auth.get("password")
        if not identifier or not secret:
            raise _bad("Password auth requires auth.username and auth.password")

    try:
        return OpenStackCredentialUpsert(
            auth_type=auth_type,
            identifier=identifier,
            secret=secret,
            **common,
        )
    except ValueError as e:
        raise _bad(str(e))
