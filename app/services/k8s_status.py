"""Live status of pod deployments, read from the cluster (.github#4).

The Infrastructure tab shows, for a deployment that runs as pods, one row per
workload: ready, restarts, phase, URL and the last warning. Nothing is cached
in the database; the namespace is the state. Ported from the pod MVP.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from app.config import settings
from app.k8s.kube import Kube, KubeApi, Snapshot
from app.k8s.render import DeploymentCtx

logger = logging.getLogger(__name__)

_factory: Callable[[], Kube] | None = None


def set_kube_factory(factory: Callable[[], Kube] | None) -> None:
    """Replace how the cluster connection is made (tests, local runs)."""
    global _factory
    _factory = factory


def kube() -> Kube | None:
    """A cluster connection, or None when the API may not read the cluster."""
    if _factory is not None:
        return _factory()
    if not settings.K8S_STATUS_ENABLED:
        return None
    return KubeApi()


def app_domain() -> str:
    return settings.K8S_APP_DOMAIN or (f"apps.{settings.K8S_ZONE}" if settings.K8S_ZONE else "")


def _phase(snap: Snapshot, workload: str, replicas: int) -> tuple[str, bool, int]:
    pods = [p for p in snap.pods if p.workload == workload]
    if replicas == 0 and not pods:
        return "Stopped", False, 0
    if not pods:
        return "Pending", False, 0
    pod = pods[0]
    if pod.unschedulable:
        return "Unschedulable", False, pod.restarts
    return pod.waiting_reason or pod.phase, pod.ready, pod.restarts


def workload_views(deployment_id, namespace: str) -> list[dict[str, Any]] | None:
    """Rows for the Infrastructure tab, or None when the cluster cannot be asked.

    A namespace that was not created for this deployment shows nothing.
    """
    client = kube()
    if client is None or not namespace:
        return None
    try:
        snap = client.snapshot(namespace)
    except Exception:
        logger.exception("could not read namespace %s", namespace)
        return None
    if not snap.exists:
        return []
    if not snap.belongs_to(str(deployment_id)):
        logger.warning("namespace %s does not belong to deployment %s", namespace, deployment_id)
        return []
    dep = DeploymentCtx(id=str(deployment_id))
    domain = app_domain()
    rows = []
    for s in snap.statefulsets:
        phase, ready, restarts = _phase(snap, s.name, s.replicas)
        event = next((f"{e.reason}: {e.message}" for e in reversed(snap.events) if s.name in e.object), None)
        rows.append(
            {
                "workload": s.name,
                "team": s.team or None,
                "user_id": s.user or None,
                "ready": ready,
                "restarts": restarts,
                "phase": phase,
                "url": f"https://{s.name}-{dep.short_id}.{domain}" if domain else None,
                "lastEvent": event,
            }
        )
    return rows
