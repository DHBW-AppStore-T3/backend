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
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

API_VERSION = "appstore/v2"

# registry[:port]/repo/path@sha256:<64 hex>. Tags are mutable, so only digests.
_IMAGE_DIGEST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]+)?(/[a-z0-9._-]+)+@sha256:[0-9a-f]{64}$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CPU_RE = re.compile(r"^(\d+)m$|^(\d+(\.\d+)?)$")
_MEM_RE = re.compile(r"^(\d+)(Mi|Gi)$")
_SIZE_RE = re.compile(r"^(\d+)(Ki|Mi|Gi)$")

ACCESS_URL_PLACEHOLDERS = {"workload", "deployment", "zone", "domain"}
ACCESS_USERNAME_PLACEHOLDERS = {"user", "team"}

# Kubernetes Secrets are capped at 1 MiB. Larger file variables would need an
# init container that writes into the PVC (open point in .github#4, §3.1).
MAX_FILE_VARIABLE_BYTES = 1024 * 1024


def parse_cpu_millicores(value: str) -> int:
    m = _CPU_RE.match(value)
    if not m:
        raise ValueError(f"invalid CPU quantity {value!r} (use e.g. '500m' or '1')")
    return int(m.group(1)) if m.group(1) is not None else round(float(m.group(2)) * 1000)


def parse_memory_mib(value: str) -> int:
    m = _MEM_RE.match(value)
    if not m:
        raise ValueError(f"invalid memory quantity {value!r} (use e.g. '512Mi' or '1Gi')")
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


class SpecValidationError(ValueError):
    """Spec is invalid; ``errors`` carries one entry per problem with a field path."""

    def __init__(self, errors: list[dict[str, str]]):
        self.errors = errors
        super().__init__("; ".join(f"{e['loc']}: {e['msg']}" for e in errors))


@dataclass(frozen=True)
class PlatformLimits:
    """Upper bounds and allowlist the spec is validated against."""

    image_registry_allowlist: tuple[str, ...] = ("ghcr.io/dhbw-appstore-t3/",)
    max_cpu_millicores: int = 2000  # APP_MAX_CPU
    max_memory_mib: int = 4096  # APP_MAX_MEMORY
    max_storage_mib: int = 20 * 1024  # APP_MAX_STORAGE
    max_containers: int = 3
    extra: dict[str, Any] = field(default_factory=dict)


def limits_from(registry_allowlist: str, max_cpu: str, max_memory: str, max_storage: str) -> PlatformLimits:
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
        if not self.path.startswith("/"):
            raise ValueError("path must start with '/'")
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


class EnvVar(_Strict):
    name: str
    source: Literal["generated-password", "team-name", "user-name"] | None = Field(default=None, alias="from")
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
    env: list[EnvVar] = Field(default_factory=list)
    writable_paths: list[str] = Field(default_factory=list, alias="writablePaths")

    @model_validator(mode="after")
    def _check(self) -> Container:
        if not _NAME_RE.match(self.name):
            raise ValueError(f"invalid container name {self.name!r} (lowercase letters, digits, '-')")
        if not _IMAGE_DIGEST_RE.match(self.image):
            raise ValueError("image must be pinned by digest: <registry>/<repo>@sha256:<64 hex> (tags are not allowed)")
        for p in self.writable_paths:
            if not p.startswith("/"):
                raise ValueError(f"writablePaths entry {p!r} must be absolute")
        return self


class Storage(_Strict):
    size: str
    mount_path: str = Field(alias="mountPath")

    @model_validator(mode="after")
    def _check(self) -> Storage:
        _storage_mib(self.size)
        if not self.mount_path.startswith("/"):
            raise ValueError("mountPath must be absolute")
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
            if not self.mount_path or not self.mount_path.startswith("/"):
                raise ValueError("file variables need an absolute mountPath")
            if self.max_size is not None and parse_size_bytes(self.max_size) > MAX_FILE_VARIABLE_BYTES:
                raise ValueError("maxSize above 1Mi is not supported yet (Secret mount limit)")
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
            raise ValueError(f"unknown placeholder {{{field_name}}} (allowed: {', '.join(sorted(allowed))})")


class AppSpec(_Strict):
    api_version: Literal["appstore/v2"] = Field(alias="apiVersion")
    name: str
    runtime: Literal["kubernetes", "openstack-vm"]
    scope: Literal["team", "user"] = "team"
    workload: Workload | None = None
    egress: Literal["none", "internet"] = "none"
    variables: list[Variable] = Field(default_factory=list)
    access: list[Access] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> AppSpec:
        if not _NAME_RE.match(self.name):
            raise ValueError(f"invalid app name {self.name!r} (lowercase letters, digits, '-')")
        if self.runtime == "kubernetes" and self.workload is None:
            raise ValueError("runtime 'kubernetes' requires 'workload'")
        if self.runtime == "openstack-vm" and self.workload is not None:
            raise ValueError("runtime 'openstack-vm' uses terraform/ + packer/, 'workload' is not allowed")
        names = [v.name for v in self.variables]
        if len(set(names)) != len(names):
            raise ValueError("variable names must be unique")
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
        errors.append({"loc": "workload.containers", "msg": f"at most {limits.max_containers} containers"})
    for i, c in enumerate(spec.workload.containers):
        base = f"workload.containers[{i}]"
        if not c.image.startswith(limits.image_registry_allowlist):
            allowed = ", ".join(limits.image_registry_allowlist)
            errors.append({"loc": f"{base}.image", "msg": f"registry not allowed (allowed prefixes: {allowed})"})
        if parse_cpu_millicores(c.resources.cpu) > limits.max_cpu_millicores:
            errors.append(
                {"loc": f"{base}.resources.cpu", "msg": f"exceeds platform limit of {limits.max_cpu_millicores}m"}
            )
        if parse_memory_mib(c.resources.memory) > limits.max_memory_mib:
            errors.append(
                {"loc": f"{base}.resources.memory", "msg": f"exceeds platform limit of {limits.max_memory_mib}Mi"}
            )
    storage = spec.workload.storage
    if storage is not None and _storage_mib(storage.size) > limits.max_storage_mib:
        errors.append({"loc": "workload.storage.size", "msg": f"exceeds platform limit of {limits.max_storage_mib}Mi"})
    return errors


def validate_spec(spec: AppSpec, limits: PlatformLimits | None = None) -> AppSpec:
    """Check ``spec`` against platform limits and allowlist. Returns it unchanged."""
    errors = _limit_errors(spec, limits or PlatformLimits())
    if errors:
        raise SpecValidationError(errors)
    return spec


def parse_spec(text: str | bytes | dict[str, Any], limits: PlatformLimits | None = None) -> AppSpec:
    """Parse and fully validate an ``appstore.yaml``. Raises :class:`SpecValidationError`."""
    if isinstance(text, dict):
        data: Any = text
    else:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise SpecValidationError([{"loc": "<root>", "msg": f"invalid YAML: {exc}"}]) from exc
    if not isinstance(data, dict):
        raise SpecValidationError([{"loc": "<root>", "msg": "spec must be a YAML mapping"}])
    try:
        spec = AppSpec.model_validate(data)
    except ValidationError as exc:
        raise SpecValidationError(
            [{"loc": _loc(e["loc"]), "msg": e["msg"].removeprefix("Value error, ")} for e in exc.errors()]
        ) from exc
    return validate_spec(spec, limits)
