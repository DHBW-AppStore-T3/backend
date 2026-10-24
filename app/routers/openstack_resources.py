"""
Read API for the OpenStack resources of one of the caller's credentials.

Used by the wizard's resource picker (``@openstack:`` markers, plan E4) so
the user no longer has to copy UUIDs from the Horizon dashboard. Mounted
under ``/me/openstack-credentials/{credential_id}/resources``: the picker
shows the project the deployment will be created in. Each endpoint:

- Authenticates the caller and checks the credential is theirs (404 otherwise)
- Obtains an OpenStack connection with that credential (per-request)
- Caches the response 60 s process-locally (see ``services/openstack_client``)
- Reduces the SDK object to a flat dict — only the fields that the
  frontend needs for display + selection. We do not want to leak SDK
  structure (sensitive fields, unwanted size).

Error strategy: 502 for OpenStack-side failures (no 500 — that is
reserved for "backend bug"). The frontend then renders a banner
"OpenStack not reachable, enter ID manually".
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import User, UserOpenStackCredential
from app.schemas import (
    AvailabilityZoneItem,
    DescribedItem,
    FlavorItem,
    ImageItem,
    KeypairItem,
    NetworkItem,
    RouterItem,
    SubnetItem,
    VolumeItem,
)
from app.services import openstack_client
from app.utils.keycloak_auth import get_current_user_keycloak

logger = logging.getLogger(__name__)

router = APIRouter()

# Static data returned for every resource endpoint when OPENSTACK_SIMULATE=true.
# Enough for the wizard to work without any real cloud.
_SIMULATE_RESOURCES: dict[str, list[dict]] = {
    "networks": [
        {"id": "net-sim-001", "name": "sim-network", "description": "Simulated network", "shared": False, "external": False, "status": "ACTIVE"},
        {"id": "net-sim-ext", "name": "ext-net", "description": "External network", "shared": True, "external": True, "status": "ACTIVE"},
    ],
    "subnets": [
        {"id": "sub-sim-001", "name": "sim-subnet", "cidr": "192.168.1.0/24", "ip_version": 4, "network_id": "net-sim-001", "gateway_ip": "192.168.1.1"},
    ],
    "flavors": [
        {"id": "flv-small", "name": "sim.small", "vcpus": 1, "ram": 1024, "disk": 10, "is_public": True},
        {"id": "flv-medium", "name": "sim.medium", "vcpus": 2, "ram": 4096, "disk": 20, "is_public": True},
        {"id": "flv-large", "name": "sim.large", "vcpus": 4, "ram": 8192, "disk": 50, "is_public": True},
    ],
    "images": [
        {"id": "img-ubuntu", "name": "Ubuntu 22.04", "status": "active", "visibility": "public", "size": 2000000000, "disk_format": "qcow2"},
        {"id": "img-debian", "name": "Debian 12", "status": "active", "visibility": "public", "size": 1500000000, "disk_format": "qcow2"},
    ],
    "keypairs": [
        {"name": "sim-keypair", "fingerprint": "aa:bb:cc:dd:ee:ff", "type": "ssh", "id": "sim-keypair"},
    ],
    "security_groups": [
        {"id": "sg-default", "name": "default", "description": "Default security group"},
        {"id": "sg-web", "name": "web", "description": "HTTP/HTTPS access"},
    ],
    "floating_ip_pools": [
        {"id": "net-sim-ext", "name": "ext-net", "description": "External network"},
    ],
    "volumes": [],
    "routers": [
        {"id": "rtr-sim-001", "name": "sim-router", "status": "ACTIVE", "external_gateway_info": None},
    ],
    "availability_zones_compute": [
        {"id": "nova", "name": "nova", "state": "available"},
    ],
    "availability_zones_network": [
        {"id": "nova", "name": "nova", "state": "available"},
    ],
    "availability_zones_volume": [
        {"id": "nova", "name": "nova", "state": "available"},
    ],
}


# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------
def _safe_get(obj: Any, *names: str, default: Any = None) -> Any:
    """Return the first non-None attribute of ``obj`` among ``names``, else ``default``.

    SDK objects are partly dict-like, partly property objects, and the same
    field has different names across API versions (``is_shared``/``shared``);
    properties that raise on access are skipped.
    """
    for n in names:
        try:
            v = getattr(obj, n, None)
            if v is not None:
                return v
        except Exception:  # noqa: BLE001 — some properties raise lazily
            continue
    return default


def _credential(
    credential_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
) -> UserOpenStackCredential:
    """Dependency: the caller's credential from the path; 404 for anyone else's."""
    return openstack_client.owned_credential(db, current_user, credential_id)


def _list_with_oserror(
    credential: UserOpenStackCredential,
    kind: str,
    filters: dict | None,
    fetch_fn,
) -> list[dict]:
    """Run ``fetch_fn`` through the per-credential TTL cache under key ``(kind, filters)``.

    HTTPExceptions (e.g. 502 ``openstack_unavailable`` from the connection,
    400 from a fetch) pass through; any other error becomes 502
    ``openstack_list_failed`` with the ``kind``.
    """
    if settings.OPENSTACK_SIMULATE:
        return _SIMULATE_RESOURCES.get(kind, [])
    try:
        return openstack_client.cached_list(
            credential_id=credential.credentialId,
            kind=kind,
            filters=filters,
            fetch=fetch_fn,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "OpenStack list %s failed for credential %s: %s", kind, credential.credentialId, exc
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"reason": "openstack_list_failed", "kind": kind},
        ) from None


# ----------------------------------------------------------------
# Cache-Refresh
# ----------------------------------------------------------------
# ``kind`` as the UI knows it (the ``@openstack:<kind>`` marker vocabulary)
# → the cache kind the list endpoints below store under. Availability zones
# are cached per service (``availability_zones_compute`` …), so their entry
# is a prefix.
_REFRESH_KINDS: dict[str, str] = {
    "network": "networks",
    "subnet": "subnets",
    "flavor": "flavors",
    "image": "images",
    "keypair": "keypairs",
    "security_group": "security_groups",
    "floating_ip_pool": "floating_ip_pools",
    "volume": "volumes",
    "router": "routers",
    "availability_zone": "availability_zones",
}


@router.post("/refresh", status_code=status.HTTP_204_NO_CONTENT)
def refresh_resource_cache(
    kind: str | None = Query(
        default=None,
        description="Only invalidate this kind: a marker kind such as network or security_group",
    ),
    credential: UserOpenStackCredential = Depends(_credential),
):
    """Drop the cached resource lists of this credential (204).

    Triggered by the refresh button next to a wizard picker — the user has
    just created a resource in Horizon and wants to see it. With ``kind``
    only that kind is dropped (the ``@openstack:<kind>`` marker names, e.g.
    ``network``, ``security_group``, ``availability_zone``); 422 for an
    unknown one. 404 for a credential that is not the caller's.
    """
    if settings.OPENSTACK_SIMULATE:
        return None
    cache_kind = None
    if kind is not None:
        cache_kind = _REFRESH_KINDS.get(kind)
        if cache_kind is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"reason": "unknown_resource_kind", "kind": kind},
            )
    removed = openstack_client.invalidate(credential.credentialId, cache_kind)
    logger.info("Cache invalidated for credential %s (kind=%s, %d entries removed)",
                credential.credentialId, kind, removed)
    return None


# ----------------------------------------------------------------
# Networks
# ----------------------------------------------------------------
@router.get("/networks", response_model=list[NetworkItem])
def list_networks(
    credential: UserOpenStackCredential = Depends(_credential),
):
    """List all networks visible to the credential's project.

    ``shared`` and ``external`` are included so the frontend can render
    "External Network" hints. Like every endpoint here: 404 for a credential
    that is not the caller's, 502 when OpenStack fails.
    """
    def fetch() -> list[dict]:
        with openstack_client.credential_connection(credential) as conn:
            return [
                {
                    "id": _safe_get(n, "id"),
                    "name": _safe_get(n, "name") or "",
                    "description": _safe_get(n, "description") or "",
                    "shared": bool(_safe_get(n, "is_shared", "shared", default=False)),
                    "external": bool(_safe_get(n, "is_router_external", "router:external", default=False)),
                    "status": _safe_get(n, "status") or "",
                }
                for n in conn.network.networks()
            ]

    return _list_with_oserror(credential, "networks", None, fetch)


# ----------------------------------------------------------------
# Subnets — optionally filtered by network
# ----------------------------------------------------------------
@router.get("/subnets", response_model=list[SubnetItem])
def list_subnets(
    network_id: str | None = Query(default=None, description="Only subnets of this network"),
    credential: UserOpenStackCredential = Depends(_credential),
):
    """List subnets, optionally only those of ``network_id``.

    The filter is passed to the OpenStack API, and each filter value is
    cached under its own key next to the unfiltered list.
    """
    filters: dict[str, Any] = {}
    if network_id:
        filters["network_id"] = network_id

    def fetch() -> list[dict]:
        with openstack_client.credential_connection(credential) as conn:
            kwargs: dict[str, Any] = {}
            if network_id:
                kwargs["network_id"] = network_id
            return [
                {
                    "id": _safe_get(s, "id"),
                    "name": _safe_get(s, "name") or "",
                    "cidr": _safe_get(s, "cidr") or "",
                    "ip_version": _safe_get(s, "ip_version", default=4),
                    "network_id": _safe_get(s, "network_id"),
                    "gateway_ip": _safe_get(s, "gateway_ip") or "",
                }
                for s in conn.network.subnets(**kwargs)
            ]

    return _list_with_oserror(credential, "subnets", filters or None, fetch)


# ----------------------------------------------------------------
# Flavors
# ----------------------------------------------------------------
@router.get("/flavors", response_model=list[FlavorItem])
def list_flavors(
    credential: UserOpenStackCredential = Depends(_credential),
):
    """List compute flavors with vCPUs, RAM (MB) and disk (GB).

    Private flavors (``is_public=false``) are included; the frontend can
    render a note.
    """
    def fetch() -> list[dict]:
        with openstack_client.credential_connection(credential) as conn:
            out: list[dict] = []
            for f in conn.compute.flavors(get_extra_specs=False):
                out.append({
                    "id": _safe_get(f, "id"),
                    "name": _safe_get(f, "name") or "",
                    "vcpus": _safe_get(f, "vcpus", default=0) or 0,
                    "ram": _safe_get(f, "ram", default=0) or 0,        # MB
                    "disk": _safe_get(f, "disk", default=0) or 0,      # GB
                    "is_public": bool(_safe_get(f, "is_public", default=True)),
                })
            return out

    return _list_with_oserror(credential, "flavors", None, fetch)


# ----------------------------------------------------------------
# Images
# ----------------------------------------------------------------
@router.get("/images", response_model=list[ImageItem])
def list_images(
    status_filter: str = Query(
        default="active",
        alias="status",
        description="Image status to list (default: active); 'all' for every status",
    ),
    credential: UserOpenStackCredential = Depends(_credential),
):
    """List Glance images visible to the project, by default only ``active`` ones.

    Default ``status=active`` keeps "queued" or "deleted" images out of the
    picker; ``status=all`` returns every image. ``size`` is in bytes.
    """
    filters = {"status": status_filter}

    def fetch() -> list[dict]:
        with openstack_client.credential_connection(credential) as conn:
            kwargs: dict[str, Any] = {}
            if status_filter and status_filter != "all":
                kwargs["status"] = status_filter
            out: list[dict] = []
            for img in conn.image.images(**kwargs):
                out.append({
                    "id": _safe_get(img, "id"),
                    "name": _safe_get(img, "name") or "",
                    "status": _safe_get(img, "status") or "",
                    "visibility": _safe_get(img, "visibility") or "",
                    "size": _safe_get(img, "size") or 0,         # bytes
                    "disk_format": _safe_get(img, "disk_format") or "",
                })
            return out

    return _list_with_oserror(credential, "images", filters, fetch)


# ----------------------------------------------------------------
# Keypairs
# ----------------------------------------------------------------
@router.get("/keypairs", response_model=list[KeypairItem])
def list_keypairs(
    credential: UserOpenStackCredential = Depends(_credential),
):
    """List the SSH keypairs of the credential's OpenStack user.

    Nova keypairs belong to the user, not the project, and Terraform modules
    reference them by ``name``; ``id`` therefore repeats the name.
    """
    def fetch() -> list[dict]:
        with openstack_client.credential_connection(credential) as conn:
            return [
                {
                    "name": _safe_get(k, "name") or "",
                    "fingerprint": _safe_get(k, "fingerprint") or "",
                    "type": _safe_get(k, "type") or "ssh",
                    # ``id`` equals the name for a keypair — we duplicate this
                    # intentionally so the picker can uniformly read ``id``.
                    "id": _safe_get(k, "name") or "",
                }
                for k in conn.compute.keypairs()
            ]

    return _list_with_oserror(credential, "keypairs", None, fetch)


# ----------------------------------------------------------------
# Security Groups
# ----------------------------------------------------------------
@router.get("/security-groups", response_model=list[DescribedItem])
def list_security_groups(
    credential: UserOpenStackCredential = Depends(_credential),
):
    """List the security groups visible to the credential's project."""
    def fetch() -> list[dict]:
        with openstack_client.credential_connection(credential) as conn:
            return [
                {
                    "id": _safe_get(sg, "id"),
                    "name": _safe_get(sg, "name") or "",
                    "description": _safe_get(sg, "description") or "",
                }
                for sg in conn.network.security_groups()
            ]

    return _list_with_oserror(credential, "security_groups", None, fetch)


# ----------------------------------------------------------------
# Floating IP Pools (External Networks)
# ----------------------------------------------------------------
@router.get("/floating-ip-pools", response_model=list[DescribedItem])
def list_floating_ip_pools(
    credential: UserOpenStackCredential = Depends(_credential),
):
    """List the floating IP pools, i.e. the external networks.

    There is no dedicated ``Pool`` resource in OpenStack — pools are
    networks with ``router:external = true``. Terraform modules usually
    expect the **name** of the external network.
    """
    def fetch() -> list[dict]:
        with openstack_client.credential_connection(credential) as conn:
            out: list[dict] = []
            for n in conn.network.networks():
                is_ext = bool(_safe_get(n, "is_router_external", "router:external", default=False))
                if not is_ext:
                    continue
                out.append({
                    "id": _safe_get(n, "id"),
                    "name": _safe_get(n, "name") or "",
                    "description": _safe_get(n, "description") or "",
                })
            return out

    return _list_with_oserror(credential, "floating_ip_pools", None, fetch)


# ----------------------------------------------------------------
# Volumes
# ----------------------------------------------------------------
@router.get("/volumes", response_model=list[VolumeItem])
def list_volumes(
    credential: UserOpenStackCredential = Depends(_credential),
):
    """List the project's Cinder volumes in every status (``size`` in GB).

    The frontend filters ``status`` itself if needed; ``in-use`` volumes are
    included because a multi-attach volume can take a second instance.
    """
    def fetch() -> list[dict]:
        with openstack_client.credential_connection(credential) as conn:
            return [
                {
                    "id": _safe_get(v, "id"),
                    "name": _safe_get(v, "name") or "",
                    "size": _safe_get(v, "size") or 0,            # GB
                    "status": _safe_get(v, "status") or "",
                    "volume_type": _safe_get(v, "volume_type") or "",
                    "bootable": bool(_safe_get(v, "is_bootable", "bootable", default=False)),
                }
                for v in conn.volume.volumes()
            ]

    return _list_with_oserror(credential, "volumes", None, fetch)


# ----------------------------------------------------------------
# Routers
# ----------------------------------------------------------------
@router.get("/routers", response_model=list[RouterItem])
def list_routers(
    credential: UserOpenStackCredential = Depends(_credential),
):
    """List the routers visible to the project, with their external gateway info."""
    def fetch() -> list[dict]:
        with openstack_client.credential_connection(credential) as conn:
            return [
                {
                    "id": _safe_get(r, "id"),
                    "name": _safe_get(r, "name") or "",
                    "status": _safe_get(r, "status") or "",
                    "external_gateway_info": _safe_get(r, "external_gateway_info") or None,
                }
                for r in conn.network.routers()
            ]

    return _list_with_oserror(credential, "routers", None, fetch)


# ----------------------------------------------------------------
# Availability Zones
# ----------------------------------------------------------------
@router.get("/availability-zones", response_model=list[AvailabilityZoneItem])
def list_availability_zones(
    service: str = Query(
        default="compute",
        description="OpenStack service: compute (Nova), network (Neutron) or volume (Cinder)",
    ),
    credential: UserOpenStackCredential = Depends(_credential),
):
    """List the availability zones of one service; the zone name doubles as ``id``.

    AZs differ per service. Default: compute (Nova), the common case (VM
    placement). 400 for a ``service`` other than compute, network or volume.
    """
    filters = {"service": service}

    def fetch() -> list[dict]:
        with openstack_client.credential_connection(credential) as conn:
            if service == "compute":
                source = conn.compute.availability_zones()
            elif service == "network":
                source = conn.network.availability_zones()
            elif service == "volume":
                source = conn.volume.availability_zones()
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Unknown service '{service}' (compute|network|volume)",
                )
            out: list[dict] = []
            for az in source:
                name = _safe_get(az, "name") or ""
                if not name:
                    continue
                out.append({
                    # AZs have no UUID — the name IS the ID.
                    "id": name,
                    "name": name,
                    "state": _safe_get(az, "state", "zoneState") or "",
                })
            return out

    return _list_with_oserror(credential, f"availability_zones_{service}", filters, fetch)
