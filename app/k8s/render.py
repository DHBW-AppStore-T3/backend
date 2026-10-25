"""Render Kubernetes objects from an :class:`~app.k8s.spec.AppSpec`.

``render`` is a pure function: no cluster access, no randomness, no I/O
(passwords are passed in). That keeps it golden-file testable and lets it move
into an operator later (.github#4, option D).

All security-relevant fields are set here, never taken from the spec:
Pod Security ``restricted`` namespace label, ``runAsNonRoot``, dropped
capabilities, ``seccompProfile: RuntimeDefault``, no service account token,
default-deny NetworkPolicy (IPv4 *and* IPv6) and a ResourceQuota.

People are identified by their user id, never by a name derived from their
e-mail address: two members whose names normalise alike get workloads of
their own, and each access entry names exactly one user id (.github#7 H).
Identical in backend and worker (``test_shared_files``).
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.k8s.spec import AppSpec, Container, parse_cpu_millicores, parse_memory_mib, parse_storage_mib

if TYPE_CHECKING:
    from collections.abc import Mapping

LABEL_PREFIX = "appstore.dhbw"
DEPLOYMENT_LABEL = f"{LABEL_PREFIX}/deployment-id"
WORKLOAD_LABEL = f"{LABEL_PREFIX}/workload"
TEAM_LABEL = f"{LABEL_PREFIX}/team"
USER_LABEL = f"{LABEL_PREFIX}/user"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
# The AppStore installation a namespace belongs to, where several share one
# cluster (staging and prod): the admission policy confines each one's pod
# deployer to its own namespaces by it, and the orphan check looks only there.
ENVIRONMENT_LABEL = f"{LABEL_PREFIX}/environment"
FIELD_MANAGER = "appstore"
PASSWORD_KEY = "password"
USERNAME_KEY = "username"
VM_NAMESPACE_LABEL = f"{LABEL_PREFIX}/vm-namespace"
REFERENCE_GRANT_API = "gateway.networking.k8s.io/v1beta1"
# Pod roles inside a kubevirt-vm namespace, for the network policies.
ROLE_LABEL = f"{LABEL_PREFIX}/role"
ROLE_VM = "vm"
ROLE_VNC_PROXY = "vnc-proxy"
VNC_PROXY_ACCOUNT = "vnc-proxy"

# Never reachable from student pods: pod/service/node networks, RFC1918,
# link-local (incl. metadata service 169.254.169.254), ULA and IPv6 link-local.
_EGRESS_EXCEPT_V4 = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"]
_EGRESS_EXCEPT_V6 = ["fc00::/7", "fe80::/10"]

# Readiness of the exposed container: the named port ``http`` (= ``expose.port``).
_READINESS_PROBE: dict[str, Any] = {
    "tcpSocket": {"port": "http"},
    "periodSeconds": 3,
    "timeoutSeconds": 2,
    "failureThreshold": 3,
}


@dataclass(frozen=True)
class DeploymentCtx:
    id: str  # UUID
    course: str = ""
    owner: str = ""  # the owner's user id

    @property
    def short_id(self) -> str:
        return self.id.replace("-", "").lower()[:8]

    @property
    def namespace(self) -> str:
        return f"dep-{self.short_id}"


@dataclass(frozen=True)
class Member:
    """A team member: the user id identifies, the user name is shown and used in slot keys."""

    user_id: str
    name: str


@dataclass(frozen=True)
class Team:
    name: str
    members: tuple[Member, ...] = ()  # used for ``scope: user``


@dataclass(frozen=True)
class RenderSettings:
    zone: str
    ingress_class: str = "traefik"
    ingress_namespace: str = "kube-system"
    tls_secret: str = ""  # wildcard cert Secret; empty => no ``tls`` block (cert-manager annotation instead)
    cluster_issuer: str = ""
    storage_class: str = ""
    node_selector: Mapping[str, str] = field(default_factory=dict)
    tolerations: tuple[Mapping[str, str], ...] = ()
    run_as_id: int = 1000
    extra_egress_except: tuple[str, ...] = ()  # pod/service/node CIDRs of the cluster
    # Hosts are <workload>-<deployment>.<app_domain>. Default apps.<zone>; set it
    # to the zone itself to stay under an existing single-level wildcard certificate.
    app_domain_override: str = ""
    # Upper bound of each ``writablePaths`` scratch volume. Without one an
    # emptyDir grows until the node's disk is full and the kubelet starts
    # evicting everything on it; beyond it only this pod is evicted.
    scratch_size_limit: str = "1Gi"
    # The installation the namespaces belong to (ENVIRONMENT_LABEL); empty: none.
    environment: str = ""

    @property
    def app_domain(self) -> str:
        return self.app_domain_override or f"apps.{self.zone}"


@dataclass(frozen=True)
class WorkloadRef:
    name: str  # DNS-1123 label, also StatefulSet/Service/Ingress/Secret name
    team: str
    user: Member | None  # None => the whole team's workload

    def host(self, dep: DeploymentCtx, settings: RenderSettings) -> str:
        return f"{self.name}-{dep.short_id}.{settings.app_domain}"


@dataclass(frozen=True)
class AccessEntry:
    workload: str
    team: str
    user: Member | None  # None => whole team
    url: str | None
    username: str | None
    password: str | None


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "x"


def _dns_name(*parts: str, max_len: int = 40) -> str:
    """Stable DNS-1123 label; shortened with a hash suffix if too long.

    Capped at 40 so ``<name>-<8 hex>`` fits a 63-char host label.
    """
    raw = "-".join(_slug(p) for p in parts)
    if len(raw) <= max_len:
        return raw
    digest = hashlib.sha256(raw.encode()).hexdigest()[:6]
    return f"{raw[: max_len - 7].rstrip('-')}-{digest}"


def label_value(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_.-")[:63].strip("_.-")
    return cleaned


def workload_refs(spec: AppSpec, teams: list[Team]) -> list[WorkloadRef]:
    """One workload per team (``scope: team``) or per member (``scope: user``).

    A member's workload is named after team and user name; where two members
    would get the same name (``a.b`` and ``a-b``), both names carry a piece of
    the user id, so nobody shares a workload. Team names that normalise alike
    are refused.
    """
    if spec.scope == "team":
        refs = [WorkloadRef(_dns_name(team.name), team.name, None) for team in teams]
    else:
        pairs = [(team, member) for team in teams for member in team.members]
        base = Counter(_dns_name(t.name, m.name) for t, m in pairs)
        refs = [
            WorkloadRef(
                (
                    _dns_name(t.name, m.name)
                    if base[_dns_name(t.name, m.name)] == 1
                    else _dns_name(t.name, m.name, m.user_id.replace("-", "")[:8])
                ),
                t.name,
                m,
            )
            for t, m in pairs
        ]
    names = [r.name for r in refs]
    if len(set(names)) != len(names):
        raise ValueError("workload names collide; team names must be distinct after normalisation")
    return refs


def _labels(dep: DeploymentCtx, workload: str | None = None) -> dict[str, str]:
    labels = {
        MANAGED_BY_LABEL: FIELD_MANAGER,
        DEPLOYMENT_LABEL: dep.id,
    }
    if dep.course:
        labels[f"{LABEL_PREFIX}/course"] = label_value(dep.course)
    if dep.owner:
        labels[f"{LABEL_PREFIX}/owner"] = label_value(dep.owner)
    if workload:
        labels[WORKLOAD_LABEL] = workload
    return {k: v for k, v in labels.items() if v}


def _meta(
    name: str,
    dep: DeploymentCtx,
    workload: str | None = None,
    *,
    namespaced: bool = True,
) -> dict[str, Any]:
    meta: dict[str, Any] = {"name": name, "labels": _labels(dep, workload)}
    if namespaced:
        meta["namespace"] = dep.namespace
    return meta


def _quantities(spec: AppSpec, n: int) -> dict[str, str]:
    assert spec.workload is not None
    cs = spec.workload.containers
    return {
        "requests.cpu": f"{sum(parse_cpu_millicores(c.resources.request_cpu) for c in cs) * n}m",
        "requests.memory": f"{sum(parse_memory_mib(c.resources.request_memory) for c in cs) * n}Mi",
        "limits.cpu": f"{sum(parse_cpu_millicores(c.resources.cpu) for c in cs) * n}m",
        "limits.memory": f"{sum(parse_memory_mib(c.resources.memory) for c in cs) * n}Mi",
    }


def _storage_mib(spec: AppSpec) -> int:
    assert spec.workload is not None
    if spec.workload.storage is None:
        return 0
    return parse_storage_mib(spec.workload.storage.size)


def _namespace_object(
    dep: DeploymentCtx,
    settings: RenderSettings,
    pod_security: str,
    extra_labels: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The deployment's Namespace; ``pod_security`` is the Pod Security level it enforces."""
    meta = _meta(dep.namespace, dep, namespaced=False)
    meta["labels"] = {
        **meta["labels"],
        "pod-security.kubernetes.io/enforce": pod_security,
        "pod-security.kubernetes.io/enforce-version": "latest",
        **(extra_labels or {}),
    }
    if settings.environment:
        meta["labels"][ENVIRONMENT_LABEL] = label_value(settings.environment)
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": meta}


def _namespace(dep: DeploymentCtx, settings: RenderSettings) -> dict[str, Any]:
    return _namespace_object(dep, settings, "restricted")


def _quota_and_limits(spec: AppSpec, dep: DeploymentCtx, n: int) -> list[dict[str, Any]]:
    assert spec.workload is not None
    total = _quantities(spec, n)
    hard: dict[str, str] = {
        **total,
        "pods": str(n),
        "services": str(n),
        "services.loadbalancers": "0",
        "services.nodeports": "0",
        "persistentvolumeclaims": str(n if spec.workload.storage else 0),
        "requests.storage": f"{_storage_mib(spec) * n}Mi",
    }
    quota = {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": _meta("appstore-quota", dep),
        "spec": {"hard": hard},
    }
    biggest = max(spec.workload.containers, key=lambda c: parse_memory_mib(c.resources.memory))
    limit_range = {
        "apiVersion": "v1",
        "kind": "LimitRange",
        "metadata": _meta("appstore-limits", dep),
        "spec": {
            "limits": [
                {
                    "type": "Container",
                    "default": {
                        "cpu": biggest.resources.cpu,
                        "memory": biggest.resources.memory,
                    },
                    "defaultRequest": {
                        "cpu": biggest.resources.request_cpu,
                        "memory": biggest.resources.request_memory,
                    },
                }
            ]
        },
    }
    return [quota, limit_range]


def _netpol(dep: DeploymentCtx, name: str, body: dict[str, Any], pod_selector: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": _meta(name, dep),
        "spec": {"podSelector": pod_selector, **body},
    }


def _np_default_deny(dep: DeploymentCtx) -> dict[str, Any]:
    return _netpol(dep, "default-deny", {"policyTypes": ["Ingress", "Egress"]}, {})


def _np_allow_ingress_controller(
    dep: DeploymentCtx, settings: RenderSettings, port: int, pod_selector: dict[str, Any]
) -> dict[str, Any]:
    """The ingress controller's namespace may reach ``port`` of the selected pods."""
    return _netpol(
        dep,
        "allow-ingress-controller",
        {
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {"kubernetes.io/metadata.name": settings.ingress_namespace}
                            }
                        }
                    ],
                    "ports": [{"protocol": "TCP", "port": port}],
                }
            ],
        },
        pod_selector,
    )


def _np_allow_dns(dep: DeploymentCtx) -> dict[str, Any]:
    return _netpol(
        dep,
        "allow-dns",
        {
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [
                        {
                            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                        }
                    ],
                    "ports": [
                        {"protocol": "UDP", "port": 53},
                        {"protocol": "TCP", "port": 53},
                    ],
                }
            ],
        },
        {},
    )


def _np_allow_egress_internet(
    dep: DeploymentCtx, settings: RenderSettings, pod_selector: dict[str, Any]
) -> dict[str, Any]:
    """Internet for the selected pods, without pod/service/node networks, private and link-local ranges."""
    extra = list(settings.extra_egress_except)
    v4 = _EGRESS_EXCEPT_V4 + [c for c in extra if ":" not in c]
    v6 = _EGRESS_EXCEPT_V6 + [c for c in extra if ":" in c]
    return _netpol(
        dep,
        "allow-egress-internet",
        {
            "policyTypes": ["Egress"],
            "egress": [
                {"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": v4}}]},
                {"to": [{"ipBlock": {"cidr": "::/0", "except": v6}}]},
            ],
        },
        pod_selector,
    )


def _np_allow_intra_namespace(
    dep: DeploymentCtx,
    peer: list[dict[str, Any]],
    pod_selector: dict[str, Any],
    ports: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Both directions between the selected pods and ``peer`` (default-deny blocks both).

    Without ``ports`` every port is open.
    """
    port_part = {"ports": ports} if ports else {}
    return _netpol(
        dep,
        "allow-intra-namespace",
        {
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [{"from": peer, **port_part}],
            "egress": [{"to": peer, **port_part}],
        },
        pod_selector,
    )


def _network_policies(spec: AppSpec, dep: DeploymentCtx, settings: RenderSettings) -> list[dict[str, Any]]:
    assert spec.workload is not None
    port = next(c.expose.port for c in spec.workload.containers if c.expose)
    policies = [
        _np_default_deny(dep),
        _np_allow_ingress_controller(dep, settings, port, {}),
        _np_allow_dns(dep),
    ]
    if spec.egress == "internet":
        policies.append(_np_allow_egress_internet(dep, settings, {}))
    if spec.pod_network == "namespace":
        # No namespaceSelector or ipBlock, so it reaches only pods of this
        # deployment's namespace; the admission policy demands TCP ports here.
        policies.append(_np_allow_intra_namespace(dep, [{"podSelector": {}}], {}, [{"protocol": "TCP", "port": port}]))
    return policies


def _ingress(dep: DeploymentCtx, settings: RenderSettings, name: str, path: str) -> dict[str, Any]:
    """Ingress of the Service ``name`` (port ``http``) at ``<name>-<deployment>.<app domain>``.

    Always a tls section: without one the ingress controller serves plain HTTP only.
    No secretName means the controller's default certificate (a cluster wildcard).
    """
    meta = _meta(name, dep, name)
    if settings.cluster_issuer:
        meta["annotations"] = {"cert-manager.io/cluster-issuer": settings.cluster_issuer}
    host = f"{name}-{dep.short_id}.{settings.app_domain}"
    tls: dict[str, Any] = {"hosts": [host]}
    if settings.tls_secret:
        tls["secretName"] = settings.tls_secret
    elif settings.cluster_issuer:
        tls["secretName"] = f"{name}-tls"
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": meta,
        "spec": {
            "ingressClassName": settings.ingress_class,
            "rules": [
                {
                    "host": host,
                    "http": {
                        "paths": [
                            {
                                "path": path,
                                "pathType": "Prefix",
                                "backend": {"service": {"name": name, "port": {"name": "http"}}},
                            }
                        ]
                    },
                }
            ],
            "tls": [tls],
        },
    }


def _env_entries(c: Container, ref: WorkloadRef) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in c.env:
        if e.value is not None:
            out.append({"name": e.name, "value": e.value})
        elif e.source == "generated-password":
            out.append(
                {
                    "name": e.name,
                    "valueFrom": {"secretKeyRef": {"name": ref.name, "key": PASSWORD_KEY}},
                }
            )
        elif e.source == "team-name":
            out.append({"name": e.name, "value": ref.team})
        else:  # user-name
            out.append({"name": e.name, "value": ref.user.name if ref.user else ref.team})
    return out


def _slot(values: Mapping[str, Any] | None, ref: WorkloadRef) -> Any:
    """The entry of a per-scope value map that belongs to ``ref``.

    Wizard maps are keyed ``all`` (whole deployment), by team name, or
    ``<team>-<user name>`` (per person). Keys are compared exactly (the API
    checks them against the roster): a normalised comparison could hand one
    person's file to another whose name normalises alike.
    """
    if not values:
        return None
    keys = ["all", f"{ref.team}-{ref.user.name}" if ref.user else None, ref.team]
    for key in keys:
        if key is not None and key in values:
            return values[key]
    return None


def _file_contents(spec: AppSpec, ref: WorkloadRef, files: Mapping[str, Mapping[str, bytes]] | None):
    """``[(variable, mountPath, bytes)]`` of the file variables ``ref`` got a file for."""
    out = []
    for v in spec.variables:
        if v.type != "file" or v.mount_path is None:
            continue
        content = _slot((files or {}).get(v.name), ref)
        if content is not None:
            out.append((v.name, v.mount_path, content))
    return out


def _variable_env(spec: AppSpec, ref: WorkloadRef, values: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Enum/string variables reach every container as ``APPSTORE_VAR_<NAME>``."""
    out = []
    for v in spec.variables:
        if v.type == "file":
            continue
        raw = (values or {}).get(v.name, v.default)
        if isinstance(raw, dict):
            raw = _slot(raw, ref)
            if raw is None:
                raw = v.default
        if raw is not None:
            out.append(
                {
                    "name": f"APPSTORE_VAR_{re.sub(r'[^A-Za-z0-9]', '_', v.name).upper()}",
                    "value": str(raw),
                }
            )
    return out


def _workload_objects(
    spec: AppSpec,
    dep: DeploymentCtx,
    ref: WorkloadRef,
    password: str,
    settings: RenderSettings,
    files: Mapping[str, Mapping[str, bytes]] | None = None,
    variable_values: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    assert spec.workload is not None
    file_items = _file_contents(spec, ref, files)
    var_env = _variable_env(spec, ref, variable_values)
    sel = {WORKLOAD_LABEL: ref.name, DEPLOYMENT_LABEL: dep.id}
    storage = spec.workload.storage
    exposed = next(c for c in spec.workload.containers if c.expose)
    assert exposed.expose is not None

    volumes: list[dict[str, Any]] = []
    containers: list[dict[str, Any]] = []
    for c in spec.workload.containers:
        mounts: list[dict[str, Any]] = []
        for i, path in enumerate(c.writable_paths):
            vol = f"{c.name}-tmp-{i}"
            volumes.append({"name": vol, "emptyDir": {"sizeLimit": settings.scratch_size_limit}})
            mounts.append({"name": vol, "mountPath": path})
        if storage:
            mounts.append({"name": "data", "mountPath": storage.mount_path})
        if c.expose and file_items:
            mounts.extend(
                {"name": "files", "mountPath": path, "subPath": key, "readOnly": True} for key, path, _ in file_items
            )
        item: dict[str, Any] = {
            "name": c.name,
            "image": c.image,
            "env": _env_entries(c, ref) + var_env,
            "resources": {
                "requests": {
                    "cpu": c.resources.request_cpu,
                    "memory": c.resources.request_memory,
                },
                "limits": {"cpu": c.resources.cpu, "memory": c.resources.memory},
            },
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": True,
                "runAsNonRoot": True,
                "capabilities": {"drop": ["ALL"]},
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "volumeMounts": mounts,
        }
        if c.expose:
            item["ports"] = [{"name": "http", "containerPort": c.expose.port, "protocol": "TCP"}]
            # Ready only once the exposed port accepts connections: without a
            # probe the pod is Ready at container start and the ingress answers
            # 502 until the app listens. A TCP check works for every app (an
            # HTTP check would need a path that answers 2xx/3xx without login).
            item["readinessProbe"] = dict(_READINESS_PROBE)
        containers.append(item)

    if file_items:
        volumes.append(
            {
                "name": "files",
                "secret": {"secretName": f"{ref.name}-files", "defaultMode": 0o444},
            }
        )

    pod_spec: dict[str, Any] = {
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": settings.run_as_id,
            "runAsGroup": settings.run_as_id,
            "fsGroup": settings.run_as_id,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": containers,
        "volumes": volumes,
    }
    if settings.node_selector:
        pod_spec["nodeSelector"] = dict(settings.node_selector)
    if settings.tolerations:
        pod_spec["tolerations"] = [dict(t) for t in settings.tolerations]

    sts_spec: dict[str, Any] = {
        "replicas": 1,
        "serviceName": ref.name,
        "selector": {"matchLabels": sel},
        "template": {
            "metadata": {"labels": {**_labels(dep, ref.name)}},
            "spec": pod_spec,
        },
    }
    if storage:
        claim: dict[str, Any] = {
            "metadata": {"name": "data", "labels": _labels(dep, ref.name)},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": storage.size}},
            },
        }
        if settings.storage_class:
            claim["spec"]["storageClassName"] = settings.storage_class
        sts_spec["volumeClaimTemplates"] = [claim]

    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": _meta(ref.name, dep, ref.name),
        "type": "Opaque",
        "stringData": {PASSWORD_KEY: password},
    }
    sts_meta = _meta(ref.name, dep, ref.name)
    # For the live view: whose workload this is (team name, user id).
    sts_meta["labels"][TEAM_LABEL] = label_value(ref.team)
    if ref.user:
        sts_meta["labels"][USER_LABEL] = label_value(ref.user.user_id)
    statefulset = {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": sts_meta,
        "spec": sts_spec,
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": _meta(ref.name, dep, ref.name),
        "spec": {
            "type": "ClusterIP",
            "selector": sel,
            "ports": [{"name": "http", "port": exposed.expose.port, "targetPort": "http"}],
        },
    }
    ingress = _ingress(dep, settings, ref.name, exposed.expose.path)
    objects = [secret]
    if file_items:
        objects.append(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": _meta(f"{ref.name}-files", dep, ref.name),
                "type": "Opaque",
                "data": {key: base64.b64encode(content).decode() for key, _, content in file_items},
            }
        )
    return [*objects, statefulset, service, ingress]


def _vm_namespace(dep: DeploymentCtx, settings: RenderSettings) -> dict[str, Any]:
    """Namespace for a kubevirt-vm deployment; PSS privileged (virt-launcher needs it)."""
    return _namespace_object(dep, settings, "privileged", {VM_NAMESPACE_LABEL: "true"})


def vm_clone_grant_name(dep: DeploymentCtx) -> str:
    return f"clone-{dep.namespace}"


def _vm_clone_grant(spec: AppSpec, dep: DeploymentCtx) -> dict[str, Any]:
    """Lets this deployment's PVCs clone the golden image of another namespace.

    The grant lives in the image namespace and names the one deployment namespace
    it is for (a ReferenceGrant has no wildcards); the worker removes it with the deployment.
    """
    assert spec.vm is not None
    meta = _meta(vm_clone_grant_name(dep), dep, namespaced=False)
    meta["namespace"] = spec.vm.image_namespace
    return {
        "apiVersion": REFERENCE_GRANT_API,
        "kind": "ReferenceGrant",
        "metadata": meta,
        "spec": {
            "from": [{"group": "", "kind": "PersistentVolumeClaim", "namespace": dep.namespace}],
            "to": [{"group": "", "kind": "PersistentVolumeClaim", "name": spec.vm.image_ref}],
        },
    }


def _vm_network_policies(spec: AppSpec, dep: DeploymentCtx, settings: RenderSettings) -> list[dict[str, Any]]:
    """Default-deny, then only what a VM namespace needs.

    The browser reaches the VNC proxy through the ingress controller; the proxy
    reaches the VM through the Kubernetes API (KubeVirt's VNC subresource), never
    through a pod port. The VM itself gets DNS, and internet / the other VMs of
    the namespace when the app (or the person, see ``userChoices``) allows it.
    """
    proxy = {"matchLabels": {ROLE_LABEL: ROLE_VNC_PROXY}}
    vm = {"matchLabels": {ROLE_LABEL: ROLE_VM}}
    policies = [
        _np_default_deny(dep),
        _np_allow_ingress_controller(dep, settings, 8080, proxy),
        _np_allow_dns(dep),
        # The proxy talks to the API server (kubernetes.default: service port 443, node port 6443).
        _netpol(
            dep,
            "allow-vnc-proxy-to-apiserver",
            {
                "policyTypes": ["Egress"],
                "egress": [
                    {
                        "to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}, {"ipBlock": {"cidr": "::/0"}}],
                        "ports": [{"protocol": "TCP", "port": 443}, {"protocol": "TCP", "port": 6443}],
                    }
                ],
            },
            proxy,
        ),
    ]
    if spec.egress == "internet":
        policies.append(_np_allow_egress_internet(dep, settings, vm))
    if spec.pod_network == "namespace":
        # The VMs of this namespace see each other (any port); the proxies are not part of it.
        policies.append(_np_allow_intra_namespace(dep, [{"podSelector": vm}], vm))
    return policies


def _vm_workload_objects(
    spec: AppSpec,
    dep: DeploymentCtx,
    ref: WorkloadRef,
    vnc_password: str,
    settings: RenderSettings,
) -> list[dict[str, Any]]:
    """All Kubernetes/KubeVirt objects for one kubevirt-vm workload slot."""
    assert spec.vm is not None
    vm = spec.vm
    workload_name = ref.name
    disk_name = f"{workload_name}-disk"
    novnc_name = f"{workload_name}-novnc"
    namespace = dep.namespace

    labels = _labels(dep, workload_name)
    novnc_labels = {**labels, WORKLOAD_LABEL: novnc_name, ROLE_LABEL: ROLE_VNC_PROXY}

    # PVC — a clone of the golden image; the platform grants the access (see _vm_clone_grant)
    pvc: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": _meta(disk_name, dep, workload_name),
        "spec": {
            "storageClassName": vm.storage_class,
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": f"{vm.disk_gib}Gi"}},
            "dataSourceRef": {
                "apiGroup": "",
                "kind": "PersistentVolumeClaim",
                "name": vm.image_ref,
                "namespace": vm.image_namespace,
            },
        },
    }

    # The VM and its pod carry whose they are, so the live view can tell the rows apart.
    vm_labels = {**labels, TEAM_LABEL: label_value(ref.team)}
    if ref.user:
        vm_labels[USER_LABEL] = label_value(ref.user.user_id)

    # VirtualMachine CR
    virtual_machine: dict[str, Any] = {
        "apiVersion": "kubevirt.io/v1",
        "kind": "VirtualMachine",
        "metadata": {
            **_meta(workload_name, dep, workload_name),
            "labels": vm_labels,
        },
        "spec": {
            "runStrategy": "Always",
            "template": {
                "metadata": {"labels": {**vm_labels, "vm.kubevirt.io/name": workload_name, ROLE_LABEL: ROLE_VM}},
                "spec": {
                    "domain": {
                        "cpu": {"cores": vm.cpu_cores},
                        "memory": {"guest": f"{vm.memory_gib}Gi"},
                        "devices": {
                            "disks": [{"name": "root", "disk": {"bus": "sata"}}],
                            "interfaces": [{"name": "default", "masquerade": {}, "model": "e1000e"}],
                            "inputs": [{"name": "tablet", "type": "tablet", "bus": "usb"}],
                            "tpm": {},
                        },
                        "features": {"hyperv": {}, "acpi": {}},
                        "firmware": {"bootloader": {"efi": {"secureBoot": False}}},
                    },
                    "networks": [{"name": "default", "pod": {}}],
                    "volumes": [
                        {
                            "name": "root",
                            "persistentVolumeClaim": {"claimName": disk_name},
                        }
                    ],
                },
            },
        },
    }
    # What the person signs in to the proxy with (HTTP basic auth, see docker/novnc-proxy).
    vnc_secret: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": _meta(workload_name, dep, workload_name),
        "type": "Opaque",
        "stringData": {PASSWORD_KEY: vnc_password, USERNAME_KEY: ref.user.name if ref.user else ref.team},
    }

    # noVNC proxy deployment: noVNC in the browser, bridged to KubeVirt's VNC subresource of this VM
    novnc_deploy: dict[str, Any] = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {**_meta(novnc_name, dep, novnc_name), "labels": novnc_labels},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {WORKLOAD_LABEL: novnc_name, DEPLOYMENT_LABEL: dep.id}},
            "template": {
                "metadata": {"labels": novnc_labels},
                "spec": {
                    "serviceAccountName": VNC_PROXY_ACCOUNT,
                    "automountServiceAccountToken": True,
                    "enableServiceLinks": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 1000,
                        "runAsGroup": 1000,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "novnc",
                            "image": "ghcr.io/dhbw-appstore-t3/novnc-proxy:latest",
                            "ports": [
                                {
                                    "name": "http",
                                    "containerPort": 8080,
                                    "protocol": "TCP",
                                }
                            ],
                            "env": [
                                {"name": "VM_NAME", "value": workload_name},
                                {"name": "VM_NAMESPACE", "value": namespace},
                                {
                                    "name": "PROXY_USER",
                                    "valueFrom": {"secretKeyRef": {"name": workload_name, "key": USERNAME_KEY}},
                                },
                                {
                                    "name": "PROXY_PASSWORD",
                                    "valueFrom": {"secretKeyRef": {"name": workload_name, "key": PASSWORD_KEY}},
                                },
                            ],
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "runAsNonRoot": True,
                                "capabilities": {"drop": ["ALL"]},
                                "seccompProfile": {"type": "RuntimeDefault"},
                            },
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "64Mi"},
                                "limits": {"cpu": "200m", "memory": "128Mi"},
                            },
                            "readinessProbe": {
                                "tcpSocket": {"port": "http"},
                                "periodSeconds": 5,
                                "timeoutSeconds": 2,
                                "failureThreshold": 3,
                            },
                        }
                    ],
                },
            },
        },
    }

    # Service for noVNC proxy
    novnc_service: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": _meta(novnc_name, dep, novnc_name),
        "spec": {
            "type": "ClusterIP",
            "selector": {WORKLOAD_LABEL: novnc_name, DEPLOYMENT_LABEL: dep.id},
            "ports": [{"name": "http", "port": 8080, "targetPort": "http"}],
        },
    }

    # Ingress for noVNC (browser access)
    ingress = _ingress(dep, settings, novnc_name, "/")

    return [
        vnc_secret,
        pvc,
        virtual_machine,
        novnc_deploy,
        novnc_service,
        ingress,
    ]


def render_vm(
    spec: AppSpec,
    dep: DeploymentCtx,
    teams: list[Team],
    settings: RenderSettings,
    passwords: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Return all objects of a kubevirt-vm deployment in apply order.

    ``passwords`` maps workload name (see :func:`workload_refs`) to its generated VNC password.
    """
    if spec.runtime != "kubevirt-vm" or spec.vm is None:
        raise ValueError("render_vm() only supports runtime 'kubevirt-vm'")
    refs = workload_refs(spec, teams)
    if not refs:
        raise ValueError("no workloads: teams are empty")
    missing = [r.name for r in refs if r.name not in passwords]
    if missing:
        raise ValueError(f"missing passwords for workloads: {', '.join(missing)}")

    # No ResourceQuota/LimitRange here, on purpose: the admission policy demands
    # limits.cpu/limits.memory in every quota, and a quota with them rejects each
    # pod without limits (KubeVirt's virt-launcher, CDI clone pods). Capping the
    # VM namespaces needs limits on those pods first.
    objects: list[dict[str, Any]] = [
        _vm_namespace(dep, settings),
        *_vm_network_policies(spec, dep, settings),
        _vm_clone_grant(spec, dep),
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": _meta(VNC_PROXY_ACCOUNT, dep)},
    ]
    for ref in refs:
        objects.extend(_vm_workload_objects(spec, dep, ref, passwords[ref.name], settings))
    return objects


def render(
    spec: AppSpec,
    dep: DeploymentCtx,
    teams: list[Team],
    settings: RenderSettings,
    passwords: Mapping[str, str],
    files: Mapping[str, Mapping[str, bytes]] | None = None,
    variable_values: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return all objects of a deployment in apply order.

    ``passwords`` maps workload name (see :func:`workload_refs`) to its generated password.
    ``files`` maps a file variable to ``{slot: content}`` (slot: ``all``, a team name or
    ``<team>-<user name>``); ``variable_values`` maps enum/string variables to a value or
    such a slot map.
    """
    if spec.runtime != "kubernetes" or spec.workload is None:
        raise ValueError("render() only supports runtime 'kubernetes'")
    refs = workload_refs(spec, teams)
    if not refs:
        raise ValueError("no workloads: teams are empty")
    missing = [r.name for r in refs if r.name not in passwords]
    if missing:
        raise ValueError(f"missing passwords for workloads: {', '.join(missing)}")

    objects = [
        _namespace(dep, settings),
        *_quota_and_limits(spec, dep, len(refs)),
        *_network_policies(spec, dep, settings),
    ]
    for ref in refs:
        objects.extend(_workload_objects(spec, dep, ref, passwords[ref.name], settings, files, variable_values))
    return objects


def _checked_url(url: str, host: str) -> str:
    """The access URL must be https and lead to the ingress the platform created for the workload."""
    scheme, sep, rest = url.partition("://")
    actual = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if not sep or scheme != "https" or actual != host:
        raise ValueError(
            f"access url {url!r} does not match the workload's host {host!r}; "
            "use https://{workload}-{deployment}.{domain}"
        )
    return url


def render_access(
    spec: AppSpec,
    dep: DeploymentCtx,
    teams: list[Team],
    settings: RenderSettings,
    passwords: Mapping[str, str],
) -> list[AccessEntry]:
    """Access data per workload; ``scope: user`` => one entry per person."""
    url_tpl = next((a.template for a in spec.access if a.type == "url"), None)
    pw = next((a for a in spec.access if a.type == "password"), None)
    entries = []
    for ref in workload_refs(spec, teams):
        fmt = {
            "workload": ref.name,
            "deployment": dep.short_id,
            "zone": settings.zone,
            "domain": settings.app_domain,
        }
        username = None
        if pw and pw.username:
            username = pw.username.format(user=ref.user.name if ref.user else ref.team, team=ref.team)
        entries.append(
            AccessEntry(
                workload=ref.name,
                team=ref.team,
                user=ref.user,
                url=(_checked_url(url_tpl.format(**fmt), ref.host(dep, settings)) if url_tpl else None),
                username=username,
                password=passwords[ref.name] if pw else None,
            )
        )
    return entries
