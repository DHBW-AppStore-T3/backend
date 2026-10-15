"""An in-memory cluster for tests: stands in for :class:`app.k8s.kube.KubeApi`.

It does what the real cluster does that the jobs depend on: applied
StatefulSets get pods, scaling adds or removes them, deleting a namespace
removes everything in it (after ``delete_polls`` polls, like finalizers), and
pods can be made to fail with a given reason. Identical in backend and worker
(``test_shared_files``); also the stand-in for a local run without a cluster.
"""

from __future__ import annotations

from typing import Any

from app.k8s.kube import EventInfo, PodInfo, Snapshot, StatefulSetInfo
from app.k8s.render import DEPLOYMENT_LABEL, TEAM_LABEL, USER_LABEL, WORKLOAD_LABEL


class FakeKube:
    def __init__(self, *, fail_reason: str | None = None, ready_after_polls: int = 1, delete_polls: int = 1):
        self.objects: dict[tuple[str, str, str | None, str], dict[str, Any]] = {}
        self.replicas: dict[tuple[str, str], int] = {}
        self.deleted_pods: list[str] = []
        self.deleted_pvcs: list[tuple[str, str]] = []
        self.pvc_delete_polls = 0  # polls a deleted claim stays Terminating
        self._terminating_pvcs: dict[tuple[str, str], int] = {}
        self.fail_reason = fail_reason
        self.ready_after_polls = ready_after_polls
        self.delete_polls = delete_polls
        self._terminating: dict[str, int] = {}
        self._polls: dict[str, int] = {}
        self._generation: dict[tuple[str, str], int] = {}
        self.namespace_labels: dict[str, dict[str, str]] = {}

    # --- writes --------------------------------------------------------
    def apply(self, obj: dict[str, Any]) -> None:
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

    def delete_namespace(self, name: str) -> None:
        if name in self.namespace_labels:
            self._terminating[name] = self.delete_polls

    def scale(self, namespace: str, statefulset: str, replicas: int) -> None:
        if replicas == 0:
            self._generation[(namespace, statefulset)] += 1  # the next pod is a new one
        self.replicas[(namespace, statefulset)] = replicas
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
                return Snapshot(exists=True, terminating=True, labels=dict(self.namespace_labels[namespace]))
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
                    name, replicas, 1 if ready else 0, labels.get(TEAM_LABEL, ""), labels.get(USER_LABEL, "")
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
                    pod.unschedulable, pod.message = True, "0/1 nodes are available: Insufficient memory."
                elif self.fail_reason:
                    pod.waiting_reason, pod.restarts = self.fail_reason, 3
                snap.pods.append(pod)
        if self.fail_reason == "quota":
            snap.events.append(EventInfo("StatefulSet/x", "FailedCreate", "exceeded quota: appstore-quota"))
        return snap

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
