"""A thin wrapper around the Kubernetes API for exactly what the AppStore needs.

The pod worker applies and deletes deployments through it, the API reads their
status. Everything else talks to :class:`KubeApi`, never to the ``kubernetes``
package, so tests swap in :class:`~app.k8s.fake.FakeKube` and no cluster is
needed.

Objects are applied with Server-Side Apply (field manager ``appstore``), so
re-applying is idempotent and only the fields the platform sets are owned by it.
Identical in backend and worker (``test_shared_files``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.k8s.render import (
    DEPLOYMENT_LABEL,
    ENVIRONMENT_LABEL,
    FIELD_MANAGER,
    MANAGED_BY_LABEL,
    TEAM_LABEL,
    USER_LABEL,
    WORKLOAD_LABEL,
    label_value,
)


@dataclass
class PodInfo:
    name: str
    workload: str
    phase: str = "Pending"
    ready: bool = False
    restarts: int = 0
    # Why a container is not running: ImagePullBackOff, CrashLoopBackOff, ...
    waiting_reason: str | None = None
    message: str | None = None
    unschedulable: bool = False
    # Changes when the pod is replaced; a StatefulSet reuses the name.
    uid: str = ""


@dataclass
class StatefulSetInfo:
    name: str
    replicas: int
    ready_replicas: int
    team: str = ""
    user: str = ""  # the member's user id (``scope: user``)


@dataclass
class EventInfo:
    object: str
    reason: str
    message: str
    type: str = "Warning"


@dataclass
class Snapshot:
    """What the cluster holds for one namespace."""

    exists: bool
    terminating: bool = False
    # The namespace's labels; ``DEPLOYMENT_LABEL`` says whose it is.
    labels: dict[str, str] = field(default_factory=dict)
    statefulsets: list[StatefulSetInfo] = field(default_factory=list)
    # Names of the volume claims, with the workload each belongs to.
    pvcs: list[tuple[str, str]] = field(default_factory=list)
    pods: list[PodInfo] = field(default_factory=list)
    events: list[EventInfo] = field(default_factory=list)

    def belongs_to(self, deployment_id: str) -> bool:
        """Whether the namespace was created for ``deployment_id`` (by this platform)."""
        return self.labels.get(DEPLOYMENT_LABEL) == deployment_id and self.labels.get(MANAGED_BY_LABEL) == FIELD_MANAGER


class Kube(Protocol):
    def apply(self, obj: dict[str, Any]) -> None: ...
    def delete_namespace(self, name: str) -> None: ...
    def namespaces(self, label_selector: str) -> list[str]: ...
    def snapshot(self, namespace: str) -> Snapshot: ...
    def scale(self, namespace: str, statefulset: str, replicas: int) -> None: ...
    def delete_pod(self, namespace: str, pod: str) -> None: ...
    def delete_pvcs(self, namespace: str, workload: str) -> None: ...


class KubeApi:
    """The real thing: in-cluster service account, or ``KUBECONFIG`` for development."""

    def __init__(self) -> None:
        from kubernetes import client, config, dynamic

        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        api = client.ApiClient()
        # The generated client is untyped for mypy's purposes; keep it behind Any.
        self._dyn: Any = dynamic.DynamicClient(api)
        self._core: Any = client.CoreV1Api(api)
        self._apps: Any = client.AppsV1Api(api)

    def apply(self, obj: dict[str, Any]) -> None:
        resource = self._dyn.resources.get(api_version=obj["apiVersion"], kind=obj["kind"])
        meta = obj["metadata"]
        self._dyn.server_side_apply(
            resource,
            body=obj,
            name=meta["name"],
            namespace=meta.get("namespace"),
            field_manager=FIELD_MANAGER,
            force_conflicts=True,
        )

    def delete_namespace(self, name: str) -> None:
        from kubernetes.client.rest import ApiException

        try:
            self._core.delete_namespace(name)
        except ApiException as e:
            if e.status != 404:
                raise

    def namespaces(self, label_selector: str) -> list[str]:
        return [n.metadata.name for n in self._core.list_namespace(label_selector=label_selector).items]

    def snapshot(self, namespace: str) -> Snapshot:
        from kubernetes.client.rest import ApiException

        try:
            ns = self._core.read_namespace(namespace)
        except ApiException as e:
            if e.status == 404:
                return Snapshot(exists=False)
            raise
        snap = Snapshot(
            exists=True,
            terminating=ns.status.phase == "Terminating",
            labels=dict(ns.metadata.labels or {}),
        )
        if snap.terminating:
            return snap
        for s in self._apps.list_namespaced_stateful_set(namespace).items:
            labels = s.metadata.labels or {}
            snap.statefulsets.append(
                StatefulSetInfo(
                    s.metadata.name,
                    s.spec.replicas or 0,
                    s.status.ready_replicas or 0,
                    labels.get(TEAM_LABEL, ""),
                    labels.get(USER_LABEL, ""),
                )
            )
        for c in self._core.list_namespaced_persistent_volume_claim(namespace).items:
            snap.pvcs.append((c.metadata.name, (c.metadata.labels or {}).get(WORKLOAD_LABEL, "")))
        for p in self._core.list_namespaced_pod(namespace).items:
            snap.pods.append(_pod_info(p))
        for ev in self._core.list_namespaced_event(namespace).items:
            if ev.type == "Warning":
                snap.events.append(
                    EventInfo(f"{ev.involved_object.kind}/{ev.involved_object.name}", ev.reason or "", ev.message or "")
                )
        return snap

    def scale(self, namespace: str, statefulset: str, replicas: int) -> None:
        self._apps.patch_namespaced_stateful_set_scale(statefulset, namespace, {"spec": {"replicas": replicas}})

    def delete_pod(self, namespace: str, pod: str) -> None:
        from kubernetes.client.rest import ApiException

        try:
            self._core.delete_namespaced_pod(pod, namespace)
        except ApiException as e:
            if e.status != 404:
                raise

    def delete_pvcs(self, namespace: str, workload: str) -> None:
        self._core.delete_collection_namespaced_persistent_volume_claim(
            namespace, label_selector=f"{WORKLOAD_LABEL}={workload}"
        )


def _pod_info(p: Any) -> PodInfo:
    info = PodInfo(
        name=p.metadata.name,
        workload=(p.metadata.labels or {}).get(WORKLOAD_LABEL, ""),
        phase=p.status.phase or "Pending",
        uid=p.metadata.uid or "",
    )
    for c in p.status.container_statuses or []:
        info.restarts += c.restart_count or 0
        if c.state and c.state.waiting:
            info.waiting_reason = c.state.waiting.reason
            info.message = c.state.waiting.message
        if c.state and c.state.terminated and c.state.terminated.reason != "Completed":
            info.waiting_reason = c.state.terminated.reason
    info.ready = any(cond.type == "Ready" and cond.status == "True" for cond in p.status.conditions or [])
    for cond in p.status.conditions or []:
        if cond.type == "PodScheduled" and cond.status == "False" and cond.reason == "Unschedulable":
            info.unschedulable = True
            info.message = cond.message
    return info


def deployment_selector(deployment_id: str) -> str:
    return f"{DEPLOYMENT_LABEL}={deployment_id}"


def managed_selector(environment: str = "") -> str:
    """Every namespace this platform created; with ``environment``, only that installation's."""
    selector = f"{MANAGED_BY_LABEL}={FIELD_MANAGER}"
    if environment:
        selector += f",{ENVIRONMENT_LABEL}={label_value(environment)}"
    return selector
