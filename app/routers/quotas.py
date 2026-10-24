"""Quota of the project behind one of the caller's OpenStack credentials.

Queries Nova, Cinder and Neutron live through ``services/openstack_client``
with the caller's own credential; nothing is stored. Ported from
dhbw-appstore.
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.services import openstack_client
from app.utils.keycloak_auth import get_current_user_keycloak

logger = logging.getLogger(__name__)

router = APIRouter()


class QuotaItem(BaseModel):
    """One quota: current usage, the project's limit (-1: unlimited) and what is
    left (may be negative when over quota; null when unlimited)."""

    used: int
    limit: int
    available: int | None
    unit: str | None = None


class ComputeQuotas(BaseModel):
    """Nova quotas; ``ram`` is in MB."""

    instances: QuotaItem
    vcpus: QuotaItem
    ram: QuotaItem


class StorageQuotas(BaseModel):
    """Cinder quotas; ``gigabytes`` sums the size of all volumes."""

    volumes: QuotaItem
    snapshots: QuotaItem
    gigabytes: QuotaItem


class NetworkQuotas(BaseModel):
    """Neutron quotas, counting only resources owned by the project."""

    floating_ips: QuotaItem
    security_groups: QuotaItem
    security_group_rules: QuotaItem
    networks: QuotaItem
    ports: QuotaItem
    routers: QuotaItem


class QuotaOverviewResponse(BaseModel):
    """Quota and usage of one OpenStack project, grouped by service."""

    compute: ComputeQuotas
    storage: StorageQuotas
    network: NetworkQuotas


def _quota_item(used: int, obj, attr: str, default: int, unit: str | None = None) -> QuotaItem:
    """Build a QuotaItem, reading the limit from ``obj.attr`` (``default`` if absent).

    ``available`` is ``limit - used`` and intentionally not clamped, so an
    over-quota project shows a negative value. A negative limit (-1 is
    unlimited in OpenStack) is passed through and has no ``available``:
    ``-1 - used`` would read as "over quota".
    """
    limit = getattr(obj, attr, default)
    available = None if limit < 0 else limit - used
    return QuotaItem(used=used, limit=limit, available=available, unit=unit)


@router.get("/me/openstack-credentials/{credential_id}/quota", response_model=QuotaOverviewResponse)
def get_project_quota(
    credential_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Quota and usage (compute, storage, network) of the credential's project.

    Asked with the caller's own credential; 404 for anyone else's, 502 when
    OpenStack does not answer. Declared sync on purpose: the SDK does
    blocking network I/O, and a sync def runs in Starlette's threadpool
    instead of stalling the event loop.
    """
    row = openstack_client.owned_credential(db, current_user, credential_id)
    with openstack_client.credential_connection(row) as conn:
        # The token's project: for an unverified credential the stored id
        # may be a typed one.
        project_id = conn.current_project_id
        compute_limits = conn.compute.get_quota_set(project_id)
        compute_usage = conn.compute.get_limits()
        compute = ComputeQuotas(
            instances=_quota_item(
                getattr(compute_usage.absolute, "total_instances_used", 0), compute_limits, "instances", 0
            ),
            vcpus=_quota_item(getattr(compute_usage.absolute, "total_cores_used", 0), compute_limits, "cores", 0),
            ram=_quota_item(getattr(compute_usage.absolute, "total_ram_used", 0), compute_limits, "ram", 0, unit="MB"),
        )

        volume_limits = conn.volume.get_quota_set(project_id)
        volumes = list(conn.volume.volumes())
        snapshots = list(conn.volume.snapshots())
        total_gb_used = sum(v.size for v in volumes)
        storage = StorageQuotas(
            volumes=_quota_item(len(volumes), volume_limits, "volumes", 0),
            snapshots=_quota_item(len(snapshots), volume_limits, "snapshots", 0),
            gigabytes=_quota_item(total_gb_used, volume_limits, "gigabytes", 0, unit="GB"),
        )

        # Only the project's own resources count against its quota; shared
        # and external networks would otherwise inflate the numbers.
        network_limits = conn.network.get_quota(project_id)
        own = {"project_id": project_id}
        security_groups = list(conn.network.security_groups(**own))
        network = NetworkQuotas(
            floating_ips=_quota_item(len(list(conn.network.ips(**own))), network_limits, "floatingip", 50),
            security_groups=_quota_item(len(security_groups), network_limits, "security_group", 10),
            security_group_rules=_quota_item(
                sum(len(list(conn.network.security_group_rules(security_group_id=sg.id))) for sg in security_groups),
                network_limits,
                "security_group_rule",
                100,
            ),
            networks=_quota_item(len(list(conn.network.networks(**own))), network_limits, "network", 100),
            ports=_quota_item(len(list(conn.network.ports(**own))), network_limits, "port", 500),
            routers=_quota_item(len(list(conn.network.routers(**own))), network_limits, "router", 10),
        )
        return QuotaOverviewResponse(compute=compute, storage=storage, network=network)
