"""App contract v2 (``appstore.yaml``) — parser and validation.

A Kubernetes app is described by a small declarative spec instead of
Terraform/Packer code. The platform renders every Kubernetes object itself
(see :mod:`app.k8s.render`); the app author only chooses image (by digest),
port, resources within platform limits, storage, variables and access.
Security-relevant fields (securityContext, network policy, RBAC, ...) are
**not** part of the spec and cannot be set from it — unknown fields are
rejected.

Validation errors are collected as ``SpecValidationError.errors`` — a list of
``{"loc": "workload.containers[0].image", "msg": "..."}`` — so the API can
answer 422 with a field path.

Ported from the pod MVP (.github#4). The module only depends on pydantic and
PyYAML. It is identical in backend and worker (``test_shared_files``).
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass, field
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    model_validator,
)

API_VERSION = "appstore/v2"

# registry[:port]/repo/path@sha256:<64 hex>. Tags are mutable, so only digests.
_IMAGE_DIGEST_RE = re.compile(
    r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]+)?(/[a-z0-9._-]+)+@sha256:[0-9a-f]{64}$"
)
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CPU_RE = re.compile(r"^(\d+)m$|^(\d+(\.\d+)?)$")
_MEM_RE = re.compile(r"^(\d+)(Mi|Gi)$")
_SIZE_RE = re.compile(r"^(\d+)(Ki|Mi|Gi)$")
# One segment of a path the spec names (ingress path, mount paths). Plain
# names only: the ingress controller builds its routing rule from the ingress
# path (Traefik writes PathPrefix(`<path>`)), so a backtick, bracket or
# operator could add a rule of its own and capture other hosts' traffic.
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
MAX_PATH_LENGTH = 255

ACCESS_URL_PLACEHOLDERS = {"workload", "deployment", "zone", "domain"}
ACCESS_USERNAME_PLACEHOLDERS = {"user", "team"}

# Kubernetes Secrets are capped at 1 MiB. Larger file variables would need an
# init container that writes into the PVC (open point in .github#4, §3.1).
MAX_FILE_VARIABLE_BYTES = 1024 * 1024


def parse_cpu_millicores(value: str) -> int:
    m = _CPU_RE.match(value)
    if not m:
        raise ValueError(f"invalid CPU quantity {value!r} (use e.g. '500m' or '1')")
    return (
        int(m.group(1)) if m.group(1) is not None else round(float(m.group(2)) * 1000)
    )


def parse_memory_mib(value: str) -> int:
    m = _MEM_RE.match(value)
    if not m:
        raise ValueError(
            f"invalid memory quantity {value!r} (use e.g. '512Mi' or '1Gi')"
        )
    return int(m.group(1)) * (1024 if m.group(2) == "Gi" else 1)


def parse_storage_mib(value: str) -> int:
    """A storage size (``5Gi``, ``512Mi``) in MiB."""
    return _storage_mib(value)


def registry_prefixes(value: str) -> tuple[str, ...]:
    """A comma-separated registry allowlist setting as prefixes ending in ``/``.

    Without the slash ``ghcr.io/dhbw-appstore-t3`` would also admit
    ``ghcr.io/dhbw-appstore-t3-evil/...``.
    """
    out = []
    for raw in value.split(","):
        prefix = raw.strip()
        if prefix:
            out.append(prefix if prefix.endswith("/") else prefix + "/")
    return tuple(out)


def parse_size_bytes(value: str) -> int:
    m = _SIZE_RE.match(value)
    if not m:
        raise ValueError(f"invalid size {value!r} (use e.g. '5Mi')")
    return int(m.group(1)) * {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3}[m.group(2)]


def check_path(path: str, what: str, *, ingress: bool = False) -> None:
    """Raise ValueError unless ``path`` is absolute and made of plain segments.

    No ``.``/``..`` segments, no empty segments, nothing but letters, digits,
    ``.``, ``_`` and ``-`` in a segment. ``ingress``: the path of an ingress
    rule, which may be ``/`` and may end in ``/``; a mount path may do
    neither.
    """
    if not path.startswith("/") or len(path) > MAX_PATH_LENGTH:
        raise ValueError(
            f"{what} must be an absolute path of at most {MAX_PATH_LENGTH} characters"
        )
    if path == "/":
        if ingress:
            return
        raise ValueError(f"{what} must not be '/'")
    segments = path[1:].split("/")
    if ingress and segments[-1] == "":
        segments = segments[:-1]
    for segment in segments:
        if segment in (".", "..") or not _PATH_SEGMENT_RE.match(segment):
            raise ValueError(
                f"{what} {path!r} may only contain plain segments (letters, digits, '.', '_', '-')"
            )


class SpecValidationError(ValueError):
    """Spec is invalid; ``errors`` carries one entry per problem with a field path."""

    def __init__(self, errors: list[dict[str, str]]):
        self.errors = errors
        super().__init__("; ".join(f"{e['loc']}: {e['msg']}" for e in errors))


@dataclass(frozen=True)
class PlatformLimits:
    """Upper bounds and allowlist the spec is validated against."""

    # The apps' packages only, not every package of the organisation (backend, worker, ...).
    image_registry_allowlist: tuple[str, ...] = ("ghcr.io/dhbw-appstore-t3/apps/",)
    max_cpu_millicores: int = 2000  # APP_MAX_CPU
    max_memory_mib: int = 4096  # APP_MAX_MEMORY
    max_storage_mib: int = 20 * 1024  # APP_MAX_STORAGE
    max_containers: int = 3
    extra: dict[str, Any] = field(default_factory=dict)


def limits_from(
    registry_allowlist: str, max_cpu: str, max_memory: str, max_storage: str
) -> PlatformLimits:
    """Platform limits from their settings (``APP_IMAGE_REGISTRY_ALLOWLIST``, ``APP_MAX_*``)."""
    return PlatformLimits(
        image_registry_allowlist=registry_prefixes(registry_allowlist),
        max_cpu_millicores=parse_cpu_millicores(max_cpu),
        max_memory_mib=parse_memory_mib(max_memory),
        max_storage_mib=parse_storage_mib(max_storage),
    )


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Expose(_Strict):
    port: int = Field(ge=1024, le=65535)  # non-root containers cannot bind <1024
    path: str = "/"

    @model_validator(mode="after")
    def _path(self) -> Expose:
        check_path(self.path, "path", ingress=True)
        return self


class ResourceRequests(_Strict):
    cpu: str
    memory: str


class Resources(_Strict):
    """``cpu``/``memory`` are the container's limits. ``requests`` is what the
    scheduler reserves; it defaults to the limits. A mostly idle app (an IDE)
    reserves little and may burst up to its limits."""

    cpu: str
    memory: str
    requests: ResourceRequests | None = None

    @model_validator(mode="after")
    def _quantities(self) -> Resources:
        parse_cpu_millicores(self.cpu)
        parse_memory_mib(self.memory)
        if self.requests is not None:
            if parse_cpu_millicores(self.requests.cpu) > parse_cpu_millicores(self.cpu):
                raise ValueError("requests.cpu must not exceed cpu")
            if parse_memory_mib(self.requests.memory) > parse_memory_mib(self.memory):
                raise ValueError("requests.memory must not exceed memory")
        return self

    @property
    def request_cpu(self) -> str:
        return self.requests.cpu if self.requests else self.cpu

    @property
    def request_memory(self) -> str:
        return self.requests.memory if self.requests else self.memory


class Range(_Strict):
    """Inclusive ``min``..``max`` of one resource, in the notation of its quantity."""

    min: str
    max: str


class ResourceBounds(_Strict):
    """What a person may choose for a container's ``cpu``/``memory`` limits.

    Opt-in per container: without ``resourceBounds`` the container always runs
    with ``resources``, which are then the defaults of this range.
    """

    cpu: Range
    memory: Range

    @model_validator(mode="after")
    def _quantities(self) -> ResourceBounds:
        if parse_cpu_millicores(self.cpu.min) > parse_cpu_millicores(self.cpu.max):
            raise ValueError("cpu.min must not exceed cpu.max")
        if parse_memory_mib(self.memory.min) > parse_memory_mib(self.memory.max):
            raise ValueError("memory.min must not exceed memory.max")
        return self


class EnvVar(_Strict):
    name: str
    source: Literal["generated-password", "team-name", "user-name"] | None = Field(
        default=None, alias="from"
    )
    value: str | None = None

    @model_validator(mode="after")
    def _one_source(self) -> EnvVar:
        if not _ENV_NAME_RE.match(self.name):
            raise ValueError(f"invalid environment variable name {self.name!r}")
        if (self.source is None) == (self.value is None):
            raise ValueError("exactly one of 'from' and 'value' is required")
        return self


class Container(_Strict):
    name: str
    image: str
    expose: Expose | None = None
    resources: Resources
    resource_bounds: ResourceBounds | None = Field(default=None, alias="resourceBounds")
    env: list[EnvVar] = Field(default_factory=list)
    writable_paths: list[str] = Field(default_factory=list, alias="writablePaths")

    @model_validator(mode="after")
    def _check(self) -> Container:
        if not _NAME_RE.match(self.name):
            raise ValueError(
                f"invalid container name {self.name!r} (lowercase letters, digits, '-')"
            )
        if not _IMAGE_DIGEST_RE.match(self.image):
            raise ValueError(
                "image must be pinned by digest: <registry>/<repo>@sha256:<64 hex> (tags are not allowed)"
            )
        b = self.resource_bounds
        if b is not None:
            cpu, mem = (
                parse_cpu_millicores(self.resources.cpu),
                parse_memory_mib(self.resources.memory),
            )
            if (
                not parse_cpu_millicores(b.cpu.min)
                <= cpu
                <= parse_cpu_millicores(b.cpu.max)
            ):
                raise ValueError(
                    "resources.cpu (the default) must lie within resourceBounds.cpu"
                )
            if (
                not parse_memory_mib(b.memory.min)
                <= mem
                <= parse_memory_mib(b.memory.max)
            ):
                raise ValueError(
                    "resources.memory (the default) must lie within resourceBounds.memory"
                )
        for p in self.writable_paths:
            check_path(p, "writablePaths entry")
        if len(set(self.writable_paths)) != len(self.writable_paths):
            raise ValueError("writablePaths must be unique")
        return self


class Storage(_Strict):
    size: str
    mount_path: str = Field(alias="mountPath")

    @model_validator(mode="after")
    def _check(self) -> Storage:
        _storage_mib(self.size)
        check_path(self.mount_path, "mountPath")
        return self


def _storage_mib(value: str) -> int:
    m = _MEM_RE.match(value)
    if not m:
        raise ValueError(f"invalid storage size {value!r} (use e.g. '5Gi')")
    return int(m.group(1)) * (1024 if m.group(2) == "Gi" else 1)


class Workload(_Strict):
    containers: list[Container] = Field(min_length=1)
    storage: Storage | None = None

    @model_validator(mode="after")
    def _one_exposed(self) -> Workload:
        names = [c.name for c in self.containers]
        if len(set(names)) != len(names):
            raise ValueError("container names must be unique")
        if sum(c.expose is not None for c in self.containers) != 1:
            raise ValueError("exactly one container must set 'expose'")
        if self.storage is not None and any(
            self.storage.mount_path in c.writable_paths for c in self.containers
        ):
            raise ValueError("storage.mountPath must not also be a writablePaths entry")
        return self


class Variable(_Strict):
    name: str
    type: Literal["file", "enum", "string"]
    scope: Literal["deployment", "team"] = "deployment"
    ext: list[str] = Field(default_factory=list)
    max_size: str | None = Field(default=None, alias="maxSize")
    mount_path: str | None = Field(default=None, alias="mountPath")
    values: list[str] = Field(default_factory=list)
    default: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Variable:
        if not _NAME_RE.match(self.name.replace("_", "-")):
            raise ValueError(f"invalid variable name {self.name!r}")
        if self.type == "file":
            if not self.mount_path:
                raise ValueError("file variables need an absolute mountPath")
            check_path(self.mount_path, "mountPath")
            if (
                self.max_size is not None
                and parse_size_bytes(self.max_size) > MAX_FILE_VARIABLE_BYTES
            ):
                raise ValueError(
                    "maxSize above 1Mi is not supported yet (Secret mount limit)"
                )
        if self.type == "enum":
            if not self.values:
                raise ValueError("enum variables need 'values'")
            if self.default is not None and self.default not in self.values:
                raise ValueError("default must be one of 'values'")
        return self


class Access(_Strict):
    type: Literal["url", "password"]
    template: str | None = None
    source: Literal["generated-password"] | None = Field(default=None, alias="from")
    username: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Access:
        if self.type == "url":
            if not self.template:
                raise ValueError("url access needs 'template'")
            # Shown to students as a link: never another scheme.
            if not self.template.startswith("https://"):
                raise ValueError("url access must start with 'https://'")
            _check_placeholders(self.template, ACCESS_URL_PLACEHOLDERS)
        else:
            if self.source is None:
                raise ValueError("password access needs 'from: generated-password'")
            if self.username:
                _check_placeholders(self.username, ACCESS_USERNAME_PLACEHOLDERS)
        return self


def _check_placeholders(template: str, allowed: set[str]) -> None:
    for _, field_name, _, _ in string.Formatter().parse(template):
        if field_name is not None and field_name not in allowed:
            raise ValueError(
                f"unknown placeholder {{{field_name}}} (allowed: {', '.join(sorted(allowed))})"
            )


class UserChoices(_Strict):
    """Settings the author lets the person deploying change (opt-in, off by default)."""

    internet: StrictBool = False
    pod_network: StrictBool = Field(default=False, alias="podNetwork")


class VmSpec(_Strict):
    """KubeVirt VM spec for runtime kubevirt-vm."""

    cpu_cores: int = Field(default=4, ge=1, le=16, alias="cpuCores")
    memory_gib: int = Field(default=8, ge=2, le=32, alias="memoryGiB")
    disk_gib: int = Field(default=80, ge=40, le=200, alias="diskGiB")
    # PVC name in the vm-images namespace used as clone source
    image_ref: str = Field(alias="imageRef")
    storage_class: str = Field(default="cinder", alias="storageClass")
    vnc_access: bool = Field(default=True, alias="vncAccess")

    @model_validator(mode="after")
    def _check(self) -> VmSpec:
        if not re.match(r"^[a-z][a-z0-9-]{0,62}$", self.image_ref):
            raise ValueError(
                f"invalid imageRef {self.image_ref!r} (lowercase letters, digits, '-')"
            )
        return self


class AppSpec(_Strict):
    api_version: Literal["appstore/v2"] = Field(alias="apiVersion")
    name: str
    runtime: Literal["kubernetes", "openstack-vm", "kubevirt-vm"]
    scope: Literal["team", "user"] = "team"
    workload: Workload | None = None
    vm: VmSpec | None = None
    egress: Literal["none", "internet"] = "none"
    pod_network: Literal["none", "namespace"] = Field(
        default="none", alias="podNetwork"
    )
    user_choices: UserChoices | None = Field(default=None, alias="userChoices")
    variables: list[Variable] = Field(default_factory=list)
    access: list[Access] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> AppSpec:
        if not _NAME_RE.match(self.name):
            raise ValueError(
                f"invalid app name {self.name!r} (lowercase letters, digits, '-')"
            )
        if self.runtime == "kubernetes" and self.workload is None:
            raise ValueError("runtime 'kubernetes' requires 'workload'")
        if self.runtime == "openstack-vm" and self.workload is not None:
            raise ValueError(
                "runtime 'openstack-vm' uses terraform/ + packer/, 'workload' is not allowed"
            )
        if self.runtime == "kubevirt-vm" and self.vm is None:
            raise ValueError("runtime 'kubevirt-vm' requires 'vm'")
        if self.runtime != "kubevirt-vm" and self.vm is not None:
            raise ValueError("'vm' is only for runtime 'kubevirt-vm'")
        names = [v.name for v in self.variables]
        if len(set(names)) != len(names):
            raise ValueError("variable names must be unique")
        # File variables are mounted into the exposed container, next to its
        # other mounts; two mounts at one path are refused by the cluster.
        files = [
            v.mount_path for v in self.variables if v.type == "file" and v.mount_path
        ]
        if len(set(files)) != len(files):
            raise ValueError("file variables need distinct mountPaths")
        if self.workload is not None:
            exposed = next((c for c in self.workload.containers if c.expose), None)
            taken = set(exposed.writable_paths if exposed else ())
            if self.workload.storage is not None:
                taken.add(self.workload.storage.mount_path)
            if taken.intersection(files):
                raise ValueError(
                    "a file variable's mountPath is already used by writablePaths or storage"
                )
        return self


def _loc(parts: tuple[Any, ...]) -> str:
    out = ""
    for p in parts:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else str(p))
    return out or "<root>"


def _limit_errors(spec: AppSpec, limits: PlatformLimits) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    if spec.workload is None:
        return errors
    if len(spec.workload.containers) > limits.max_containers:
        errors.append(
            {
                "loc": "workload.containers",
                "msg": f"at most {limits.max_containers} containers",
            }
        )
    for i, c in enumerate(spec.workload.containers):
        base = f"workload.containers[{i}]"
        if not c.image.startswith(limits.image_registry_allowlist):
            allowed = ", ".join(limits.image_registry_allowlist)
            errors.append(
                {
                    "loc": f"{base}.image",
                    "msg": f"registry not allowed (allowed prefixes: {allowed})",
                }
            )
        if parse_cpu_millicores(c.resources.cpu) > limits.max_cpu_millicores:
            errors.append(
                {
                    "loc": f"{base}.resources.cpu",
                    "msg": f"exceeds platform limit of {limits.max_cpu_millicores}m",
                }
            )
        if parse_memory_mib(c.resources.memory) > limits.max_memory_mib:
            errors.append(
                {
                    "loc": f"{base}.resources.memory",
                    "msg": f"exceeds platform limit of {limits.max_memory_mib}Mi",
                }
            )
        if c.resource_bounds is not None:
            if (
                parse_cpu_millicores(c.resource_bounds.cpu.max)
                > limits.max_cpu_millicores
            ):
                errors.append(
                    {
                        "loc": f"{base}.resourceBounds.cpu.max",
                        "msg": f"exceeds platform limit of {limits.max_cpu_millicores}m",
                    }
                )
            if parse_memory_mib(c.resource_bounds.memory.max) > limits.max_memory_mib:
                errors.append(
                    {
                        "loc": f"{base}.resourceBounds.memory.max",
                        "msg": f"exceeds platform limit of {limits.max_memory_mib}Mi",
                    }
                )
    storage = spec.workload.storage
    if storage is not None and _storage_mib(storage.size) > limits.max_storage_mib:
        errors.append(
            {
                "loc": "workload.storage.size",
                "msg": f"exceeds platform limit of {limits.max_storage_mib}Mi",
            }
        )
    return errors


def validate_spec(spec: AppSpec, limits: PlatformLimits | None = None) -> AppSpec:
    """Check ``spec`` against platform limits and allowlist. Returns it unchanged."""
    errors = _limit_errors(spec, limits or PlatformLimits())
    if errors:
        raise SpecValidationError(errors)
    return spec


def parse_spec(
    text: str | bytes | dict[str, Any], limits: PlatformLimits | None = None
) -> AppSpec:
    """Parse and fully validate an ``appstore.yaml``. Raises :class:`SpecValidationError`."""
    if isinstance(text, dict):
        data: Any = text
    else:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise SpecValidationError(
                [{"loc": "<root>", "msg": f"invalid YAML: {exc}"}]
            ) from exc
    if not isinstance(data, dict):
        raise SpecValidationError(
            [{"loc": "<root>", "msg": "spec must be a YAML mapping"}]
        )
    try:
        spec = AppSpec.model_validate(data)
    except ValidationError as exc:
        raise SpecValidationError(
            [
                {"loc": _loc(e["loc"]), "msg": e["msg"].removeprefix("Value error, ")}
                for e in exc.errors()
            ]
        ) from exc
    return validate_spec(spec, limits)


def _format_cpu(millicores: int) -> str:
    return str(millicores // 1000) if millicores % 1000 == 0 else f"{millicores}m"


def _format_memory(mib: int) -> str:
    return f"{mib // 1024}Gi" if mib % 1024 == 0 else f"{mib}Mi"


def apply_resource_overrides(
    spec: AppSpec, overrides: Any, limits: PlatformLimits | None = None
) -> AppSpec:
    """A copy of ``spec`` with the ``cpu``/``memory`` a person chose per container.

    ``overrides`` maps a container name to ``{"cpu": ..., "memory": ...}`` (either
    key may be left out). Only containers with ``resourceBounds`` can be
    changed, and only within them and the platform limits. The scheduler
    request keeps its ratio to the limit, so a mostly idle app stays one.
    Raises :class:`SpecValidationError` with one field path per problem.
    """
    if not overrides:
        return spec
    limits = limits or PlatformLimits()
    errors: list[dict[str, str]] = []
    if not isinstance(overrides, dict) or spec.workload is None:
        raise SpecValidationError(
            [{"loc": "resources", "msg": "must map container names to cpu/memory"}]
        )
    by_name = {c.name: c for c in spec.workload.containers}
    result = spec.model_copy(deep=True)
    new_by_name = {c.name: c for c in result.workload.containers}  # type: ignore[union-attr]
    for name, choice in overrides.items():
        loc = f"resources.{name}"
        container = by_name.get(name)
        if container is None:
            errors.append({"loc": loc, "msg": "unknown container"})
            continue
        if container.resource_bounds is None:
            errors.append(
                {"loc": loc, "msg": "this container's resources cannot be changed"}
            )
            continue
        if not isinstance(choice, dict) or set(choice) - {"cpu", "memory"}:
            errors.append({"loc": loc, "msg": "only cpu and memory can be set"})
            continue
        bounds, current = container.resource_bounds, container.resources
        try:
            cpu = parse_cpu_millicores(choice["cpu"]) if "cpu" in choice else None
            mem = parse_memory_mib(choice["memory"]) if "memory" in choice else None
        except (ValueError, TypeError) as exc:
            errors.append({"loc": loc, "msg": str(exc)})
            continue
        if cpu is not None:
            lo, hi = (
                parse_cpu_millicores(bounds.cpu.min),
                min(parse_cpu_millicores(bounds.cpu.max), limits.max_cpu_millicores),
            )
            if not lo <= cpu <= hi:
                errors.append(
                    {
                        "loc": f"{loc}.cpu",
                        "msg": f"must be between {_format_cpu(lo)} and {_format_cpu(hi)}",
                    }
                )
        if mem is not None:
            lo, hi = (
                parse_memory_mib(bounds.memory.min),
                min(parse_memory_mib(bounds.memory.max), limits.max_memory_mib),
            )
            if not lo <= mem <= hi:
                errors.append(
                    {
                        "loc": f"{loc}.memory",
                        "msg": f"must be between {_format_memory(lo)} and {_format_memory(hi)}",
                    }
                )
        if errors:
            continue
        target = new_by_name[name].resources
        old_cpu, old_mem = parse_cpu_millicores(current.cpu), parse_memory_mib(
            current.memory
        )
        if cpu is not None:
            target.cpu = _format_cpu(cpu)
        if mem is not None:
            target.memory = _format_memory(mem)
        if current.requests is not None:
            new_cpu, new_mem = cpu or old_cpu, mem or old_mem
            req_cpu = parse_cpu_millicores(current.requests.cpu) * new_cpu // old_cpu
            req_mem = parse_memory_mib(current.requests.memory) * new_mem // old_mem
            target.requests = ResourceRequests(
                cpu=_format_cpu(max(1, min(req_cpu, new_cpu))),
                memory=_format_memory(max(1, min(req_mem, new_mem))),
            )
    if errors:
        raise SpecValidationError(errors)
    return result


def resource_choices(
    spec: AppSpec, limits: PlatformLimits | None = None
) -> list[dict[str, Any]]:
    """What the wizard offers: per container with ``resourceBounds`` the default and range."""
    limits = limits or PlatformLimits()
    out: list[dict[str, Any]] = []
    for c in spec.workload.containers if spec.workload else []:
        b = c.resource_bounds
        if b is None:
            continue
        cpu_max = min(parse_cpu_millicores(b.cpu.max), limits.max_cpu_millicores)
        mem_max = min(parse_memory_mib(b.memory.max), limits.max_memory_mib)
        out.append(
            {
                "container": c.name,
                "cpu": {
                    "default": c.resources.cpu,
                    "min": b.cpu.min,
                    "max": _format_cpu(cpu_max),
                },
                "memory": {
                    "default": c.resources.memory,
                    "min": b.memory.min,
                    "max": _format_memory(mem_max),
                },
            }
        )
    return out


def apply_network_overrides(spec: AppSpec, choice: Any) -> AppSpec:
    """A copy of ``spec`` with the network settings a person chose.

    ``choice`` is ``{"internet": bool, "podNetwork": bool}`` (either key may be
    left out). Only keys the author released in ``userChoices`` can be set;
    the rest keeps the author's value. Raises :class:`SpecValidationError`
    with one field path per problem.
    """
    if choice is None or choice == {}:
        return spec
    if not isinstance(choice, dict):
        raise SpecValidationError(
            [{"loc": "network", "msg": "must map internet/podNetwork to true or false"}]
        )
    allowed = spec.user_choices
    errors: list[dict[str, str]] = []
    for key, value in choice.items():
        loc = f"network.{key}"
        if key not in ("internet", "podNetwork"):
            errors.append(
                {"loc": loc, "msg": "unknown setting (allowed: internet, podNetwork)"}
            )
        elif not isinstance(value, bool):
            errors.append({"loc": loc, "msg": "must be true or false"})
        elif allowed is None or not (
            allowed.internet if key == "internet" else allowed.pod_network
        ):
            errors.append({"loc": loc, "msg": "this setting cannot be changed"})
    if errors:
        raise SpecValidationError(errors)
    result = spec.model_copy(deep=True)
    if "internet" in choice:
        result.egress = "internet" if choice["internet"] else "none"
    if "podNetwork" in choice:
        result.pod_network = "namespace" if choice["podNetwork"] else "none"
    return result


def network_choices(spec: AppSpec) -> dict[str, Any]:
    """What the wizard offers: per released setting the author's default."""
    out: dict[str, Any] = {}
    if spec.user_choices is None:
        return out
    if spec.user_choices.internet:
        out["internet"] = {"default": spec.egress == "internet"}
    if spec.user_choices.pod_network:
        out["podNetwork"] = {"default": spec.pod_network == "namespace"}
    return out
