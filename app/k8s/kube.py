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

import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from app.k8s.render import (
    DEPLOYMENT_LABEL,
    ENVIRONMENT_LABEL,
    FIELD_MANAGER,
    MANAGED_BY_LABEL,
    TEAM_LABEL,
    USER_LABEL,
    VM_NAMESPACE_LABEL,
    WORKLOAD_LABEL,
    label_value,
)

if TYPE_CHECKING:
    from datetime import datetime

# Seconds a call to the API server may take to connect and to answer. The
# client has no default, so a connection the network dropped without a reset
# would block its caller for good: a job of the worker or a request of the API.
REQUEST_TIMEOUT = (10, 60)


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
    # When a running container of the pod last started, and since when the pod
    # is not Ready (the Ready condition's last change); None when unknown.
    # Together they tell an app that is still starting from one that answered
    # and stopped answering.
    started_at: datetime | None = None
    not_ready_since: datetime | None = None


@dataclass
class StatefulSetInfo:
    name: str
    replicas: int
    ready_replicas: int
    team: str = ""
    user: str = ""  # the member's user id (``scope: user``)


@dataclass
class DeploymentInfo:
    name: str
    replicas: int
    ready_replicas: int


@dataclass
class VmiInfo:
    """A running KubeVirt VirtualMachineInstance (``phase``: Pending, Scheduling, Running, Failed, ...)."""

    name: str
    phase: str = "Pending"
    ready: bool = False
    team: str = ""
    user: str = ""  # the member's user id (``scope: user``)


@dataclass
class VmInfo:
    """A KubeVirt VirtualMachine (the desired state; its VMI exists only while it runs)."""

    name: str
    run_strategy: str = "Always"  # Always, Halted, ...


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
    # kubevirt-vm namespaces: the noVNC proxies and the VMs.
    deployments: list[DeploymentInfo] = field(default_factory=list)
    vmis: list[VmiInfo] = field(default_factory=list)
    # Names of the volume claims, with the workload each belongs to.
    pvcs: list[tuple[str, str]] = field(default_factory=list)
    # kubevirt-vm namespaces: the VirtualMachines themselves (also while halted).
    virtual_machines: list[VmInfo] = field(default_factory=list)
    # Phase per volume claim (Pending, Bound, ...) and the Warning events of the claims
    # as (claim name, reason, message): a stuck clone shows up here, not on a pod.
    pvc_phases: dict[str, str] = field(default_factory=dict)
    pvc_issues: list[tuple[str, str, str]] = field(default_factory=list)
    pods: list[PodInfo] = field(default_factory=list)
    events: list[EventInfo] = field(default_factory=list)

    def belongs_to(self, deployment_id: str) -> bool:
        """Whether the namespace was created for ``deployment_id`` (by this platform)."""
        return self.labels.get(DEPLOYMENT_LABEL) == deployment_id and self.labels.get(MANAGED_BY_LABEL) == FIELD_MANAGER


class Kube(Protocol):
    def apply(self, obj: dict[str, Any]) -> None: ...
    def delete_object(self, api_version: str, kind: str, namespace: str, name: str) -> None: ...
    def delete_namespace(self, name: str) -> None: ...
    def namespaces(self, label_selector: str) -> list[str]: ...
    def snapshot(self, namespace: str) -> Snapshot: ...
    def scale(self, namespace: str, statefulset: str, replicas: int) -> None: ...
    def scale_deployment(self, namespace: str, name: str, replicas: int) -> None: ...
    def set_run_strategy(self, namespace: str, vm_name: str, run_strategy: str) -> None: ...
    def delete_pod(self, namespace: str, pod: str) -> None: ...
    def delete_pvcs(self, namespace: str, workload: str) -> None: ...


class KubeApi:
    """The real thing: in-cluster service account, or ``KUBECONFIG`` for development."""

    def __init__(self) -> None:
        from kubernetes import client, config, dynamic

        class ApiClient(client.ApiClient):
            """Every request, typed or dynamic, goes through ``call_api``: it gets ``REQUEST_TIMEOUT``."""

            def call_api(self, *args: Any, _request_timeout: Any = None, **kwargs: Any) -> Any:
                # ``_request_timeout`` is keyword-only here, so *args cannot fill it (the kubernetes stub's positional
                # parameter list makes mypy think it can).
                return super().call_api(  # type: ignore[misc]
                    *args, _request_timeout=_request_timeout or REQUEST_TIMEOUT, **kwargs
                )

        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        api = ApiClient()
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

    def delete_object(self, api_version: str, kind: str, namespace: str, name: str) -> None:
        from kubernetes.dynamic.exceptions import NotFoundError

        resource = self._dyn.resources.get(api_version=api_version, kind=kind)
        with contextlib.suppress(NotFoundError):
            self._dyn.delete(resource, name=name, namespace=namespace)

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
        if snap.labels.get(VM_NAMESPACE_LABEL) == "true":
            for d in self._apps.list_namespaced_deployment(namespace).items:
                snap.deployments.append(
                    DeploymentInfo(d.metadata.name, d.spec.replicas or 0, d.status.ready_replicas or 0)
                )
            snap.vmis.extend(self._vmis(namespace))
            snap.virtual_machines.extend(self._virtual_machines(namespace))
        for c in self._core.list_namespaced_persistent_volume_claim(namespace).items:
            snap.pvcs.append((c.metadata.name, (c.metadata.labels or {}).get(WORKLOAD_LABEL, "")))
            snap.pvc_phases[c.metadata.name] = (c.status.phase if c.status else None) or "Pending"
        for p in self._core.list_namespaced_pod(namespace).items:
            snap.pods.append(_pod_info(p))
        for ev in self._core.list_namespaced_event(namespace).items:
            if ev.type == "Warning":
                if ev.involved_object.kind == "PersistentVolumeClaim":
                    snap.pvc_issues.append((ev.involved_object.name, ev.reason or "", ev.message or ""))
                snap.events.append(
                    EventInfo(
                        f"{ev.involved_object.kind}/{ev.involved_object.name}",
                        ev.reason or "",
                        ev.message or "",
                    )
                )
        return snap

    def _vmis(self, namespace: str) -> list[VmiInfo]:
        resource = self._dyn.resources.get(api_version="kubevirt.io/v1", kind="VirtualMachineInstance")
        out = []
        for item in resource.get(namespace=namespace).items:
            status = item.get("status") or {}
            conditions = status.get("conditions") or []
            ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
            labels = item["metadata"].get("labels") or {}
            out.append(
                VmiInfo(
                    item["metadata"]["name"],
                    status.get("phase") or "Pending",
                    ready,
                    labels.get(TEAM_LABEL, ""),
                    labels.get(USER_LABEL, ""),
                )
            )
        return out

    def _virtual_machines(self, namespace: str) -> list[VmInfo]:
        resource = self._dyn.resources.get(api_version="kubevirt.io/v1", kind="VirtualMachine")
        return [
            VmInfo(item["metadata"]["name"], (item.get("spec") or {}).get("runStrategy") or "Always")
            for item in resource.get(namespace=namespace).items
        ]

    def scale_deployment(self, namespace: str, name: str, replicas: int) -> None:
        # The deployer may patch deployments but has no deployments/scale right.
        self._apps.patch_namespaced_deployment(name, namespace, {"spec": {"replicas": replicas}})

    def set_run_strategy(self, namespace: str, vm_name: str, run_strategy: str) -> None:
        """Merge patch of ``spec.runStrategy`` (Halted stops the VM, Always starts it)."""
        resource = self._dyn.resources.get(api_version="kubevirt.io/v1", kind="VirtualMachine")
        self._dyn.patch(
            resource,
            body={"spec": {"runStrategy": run_strategy}},
            name=vm_name,
            namespace=namespace,
            content_type="application/merge-patch+json",
        )

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
        running = c.state.running if c.state else None
        if running and running.started_at and (info.started_at is None or running.started_at > info.started_at):
            info.started_at = running.started_at
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
        if cond.type == "Ready" and cond.status != "True":
            info.not_ready_since = cond.last_transition_time
    return info


def deployment_selector(deployment_id: str) -> str:
    return f"{DEPLOYMENT_LABEL}={deployment_id}"


def managed_selector(environment: str = "") -> str:
    """Every namespace this platform created; with ``environment``, only that installation's."""
    selector = f"{MANAGED_BY_LABEL}={FIELD_MANAGER}"
    if environment:
        selector += f",{ENVIRONMENT_LABEL}={label_value(environment)}"
    return selector
