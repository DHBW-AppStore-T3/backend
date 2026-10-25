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
from app.k8s.spec import (
    PlatformLimits,
    SpecValidationError,
    apply_network_overrides,
    apply_resource_overrides,
    network_choices,
    parse_spec,
    resource_choices,
)
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
            assert DEPLOYMENT_LABEL in labels, (
                f"{obj['kind']} {obj['metadata']['name']} missing deployment label"
            )


def test_render_vm_network_policies_include_default_deny():
    _, objs, _ = _build_vm()
    policies = of_kind(objs, "NetworkPolicy")
    names = [p["metadata"]["name"] for p in policies]
    assert "default-deny" in names
    assert "allow-dns" in names
    assert "allow-ingress-controller" in names


# ── clone access, VNC proxy, network ───────────────────────────────────────────


def _vm_objects(spec=None, settings=SETTINGS):
    parsed = parse_spec(copy.deepcopy(spec or VALID_VM_SPEC))
    refs = workload_refs(parsed, TEAMS)
    return render_vm(parsed, DEP, TEAMS, settings, {r.name: "pw" for r in refs})


def _by_kind(objects, kind):
    return [o for o in objects if o["kind"] == kind]


def test_render_vm_grants_the_clone_to_exactly_this_namespace():
    (grant,) = _by_kind(_vm_objects(), "ReferenceGrant")

    assert grant["metadata"]["namespace"] == "vm-poc"  # the image namespace, not the deployment's
    assert grant["metadata"]["name"] == f"clone-{DEP.namespace}"
    assert grant["spec"]["from"] == [
        {"group": "", "kind": "PersistentVolumeClaim", "namespace": DEP.namespace}
    ]
    assert grant["spec"]["to"] == [
        {"group": "", "kind": "PersistentVolumeClaim", "name": "windows11-matlab-golden"}
    ]
    (pvc,) = _by_kind(_vm_objects(), "PersistentVolumeClaim")
    assert pvc["spec"]["dataSourceRef"]["namespace"] == "vm-poc"


def test_render_vm_applies_the_grant_before_the_claim_that_needs_it():
    kinds = [o["kind"] for o in _vm_objects()]
    assert kinds.index("ReferenceGrant") < kinds.index("PersistentVolumeClaim")
    assert kinds.index("ServiceAccount") < kinds.index("Deployment")


def test_render_vm_has_no_vnc_service_because_qemu_listens_on_no_pod_port():
    objects = _vm_objects()
    assert not _by_kind(objects, "Service") or all(
        s["metadata"]["name"].endswith("-novnc") for s in _by_kind(objects, "Service")
    )
    (proxy,) = _by_kind(objects, "Deployment")
    pod = proxy["spec"]["template"]["spec"]
    env = {e["name"]: e for e in pod["containers"][0]["env"]}
    assert pod["serviceAccountName"] == "vnc-proxy" and pod["automountServiceAccountToken"] is True
    assert env["VM_NAME"]["value"] == proxy["metadata"]["name"].removesuffix("-novnc")
    assert {"PROXY_USER", "PROXY_PASSWORD"} <= set(env) and "VNC_HOST" not in env
    (secret,) = _by_kind(objects, "Secret")
    assert secret["stringData"] == {"password": "pw", "username": "alice"}


def _policy_names(objects):
    return {o["metadata"]["name"] for o in _by_kind(objects, "NetworkPolicy")}


def test_render_vm_network_is_closed_unless_the_app_opens_it():
    names = _policy_names(_vm_objects())
    assert names == {
        "default-deny",
        "allow-ingress-controller",
        "allow-dns",
        "allow-vnc-proxy-to-apiserver",
    }


def test_render_vm_internet_and_pod_network_follow_the_spec():
    spec = {**VALID_VM_SPEC, "egress": "internet", "podNetwork": "namespace"}
    objects = _vm_objects(spec)
    policies = {o["metadata"]["name"]: o for o in _by_kind(objects, "NetworkPolicy")}

    assert {"allow-egress-internet", "allow-intra-namespace"} <= set(policies)
    # Only the VMs get the internet and see each other, never the proxies.
    for name in ("allow-egress-internet", "allow-intra-namespace"):
        assert policies[name]["spec"]["podSelector"] == {
            "matchLabels": {"appstore.dhbw/role": "vm"}
        }
    internet = policies["allow-egress-internet"]["spec"]["egress"][0]["to"][0]["ipBlock"]
    assert "10.0.0.0/8" in internet["except"]


def test_the_vm_image_namespace_must_be_one_the_platform_allows():
    spec = copy.deepcopy(VALID_VM_SPEC)
    spec["vm"]["imageNamespace"] = "kube-system"

    with pytest.raises(SpecValidationError) as exc:
        parse_spec(spec, PlatformLimits())

    assert [e["loc"] for e in exc.value.errors] == ["vm.imageNamespace"]
    spec["vm"]["imageNamespace"] = "vm-images"
    parse_spec(spec, PlatformLimits(vm_image_namespaces=("vm-images",)))


# ── vCPU / RAM chosen in the wizard ────────────────────────────────────────────

BOUNDED = {
    **VALID_VM_SPEC,
    "vm": {
        **VALID_VM_SPEC["vm"],
        "bounds": {"cpuCores": {"min": 2, "max": 8}, "memoryGiB": {"min": 4, "max": 16}},
    },
}


def test_a_vm_offers_its_bounds_to_the_wizard_like_a_container():
    assert resource_choices(parse_spec(BOUNDED)) == [
        {
            "container": "vm",
            "cpu": {"default": "4", "min": "2", "max": "8"},
            "memory": {"default": "8Gi", "min": "4Gi", "max": "16Gi"},
        }
    ]
    assert resource_choices(parse_spec(VALID_VM_SPEC)) == []


def test_a_person_may_choose_vcpus_and_memory_within_the_bounds():
    chosen = apply_resource_overrides(parse_spec(BOUNDED), {"vm": {"cpu": "6", "memory": "12Gi"}})
    assert (chosen.vm.cpu_cores, chosen.vm.memory_gib) == (6, 12)
    only_cpu = apply_resource_overrides(parse_spec(BOUNDED), {"vm": {"cpu": "2"}})
    assert (only_cpu.vm.cpu_cores, only_cpu.vm.memory_gib) == (2, 8)


@pytest.mark.parametrize(
    ("overrides", "loc"),
    [
        ({"vm": {"cpu": "9"}}, "resources.vm.cpu"),
        ({"vm": {"memory": "2Gi"}}, "resources.vm.memory"),
        ({"vm": {"cpu": "1500m"}}, "resources.vm"),
        ({"vm": {"memory": "1536Mi"}}, "resources.vm"),
        ({"vm": {"disk": "1Ti"}}, "resources.vm"),
        ({"desktop": {"cpu": "2"}}, "resources"),
    ],
)
def test_a_choice_outside_the_bounds_is_refused_with_its_field(overrides, loc):
    with pytest.raises(SpecValidationError) as exc:
        apply_resource_overrides(parse_spec(BOUNDED), overrides)
    assert exc.value.errors[0]["loc"] == loc


def test_a_vm_without_bounds_cannot_be_resized():
    with pytest.raises(SpecValidationError) as exc:
        apply_resource_overrides(parse_spec(VALID_VM_SPEC), {"vm": {"cpu": "2"}})
    assert exc.value.errors[0]["msg"] == "this VM's resources cannot be changed"


def test_the_defaults_must_lie_within_the_bounds():
    spec = copy.deepcopy(BOUNDED)
    spec["vm"]["cpuCores"] = 12
    with pytest.raises(SpecValidationError):
        parse_spec(spec)


def test_a_vm_follows_the_network_choices_the_author_released():
    spec = {**VALID_VM_SPEC, "userChoices": {"internet": True, "podNetwork": True}}
    parsed = parse_spec(spec)
    assert network_choices(parsed) == {
        "internet": {"default": False},
        "podNetwork": {"default": False},
    }

    chosen = apply_network_overrides(parsed, {"internet": True, "podNetwork": True})
    names = _policy_names(
        render_vm(
            chosen.model_copy(deep=True),
            DEP,
            TEAMS,
            SETTINGS,
            {r.name: "pw" for r in workload_refs(chosen, TEAMS)},
        )
    )
    assert {"allow-egress-internet", "allow-intra-namespace"} <= names
    with pytest.raises(SpecValidationError):
        apply_network_overrides(parse_spec(VALID_VM_SPEC), {"internet": True})


# ── the live view of a VM deployment ───────────────────────────────────────────


def _live_vm(ready_after_polls=0):
    from app.k8s.fake import FakeKube
    from app.services import k8s_status

    kube = FakeKube(ready_after_polls=ready_after_polls)
    kube.namespace_labels["vm-poc"] = {}  # the image namespace exists before any deployment
    for obj in _vm_objects():
        kube.apply(obj)
    k8s_status.set_kube_factory(lambda: kube)
    return k8s_status


def test_the_live_view_shows_one_row_per_vm_with_the_link_to_its_proxy(monkeypatch):
    from app.services import k8s_status

    monkeypatch.setattr(k8s_status, "app_domain", lambda: "apps.example.org")
    status = _live_vm()
    try:
        status.workload_views(DEP.id, DEP.namespace)  # first poll
        (row,) = status.workload_views(DEP.id, DEP.namespace)
    finally:
        status.set_kube_factory(None)

    assert row["workload"].startswith("team-a")
    assert (row["phase"], row["ready"]) == ("Running", True)
    assert row["team"] == "team-a" and row["user_id"] == "uid-1"
    assert row["url"] == f"https://{row['workload']}-novnc-{DEP.short_id}.apps.example.org"


def test_a_booting_vm_is_not_ready_yet():
    status = _live_vm(ready_after_polls=10**6)
    try:
        (row,) = status.workload_views(DEP.id, DEP.namespace)
    finally:
        status.set_kube_factory(None)
    assert row["ready"] is False and row["phase"] != "Running"
