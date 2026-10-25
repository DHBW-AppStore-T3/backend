"""Tests for kubevirt-vm runtime in spec.py and render.py."""

import copy

import pytest

from app.k8s.render import (
    DEPLOYMENT_LABEL,
    DeploymentCtx,
    Member,
    RenderSettings,
    Team,
    render_vm,
    workload_refs,
)
from app.k8s.spec import SpecValidationError, parse_spec
from tests.unit.test_k8s_spec import VALID

pytestmark = pytest.mark.unit

VALID_VM_SPEC = {
    "apiVersion": "appstore/v2",
    "name": "windows-matlab",
    "runtime": "kubevirt-vm",
    "scope": "user",
    "vm": {
        "cpuCores": 4,
        "memoryGiB": 8,
        "diskGiB": 80,
        "imageRef": "windows11-matlab-golden",
        "storageClass": "cinder",
        "vncAccess": True,
    },
    "access": [{"type": "url", "template": "https://{workload}-{deployment}.apps.{zone}"}],
}

DEP = DeploymentCtx(id="123e4567-e89b-12d3-a456-426614174000", owner="owner-id")
SETTINGS = RenderSettings(zone="t3-k8s.example.org", cluster_issuer="letsencrypt")
TEAMS = [Team("team-a", (Member(user_id="uid-1", name="alice"),))]


# ── spec.py tests ──────────────────────────────────────────────────────────────


def test_kubevirt_vm_spec_parses():
    spec = parse_spec(VALID_VM_SPEC)
    assert spec.runtime == "kubevirt-vm"
    assert spec.vm is not None
    assert spec.vm.cpu_cores == 4
    assert spec.vm.memory_gib == 8
    assert spec.vm.disk_gib == 80
    assert spec.vm.image_ref == "windows11-matlab-golden"
    assert spec.vm.storage_class == "cinder"
    assert spec.vm.vnc_access is True


def test_kubevirt_vm_without_vm_block_is_rejected():
    data = {**VALID_VM_SPEC}
    del data["vm"]
    with pytest.raises(SpecValidationError) as exc:
        parse_spec(data)
    assert any("vm" in e["msg"].lower() or "kubevirt" in e["msg"].lower() for e in exc.value.errors)


def test_kubernetes_runtime_with_vm_block_is_rejected():
    data = copy.deepcopy(VALID)
    data["vm"] = {
        "cpuCores": 4,
        "memoryGiB": 8,
        "diskGiB": 80,
        "imageRef": "x",
        "storageClass": "cinder",
    }
    with pytest.raises(SpecValidationError) as exc:
        parse_spec(data)
    assert any("vm" in e["msg"].lower() for e in exc.value.errors)


def test_kubevirt_vm_defaults():
    data = {**VALID_VM_SPEC, "vm": {"imageRef": "win11"}}
    spec = parse_spec(data)
    assert spec.vm.cpu_cores == 4
    assert spec.vm.memory_gib == 8
    assert spec.vm.disk_gib == 80
    assert spec.vm.storage_class == "cinder"
    assert spec.vm.vnc_access is True


def test_kubevirt_vm_cpu_cores_out_of_range():
    data = {**VALID_VM_SPEC, "vm": {**VALID_VM_SPEC["vm"], "cpuCores": 32}}
    with pytest.raises(SpecValidationError):
        parse_spec(data)


def test_kubevirt_vm_disk_gib_below_minimum():
    data = {**VALID_VM_SPEC, "vm": {**VALID_VM_SPEC["vm"], "diskGiB": 10}}
    with pytest.raises(SpecValidationError):
        parse_spec(data)


def test_kubevirt_vm_invalid_image_ref():
    data = {
        **VALID_VM_SPEC,
        "vm": {**VALID_VM_SPEC["vm"], "imageRef": "INVALID IMAGE REF!"},
    }
    with pytest.raises(SpecValidationError):
        parse_spec(data)


# ── render_vm tests ─────────────────────────────────────────────────────────────


def _build_vm(spec_data=VALID_VM_SPEC, teams=TEAMS, settings=SETTINGS):
    spec = parse_spec(spec_data)
    refs = workload_refs(spec, teams)
    passwords = {r.name: f"vnc-pw-{r.name}" for r in refs}
    return spec, render_vm(spec, DEP, teams, settings, passwords), passwords


def of_kind(objs, kind):
    return [o for o in objs if o["kind"] == kind]


def test_render_vm_returns_expected_object_kinds():
    _, objs, _ = _build_vm()
    kinds = {o["kind"] for o in objs}
    assert "Namespace" in kinds
    assert "NetworkPolicy" in kinds
    assert "PersistentVolumeClaim" in kinds
    assert "VirtualMachine" in kinds
    assert "Service" in kinds
    assert "Deployment" in kinds
    assert "Ingress" in kinds
    assert "Secret" in kinds


def test_render_vm_namespace_is_privileged():
    _, objs, _ = _build_vm()
    ns = next(o for o in objs if o["kind"] == "Namespace")
    assert ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "privileged"
    assert ns["metadata"]["labels"]["appstore.dhbw/vm-namespace"] == "true"


def test_render_vm_pvc_uses_cinder_and_clones_image():
    _, objs, _ = _build_vm()
    pvcs = of_kind(objs, "PersistentVolumeClaim")
    assert len(pvcs) == 1
    pvc = pvcs[0]
    assert pvc["spec"]["storageClassName"] == "cinder"
    assert pvc["spec"]["resources"]["requests"]["storage"] == "80Gi"
    ds = pvc["spec"]["dataSourceRef"]
    assert ds["name"] == "windows11-matlab-golden"
    assert ds["namespace"] == "vm-poc"


def test_render_vm_virtual_machine_has_correct_resources():
    _, objs, _ = _build_vm()
    vms = of_kind(objs, "VirtualMachine")
    assert len(vms) == 1
    vm = vms[0]
    domain = vm["spec"]["template"]["spec"]["domain"]
    assert domain["cpu"]["cores"] == 4
    assert domain["memory"]["guest"] == "8Gi"
    assert domain["firmware"]["bootloader"]["efi"]["secureBoot"] is False
    assert domain["devices"]["tpm"] == {}


def test_render_vm_ingress_points_to_novnc():
    _, objs, _ = _build_vm()
    ingresses = of_kind(objs, "Ingress")
    assert len(ingresses) == 1
    backend = ingresses[0]["spec"]["rules"][0]["http"]["paths"][0]["backend"]
    assert "novnc" in backend["service"]["name"]
    assert backend["service"]["port"]["name"] == "http"


def test_render_vm_secret_contains_vnc_password():
    _, objs, passwords = _build_vm()
    refs = workload_refs(parse_spec(VALID_VM_SPEC), TEAMS)
    secret = next(o for o in objs if o["kind"] == "Secret")
    assert secret["stringData"]["password"] == passwords[refs[0].name]


def test_render_vm_raises_for_wrong_runtime():
    from tests.unit.test_k8s_spec import VALID

    spec = parse_spec(VALID)
    with pytest.raises(ValueError, match="kubevirt-vm"):
        render_vm(spec, DEP, TEAMS, SETTINGS, {})


def test_render_vm_raises_when_passwords_missing():
    spec = parse_spec(VALID_VM_SPEC)
    with pytest.raises(ValueError, match="missing passwords"):
        render_vm(spec, DEP, TEAMS, SETTINGS, {})


def test_render_vm_deployment_label_on_all_objects():
    _, objs, _ = _build_vm()
    for obj in objs:
        labels = obj.get("metadata", {}).get("labels", {})
        if obj["kind"] != "Namespace":
            assert (
                DEPLOYMENT_LABEL in labels
            ), f"{obj['kind']} {obj['metadata']['name']} missing deployment label"


def test_render_vm_network_policies_include_default_deny():
    _, objs, _ = _build_vm()
    policies = of_kind(objs, "NetworkPolicy")
    names = [p["metadata"]["name"] for p in policies]
    assert "default-deny" in names
    assert "allow-dns" in names
    assert "allow-ingress-controller" in names
