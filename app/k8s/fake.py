"""An in-memory cluster for tests: stands in for :class:`app.k8s.kube.KubeApi`.

It does what the real cluster does that the jobs depend on: applied
StatefulSets get pods, scaling adds or removes them, deleting a namespace
removes everything in it (after ``delete_polls`` polls, like finalizers), and
pods can be made to fail with a given reason. In a kubevirt-vm namespace a VirtualMachine
has a ``runStrategy`` (``Halted`` removes its VMI and pod after ``stop_polls`` polls) and a
noVNC proxy Deployment can be scaled (0 removes its pod). Identical in backend and worker
(``test_shared_files``); also the stand-in for a local run without a cluster.
"""

from __future__ import annotations

import copy
from typing import Any

from app.k8s.kube import DeploymentInfo, EventInfo, PodInfo, Snapshot, StatefulSetInfo, VmiInfo, VmInfo
from app.k8s.render import DEPLOYMENT_LABEL, TEAM_LABEL, USER_LABEL, VM_NAMESPACE_LABEL, WORKLOAD_LABEL


class FakeKube:
    def __init__(
        self,
        *,
        fail_reason: str | None = None,
        ready_after_polls: int = 1,
        delete_polls: int = 1,
        stop_polls: int = 0,
    ):
        self.objects: dict[tuple[str, str, str | None, str], dict[str, Any]] = {}
        self.replicas: dict[tuple[str, str], int] = {}
        self.deleted_pods: list[str] = []
        self.deleted_pvcs: list[tuple[str, str]] = []
        self.pvc_delete_polls = 0  # polls a deleted claim stays Terminating
        self._terminating_pvcs: dict[tuple[str, str], int] = {}
        self.fail_reason = fail_reason
        self.ready_after_polls = ready_after_polls
        self.delete_polls = delete_polls
        self.stop_polls = stop_polls  # polls a halted VM / scaled-down proxy still shows its pod
        self.run_strategies: list[tuple[str, str, str]] = []  # (namespace, vm, strategy) in call order
        self.deployment_scales: list[tuple[str, str, int]] = []
        self._stopping: dict[tuple[str, str], int] = {}
        self._terminating: dict[str, int] = {}
        self._polls: dict[str, int] = {}
        self._generation: dict[tuple[str, str], int] = {}
        self.namespace_labels: dict[str, dict[str, str]] = {}

    # --- writes --------------------------------------------------------
    def apply(self, obj: dict[str, Any]) -> None:
        obj = copy.deepcopy(obj)  # a later patch must not change the caller's object
        meta = obj["metadata"]
        ns = meta.get("namespace")
        if obj["kind"] != "Namespace" and ns is not None and ns not in self.namespace_labels:
            raise RuntimeError(f"namespace {ns} not found")
        self.objects[(obj["kind"], meta["name"], ns, obj["apiVersion"])] = obj
        if obj["kind"] == "Namespace":
            self.namespace_labels[meta["name"]] = dict(meta.get("labels", {}))
        if obj["kind"] == "StatefulSet":
            key = (ns, meta["name"])
            self.replicas[key] = obj["spec"]["replicas"]
            self._generation.setdefault(key, 0)

    def delete_object(self, api_version: str, kind: str, namespace: str, name: str) -> None:
        self.objects.pop((kind, name, namespace, api_version), None)

    def delete_namespace(self, name: str) -> None:
        if name in self.namespace_labels:
            self._terminating[name] = self.delete_polls

    def scale(self, namespace: str, statefulset: str, replicas: int) -> None:
        if replicas == 0:
            self._generation[(namespace, statefulset)] += 1  # the next pod is a new one
        self.replicas[(namespace, statefulset)] = replicas
        self._polls[namespace] = 0

    def scale_deployment(self, namespace: str, name: str, replicas: int) -> None:
        obj = self._object("Deployment", name, namespace)
        obj["spec"]["replicas"] = replicas
        self.deployment_scales.append((namespace, name, replicas))
        self._changed(namespace, name, stopping=replicas == 0)

    def set_run_strategy(self, namespace: str, vm_name: str, run_strategy: str) -> None:
        obj = self._object("VirtualMachine", vm_name, namespace)
        obj["spec"]["runStrategy"] = run_strategy
        self.run_strategies.append((namespace, vm_name, run_strategy))
        self._changed(namespace, vm_name, stopping=run_strategy == "Halted")

    def _object(self, kind: str, name: str, namespace: str) -> dict[str, Any]:
        for (k, n, ns, _v), obj in self.objects.items():
            if (k, n, ns) == (kind, name, namespace):
                return obj
        raise RuntimeError(f"{kind} {namespace}/{name} not found")

    def _changed(self, namespace: str, name: str, *, stopping: bool) -> None:
        """The pod of a changed workload is new, so it must become ready again."""
        if stopping:
            self._stopping[(namespace, name)] = self.stop_polls
        else:
            self._stopping.pop((namespace, name), None)
        self._polls[namespace] = 0

    def delete_pod(self, namespace: str, pod: str) -> None:
        self.deleted_pods.append(pod)
        workload = pod.rsplit("-", 1)[0]
        self._generation[(namespace, workload)] += 1
        self._polls[namespace] = 0

    def delete_pvcs(self, namespace: str, workload: str) -> None:
        self.deleted_pvcs.append((namespace, workload))
        self._terminating_pvcs[(namespace, workload)] = self.pvc_delete_polls

    # --- reads ---------------------------------------------------------
    def namespaces(self, label_selector: str) -> list[str]:
        """Equality selectors only (``k=v[,k=v...]``), as the platform uses them."""
        wanted = [part.partition("=")[::2] for part in label_selector.split(",") if part]
        return [n for n, labels in self.namespace_labels.items() if all(labels.get(k) == v for k, v in wanted)]

    def kinds(self, namespace: str | None = None) -> list[str]:
        return sorted(k for k, _n, ns, _v in self.objects if namespace is None or ns == namespace)

    def snapshot(self, namespace: str) -> Snapshot:
        if namespace in self._terminating:
            if self._terminating[namespace] > 0:
                self._terminating[namespace] -= 1
                return Snapshot(
                    exists=True,
                    terminating=True,
                    labels=dict(self.namespace_labels[namespace]),
                )
            self._purge(namespace)
        if namespace not in self.namespace_labels:
            return Snapshot(exists=False)
        polls = self._polls[namespace] = self._polls.get(namespace, 0) + 1
        snap = Snapshot(exists=True, labels=dict(self.namespace_labels[namespace]))
        for key in list(self._terminating_pvcs):
            if key[0] == namespace:
                if self._terminating_pvcs[key] <= 0:
                    del self._terminating_pvcs[key]
                else:
                    self._terminating_pvcs[key] -= 1
                    snap.pvcs.append((f"data-{key[1]}-0", key[1]))
        for (ns, name), replicas in sorted(self.replicas.items()):
            if ns != namespace:
                continue
            up = replicas >= 1
            ready = up and polls > self.ready_after_polls and not self.fail_reason
            labels = self.objects[("StatefulSet", name, ns, "apps/v1")]["metadata"]["labels"]
            snap.statefulsets.append(
                StatefulSetInfo(
                    name,
                    replicas,
                    1 if ready else 0,
                    labels.get(TEAM_LABEL, ""),
                    labels.get(USER_LABEL, ""),
                )
            )
            if up:
                pod = PodInfo(
                    name=f"{name}-0",
                    workload=name,
                    phase="Running" if ready else "Pending",
                    ready=ready,
                )
                pod.uid = f"{name}-uid-{self._generation[(ns, name)]}"
                if self.fail_reason == "Unschedulable":
                    pod.unschedulable, pod.message = (
                        True,
                        "0/1 nodes are available: Insufficient memory.",
                    )
                elif self.fail_reason:
                    pod.waiting_reason, pod.restarts = self.fail_reason, 3
                snap.pods.append(pod)
        if snap.labels.get(VM_NAMESPACE_LABEL) == "true":
            self._vm_snapshot(snap, namespace, polls)
        if self.fail_reason == "quota":
            snap.events.append(EventInfo("StatefulSet/x", "FailedCreate", "exceeded quota: appstore-quota"))
        return snap

    def _vm_snapshot(self, snap: Snapshot, namespace: str, polls: int) -> None:
        """noVNC proxies (Deployments) and VMs of a kubevirt-vm namespace, ready after ``ready_after_polls`` polls."""
        ready = polls > self.ready_after_polls and not self.fail_reason
        for (kind, name, ns, _v), obj in sorted(self.objects.items(), key=lambda i: i[0][:3]):
            if ns != namespace:
                continue
            if kind == "Deployment":
                replicas = obj["spec"].get("replicas", 1)
                running = replicas >= 1 or self._still_stopping(namespace, name)
                snap.deployments.append(DeploymentInfo(name, replicas, 1 if ready and replicas >= 1 else 0))
                workload = obj["metadata"]["labels"].get(WORKLOAD_LABEL, name)
                if running:
                    up = ready and replicas >= 1
                    snap.pods.append(
                        PodInfo(name=f"{name}-0", workload=workload, phase="Running" if up else "Pending", ready=up)
                    )
            elif kind == "VirtualMachine":
                labels = obj["metadata"].get("labels", {})
                strategy = obj["spec"].get("runStrategy", "Always")
                snap.virtual_machines.append(VmInfo(name, strategy))
                if strategy != "Halted" or self._still_stopping(namespace, name):
                    up = ready and strategy != "Halted"
                    snap.vmis.append(
                        VmiInfo(
                            name,
                            "Running" if up else "Scheduling",
                            up,
                            labels.get(TEAM_LABEL, ""),
                            labels.get(USER_LABEL, ""),
                        )
                    )
                    # KubeVirt's virt-launcher pod, one per running VM.
                    snap.pods.append(
                        PodInfo(
                            name=f"virt-launcher-{name}-x",
                            workload=labels.get(WORKLOAD_LABEL, name),
                            phase="Running" if up else "Pending",
                            ready=up,
                        )
                    )
        self._tick_stopping(namespace)
        self._pvc_issues(snap)

    def _still_stopping(self, namespace: str, name: str) -> bool:
        return self._stopping.get((namespace, name), 0) > 0

    def _tick_stopping(self, namespace: str) -> None:
        for key in [k for k in self._stopping if k[0] == namespace]:
            self._stopping[key] -= 1

    def _pvc_issues(self, snap: Snapshot) -> None:
        """``fail_reason="pvc"``: the clone of every VM disk is stuck Pending (ProvisioningFailed, quota)."""
        if self.fail_reason != "pvc":
            return
        for vm in snap.virtual_machines:
            disk = f"{vm.name}-disk"
            snap.pvcs.append((disk, vm.name))
            snap.pvc_phases[disk] = "Pending"
            snap.pvc_issues.append(
                (disk, "ProvisioningFailed", "failed to provision volume: exceeded quota: gigabytes_cinder")
            )

    def _purge(self, namespace: str) -> None:
        self._terminating.pop(namespace, None)
        self.namespace_labels.pop(namespace, None)
        self.objects = {k: v for k, v in self.objects.items() if k[2] != namespace and k[1] != namespace}
        self.replicas = {k: v for k, v in self.replicas.items() if k[0] != namespace}

    def workloads(self, namespace: str) -> set[str]:
        return {
            o["metadata"]["labels"][WORKLOAD_LABEL]
            for (kind, _n, ns, _v), o in self.objects.items()
            if ns == namespace and kind == "StatefulSet"
        }

    def deployment_label(self, namespace: str) -> str | None:
        return self.namespace_labels.get(namespace, {}).get(DEPLOYMENT_LABEL)
