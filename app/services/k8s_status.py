"""Live status of pod deployments, read from the cluster (.github#4).

The Infrastructure tab shows, for a deployment that runs as pods, one row per
workload: ready, restarts, phase, URL and the last warning. Nothing is cached
in the database; the namespace is the state. Ported from the pod MVP.
"""

from __future__ import annotations

import json
import logging
import re
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


# An app that runs a setup after it started (an installer) tells the platform how
# far it is with one log line: ``appstore-progress {"percent": 42, "step": "...",
# "etaSeconds": 600}``. Only this line is read from the log, nothing else.
PROGRESS_PREFIX = "appstore-progress "
_LOG_TAIL_LINES = 100
_LOG_LIMIT_BYTES = 64 * 1024
_MAX_SETUP_WORKLOADS = 20  # logs are read per running workload; bound the cost


def parse_progress(log: str) -> dict[str, Any] | None:
    """The last valid progress report in ``log``, or None.

    The text comes from the app: values are checked and cut, never trusted.
    """
    for line in reversed(log.splitlines()):
        line = line.strip()
        if not line.startswith(PROGRESS_PREFIX):
            continue
        try:
            data = json.loads(line[len(PROGRESS_PREFIX) :])
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        percent = data.get("percent")
        if (
            isinstance(percent, bool)
            or not isinstance(percent, (int, float))
            or not 0 <= percent <= 100
        ):
            continue
        step = data.get("step")
        eta = data.get("etaSeconds")
        return {
            "percent": int(percent),
            "step": re.sub(r"[\x00-\x1f]", " ", step)[:80]
            if isinstance(step, str) and step
            else None,
            "etaSeconds": int(eta)
            if isinstance(eta, (int, float)) and not isinstance(eta, bool) and 0 <= eta <= 7 * 86400
            else None,
        }
    return None


def _setup_progress(client: Kube, namespace: str, pod: str) -> dict[str, Any] | None:
    """The setup progress of a running pod, read from its log; None if there is none."""
    core = getattr(client, "_core", None)  # the real client only; the fake has no logs
    if core is None:
        return None
    try:
        log = core.read_namespaced_pod_log(
            pod, namespace, tail_lines=_LOG_TAIL_LINES, limit_bytes=_LOG_LIMIT_BYTES
        )
    except Exception:
        logger.debug("no log of %s/%s", namespace, pod, exc_info=True)
        return None
    return parse_progress(log) if isinstance(log, str) else None


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
    for index, s in enumerate(snap.statefulsets):
        phase, ready, restarts = _phase(snap, s.name, s.replicas)
        # Once the workload is ready, a warning from its start (a readiness probe that
        # failed before the server listened) is history; the page says it started.
        event = (
            None
            if ready
            else next(
                (f"{e.reason}: {e.message}" for e in reversed(snap.events) if s.name in e.object),
                None,
            )
        )
        setup = None
        if phase == "Running" and index < _MAX_SETUP_WORKLOADS:
            pod = next((p for p in snap.pods if p.workload == s.name), None)
            setup = _setup_progress(client, namespace, pod.name) if pod is not None else None
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
                "setup": setup if setup is not None and setup["percent"] < 100 else None,
            }
        )
    return rows
