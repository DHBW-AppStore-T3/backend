"""The app contract v2 (``appstore.yaml``) as the API uses it (.github#4).

An app version has an ``appstore.yaml`` at its commit (it runs as pods); a
version without a valid one cannot be submitted, approved or deployed. The
file is read from exactly the commit being submitted, approved or deployed,
validated against the platform limits, and turned into what the rest of the
API needs: the runtime, the spec hash and image digests a review covers, and
the wizard variables. Ported from the pod MVP.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any

import yaml
from fastapi import HTTPException, status

from app.config import settings
from app.k8s.spec import (
    AppSpec,
    PlatformLimits,
    SpecValidationError,
    apply_network_overrides,
    apply_resource_overrides,
    limits_from,
    parse_spec,
)
from app.models import RUNTIME_K8S, RUNTIME_KUBEVIRT
from app.utils import safe_yaml

SPEC_FILE = "appstore.yaml"


@dataclass(frozen=True)
class LoadedSpec:
    spec: AppSpec
    sha256: str
    image_digests: tuple[str, ...]

    @property
    def runtime(self) -> str:
        return RUNTIME_KUBEVIRT if self.spec.runtime == "kubevirt-vm" else RUNTIME_K8S

    def payload(self) -> dict[str, Any]:
        """The spec as the pod worker gets it in the job (it validates it again)."""
        data = self.spec.model_dump(mode="json", by_alias=True, exclude_none=True)
        # Left out at its default so that a worker that predates the field
        # (it forbids unknown keys) still accepts apps that do not use it.
        if data.get("podNetwork") == "none":
            del data["podNetwork"]
        return data


def platform_limits() -> PlatformLimits:
    """Limits from the settings (``APP_IMAGE_REGISTRY_ALLOWLIST``, ``APP_MAX_*``, ``APP_VM_IMAGE_NAMESPACES``)."""
    return limits_from(
        settings.APP_IMAGE_REGISTRY_ALLOWLIST,
        settings.APP_MAX_CPU,
        settings.APP_MAX_MEMORY,
        settings.APP_MAX_STORAGE,
        settings.APP_VM_IMAGE_NAMESPACES,
    )


def parse_checked(raw: bytes) -> LoadedSpec:
    """Parse and validate ``raw``; 422 with one field path per problem (``errors``)."""
    try:
        # The file comes from somebody else's repository: no aliases, no huge or deeply nested
        # documents (they would be expanded or recursed into before any field is checked).
        try:
            document: Any = safe_yaml.load(raw)
        except safe_yaml.UnsafeYaml as exc:
            raise SpecValidationError([{"loc": "<root>", "msg": str(exc)}]) from exc
        except yaml.YAMLError as exc:
            raise SpecValidationError([{"loc": "<root>", "msg": f"invalid YAML: {exc}"}]) from exc
        spec = parse_spec(document if isinstance(document, dict) else raw, platform_limits())
    except SpecValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "reason": "invalid_app_spec",
                "message": "appstore.yaml is invalid",
                "errors": exc.errors,
            },
        ) from exc
    digests = (
        tuple(sorted({c.image.split("@", 1)[1] for c in spec.workload.containers}))
        if spec.workload
        else ()
    )
    return LoadedSpec(spec=spec, sha256=hashlib.sha256(raw).hexdigest(), image_digests=digests)


def with_resource_overrides(loaded: LoadedSpec, overrides: Any) -> LoadedSpec:
    """``loaded`` with the cpu/memory a person chose in the wizard (``userInputVar.resources``).

    Only the job gets the changed spec; hash and digests stay those of the
    reviewed file, which is why the approval still matches. 422 with one field
    path per problem.
    """
    if not overrides:
        return loaded
    try:
        spec = apply_resource_overrides(loaded.spec, overrides, platform_limits())
    except SpecValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "reason": "invalid_resources",
                "message": "resources are invalid",
                "errors": exc.errors,
            },
        ) from exc
    return dataclasses.replace(loaded, spec=spec)


def with_network_overrides(loaded: LoadedSpec, choice: Any) -> LoadedSpec:
    """``loaded`` with the network settings a person chose (``userInputVar.network``).

    Like the resources, only the job gets the changed spec; hash and digests
    stay those of the reviewed file. 422 with one field path per problem.
    """
    if choice is None or choice == {}:
        return loaded
    try:
        spec = apply_network_overrides(loaded.spec, choice)
    except SpecValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "reason": "invalid_network",
                "message": "network settings are invalid",
                "errors": exc.errors,
            },
        ) from exc
    return dataclasses.replace(loaded, spec=spec)


def read_spec_from_repo(repo_path: str) -> LoadedSpec:
    """The validated spec in a cloned commit; 422 ``appstore_yaml_missing`` without one."""
    path = os.path.join(repo_path, SPEC_FILE)
    if not os.path.isfile(path):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"reason": "appstore_yaml_missing", "message": f"the repository has no {SPEC_FILE}"},
        )
    with open(path, "rb") as fh:
        return parse_checked(fh.read())


def digests_json(loaded: LoadedSpec) -> str:
    """The image digests as stored on an approval."""
    return json.dumps(list(loaded.image_digests))


def spec_variables(spec: AppSpec) -> list[dict[str, Any]]:
    """The spec's variables in the shape ``GET /apps/{id}/variables`` returns.

    The keys are the ones the wizard renders and the API validates
    (``osType`` and friends are historical names): values
    travel in ``userInputVar.terraform`` (``source``), a file variable is
    ``osType`` ``file`` with scope ``all`` or ``team``, an enum has ``osType``
    ``enum`` with its ``values``.
    """
    out: list[dict[str, Any]] = []
    for v in spec.variables:
        scope = "team" if v.scope == "team" else None
        entry: dict[str, Any] = {
            "name": v.name,
            "type": "string",
            "description": None,
            "default": v.default,
            "required": v.default is None and v.type == "string",
            "source": "terraform",
            "osType": None,
            "osMode": None,
            "osMulti": None,
            "osScope": None,
            "varScope": scope,
            "fileExtensions": None,
        }
        if v.type == "file":
            file_scope = scope or "all"
            entry.update(
                osType="file",
                osScope=file_scope,
                varScope=file_scope,
                required=False,
                fileExtensions=[e.lstrip(".") for e in v.ext] or None,
            )
        elif v.type == "enum":
            entry.update(osType="enum", values=list(v.values), required=False)
        out.append(entry)
    return out
