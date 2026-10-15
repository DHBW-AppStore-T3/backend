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
FIELD_MANAGER = "appstore"
PASSWORD_KEY = "password"

# Never reachable from student pods: pod/service/node networks, RFC1918,
# link-local (incl. metadata service 169.254.169.254), ULA and IPv6 link-local.
_EGRESS_EXCEPT_V4 = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"]
_EGRESS_EXCEPT_V6 = ["fc00::/7", "fe80::/10"]


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


def _meta(name: str, dep: DeploymentCtx, workload: str | None = None, *, namespaced: bool = True) -> dict[str, Any]:
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


def _namespace(dep: DeploymentCtx) -> dict[str, Any]:
    meta = _meta(dep.namespace, dep, namespaced=False)
    meta["labels"] = {
        **meta["labels"],
        "pod-security.kubernetes.io/enforce": "restricted",
        "pod-security.kubernetes.io/enforce-version": "latest",
    }
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": meta}


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
                    "default": {"cpu": biggest.resources.cpu, "memory": biggest.resources.memory},
                    "defaultRequest": {
                        "cpu": biggest.resources.request_cpu,
                        "memory": biggest.resources.request_memory,
                    },
                }
            ]
        },
    }
    return [quota, limit_range]


def _network_policies(spec: AppSpec, dep: DeploymentCtx, settings: RenderSettings) -> list[dict[str, Any]]:
    assert spec.workload is not None
    port = next(c.expose.port for c in spec.workload.containers if c.expose)

    def policy(name: str, body: dict[str, Any]) -> dict[str, Any]:
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": _meta(name, dep),
            "spec": {"podSelector": {}, **body},
        }

    policies = [
        policy("default-deny", {"policyTypes": ["Ingress", "Egress"]}),
        policy(
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
        ),
        policy(
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
                        "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
                    }
                ],
            },
        ),
    ]
    if spec.egress == "internet":
        extra = list(settings.extra_egress_except)
        v4 = _EGRESS_EXCEPT_V4 + [c for c in extra if ":" not in c]
        v6 = _EGRESS_EXCEPT_V6 + [c for c in extra if ":" in c]
        policies.append(
            policy(
                "allow-egress-internet",
                {
                    "policyTypes": ["Egress"],
                    "egress": [
                        {"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": v4}}]},
                        {"to": [{"ipBlock": {"cidr": "::/0", "except": v6}}]},
                    ],
                },
            )
        )
    return policies


def _env_entries(c: Container, ref: WorkloadRef) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in c.env:
        if e.value is not None:
            out.append({"name": e.name, "value": e.value})
        elif e.source == "generated-password":
            out.append({"name": e.name, "valueFrom": {"secretKeyRef": {"name": ref.name, "key": PASSWORD_KEY}}})
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
            out.append({"name": f"APPSTORE_VAR_{re.sub(r'[^A-Za-z0-9]', '_', v.name).upper()}", "value": str(raw)})
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
                "requests": {"cpu": c.resources.request_cpu, "memory": c.resources.request_memory},
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
        containers.append(item)

    if file_items:
        volumes.append({"name": "files", "secret": {"secretName": f"{ref.name}-files", "defaultMode": 0o444}})

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
        "template": {"metadata": {"labels": {**_labels(dep, ref.name)}}, "spec": pod_spec},
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
    ingress_meta = _meta(ref.name, dep, ref.name)
    if settings.cluster_issuer:
        ingress_meta["annotations"] = {"cert-manager.io/cluster-issuer": settings.cluster_issuer}
    host = ref.host(dep, settings)
    ingress_spec: dict[str, Any] = {
        "ingressClassName": settings.ingress_class,
        "rules": [
            {
                "host": host,
                "http": {
                    "paths": [
                        {
                            "path": exposed.expose.path,
                            "pathType": "Prefix",
                            "backend": {"service": {"name": ref.name, "port": {"name": "http"}}},
                        }
                    ]
                },
            }
        ],
    }
    # Always a tls section: without one the ingress controller serves plain HTTP only.
    # No secretName means the controller's default certificate (a cluster wildcard).
    tls: dict[str, Any] = {"hosts": [host]}
    if settings.tls_secret:
        tls["secretName"] = settings.tls_secret
    elif settings.cluster_issuer:
        tls["secretName"] = f"{ref.name}-tls"
    ingress_spec["tls"] = [tls]
    ingress = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": ingress_meta,
        "spec": ingress_spec,
    }
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

    objects = [_namespace(dep), *_quota_and_limits(spec, dep, len(refs)), *_network_policies(spec, dep, settings)]
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
                url=_checked_url(url_tpl.format(**fmt), ref.host(dep, settings)) if url_tpl else None,
                username=username,
                password=passwords[ref.name] if pw else None,
            )
        )
    return entries
