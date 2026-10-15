"""Tests for the pure Kubernetes renderer (shared app/k8s/render.py): golden file + invariants."""

import copy
import json
import os
import uuid
from pathlib import Path

import pytest

from app.k8s.render import (
    DEPLOYMENT_LABEL,
    USER_LABEL,
    DeploymentCtx,
    Member,
    RenderSettings,
    Team,
    render,
    render_access,
    workload_refs,
)
from app.k8s.spec import parse_spec
from tests.unit.test_k8s_spec import VALID

pytestmark = pytest.mark.unit

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "online_ide_3_teams.json"
OWNER = "11111111-2222-4333-8444-555555555555"
DEP = DeploymentCtx(id="123e4567-e89b-12d3-a456-426614174000", owner=OWNER)
SETTINGS = RenderSettings(
    zone="t3-k8s.example.org",
    cluster_issuer="letsencrypt",
    storage_class="local-path",
    node_selector={"appstore.dhbw/workload": "student"},
    tolerations=({"key": "appstore.dhbw/workload", "operator": "Equal", "value": "student", "effect": "NoSchedule"},),
)


def member(name: str) -> Member:
    return Member(user_id=str(uuid.uuid5(uuid.NAMESPACE_DNS, name)), name=name)


TEAMS = [
    Team("team-a", (member("alice"), member("bob"))),
    Team("team-b", (member("carol"), member("dave"))),
    Team("team-c", (member("erin"), member("frank"))),
]


def build(spec_data=VALID, teams=TEAMS, settings=SETTINGS):
    spec = parse_spec(spec_data)
    refs = workload_refs(spec, teams)
    passwords = {r.name: f"pw-{r.name}" for r in refs}
    return spec, render(spec, DEP, teams, settings, passwords), passwords


def of_kind(objs, kind):
    return [o for o in objs if o["kind"] == kind]


def test_matches_golden_file():
    _, objs, _ = build()
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(json.dumps(objs, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert objs == json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_render_is_deterministic():
    assert build()[1] == build()[1]


def test_six_workloads_for_scope_user_three_for_scope_team():
    assert len(of_kind(build()[1], "StatefulSet")) == 6
    team_scope = {**VALID, "scope": "team"}
    assert len(of_kind(build(team_scope)[1], "StatefulSet")) == 3


def test_every_object_carries_deployment_label_and_namespace():
    _, objs, _ = build()
    for o in objs:
        assert o["metadata"]["labels"][DEPLOYMENT_LABEL] == DEP.id
        if o["kind"] != "Namespace":
            assert o["metadata"]["namespace"] == "dep-123e4567"


def test_namespace_enforces_restricted_pod_security():
    ns = of_kind(build()[1], "Namespace")[0]
    assert ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"


def test_pods_are_hardened_and_cannot_be_weakened_by_spec():
    for sts in of_kind(build()[1], "StatefulSet"):
        pod = sts["spec"]["template"]["spec"]
        assert pod["automountServiceAccountToken"] is False
        assert pod["securityContext"]["runAsNonRoot"] is True
        assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
        for c in pod["containers"]:
            sc = c["securityContext"]
            assert sc["allowPrivilegeEscalation"] is False
            assert sc["capabilities"] == {"drop": ["ALL"]}
            assert sc["readOnlyRootFilesystem"] is True
        assert "hostNetwork" not in pod and "hostPID" not in pod
        assert all("hostPath" not in v for v in pod["volumes"])
        # Scratch space is bounded, so one pod cannot fill the node's disk.
        assert all(v["emptyDir"].get("sizeLimit") for v in pod["volumes"] if "emptyDir" in v)


def test_network_policy_denies_by_default_and_excludes_internal_ranges_for_both_families():
    pols = {p["metadata"]["name"]: p["spec"] for p in of_kind(build()[1], "NetworkPolicy")}
    assert pols["default-deny"]["policyTypes"] == ["Ingress", "Egress"]
    assert "ingress" not in pols["default-deny"] and "egress" not in pols["default-deny"]
    blocks = {
        b["to"][0]["ipBlock"]["cidr"]: b["to"][0]["ipBlock"]["except"] for b in pols["allow-egress-internet"]["egress"]
    }
    assert "169.254.0.0/16" in blocks["0.0.0.0/0"] and "10.0.0.0/8" in blocks["0.0.0.0/0"]
    assert "fc00::/7" in blocks["::/0"] and "fe80::/10" in blocks["::/0"]


def test_egress_none_has_no_internet_policy():
    names = {p["metadata"]["name"] for p in of_kind(build({**VALID, "egress": "none"})[1], "NetworkPolicy")}
    assert names == {"default-deny", "allow-ingress-controller", "allow-dns"}


def test_extra_egress_except_split_by_family():
    s = RenderSettings(zone="z", extra_egress_except=("100.64.0.0/10", "2001:db8::/32"))
    pol = next(
        p for p in of_kind(build(settings=s)[1], "NetworkPolicy") if p["metadata"]["name"] == "allow-egress-internet"
    )
    v4, v6 = (r["to"][0]["ipBlock"]["except"] for r in pol["spec"]["egress"])
    assert "100.64.0.0/10" in v4 and "100.64.0.0/10" not in v6
    assert "2001:db8::/32" in v6


def test_quota_sums_all_workloads_and_blocks_nodeports():
    hard = of_kind(build()[1], "ResourceQuota")[0]["spec"]["hard"]
    assert hard["pods"] == "6" and hard["requests.cpu"] == "3000m" and hard["requests.memory"] == "6144Mi"
    assert hard["requests.storage"] == "30720Mi" and hard["services.nodeports"] == "0"


def test_ingress_hosts_unique_and_follow_pattern():
    hosts = [i["spec"]["rules"][0]["host"] for i in of_kind(build()[1], "Ingress")]
    assert len(set(hosts)) == 6
    assert all(h.endswith("-123e4567.apps.t3-k8s.example.org") for h in hosts)
    assert all(len(h.split(".")[0]) <= 63 for h in hosts)


def test_password_secret_wired_via_secret_key_ref_not_inline():
    objs = build()[1]
    sts = of_kind(objs, "StatefulSet")[0]
    env = {e["name"]: e for e in sts["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "value" not in env["PASSWORD"]
    assert env["PASSWORD"]["valueFrom"]["secretKeyRef"]["key"] == "password"
    assert env["TEAM"]["value"] == "team-a"


def test_workloads_carry_the_members_user_id():
    sts = {s["metadata"]["name"]: s for s in of_kind(build()[1], "StatefulSet")}
    assert sts["team-a-alice"]["metadata"]["labels"][USER_LABEL] == member("alice").user_id


def test_access_entries_are_per_person_with_distinct_credentials():
    spec, _, pw = build()
    entries = render_access(spec, DEP, TEAMS, SETTINGS, pw)
    alice, bob = entries[0], entries[1]
    assert (alice.user, bob.user) == (member("alice"), member("bob"))
    assert alice.password != bob.password and alice.url != bob.url
    assert alice.username == "alice"
    assert alice.url == "https://team-a-alice-123e4567.apps.t3-k8s.example.org"


def test_members_whose_names_normalise_alike_get_separate_workloads():
    """``anna.b`` and ``anna-b`` must never share a workload, a password or a URL."""
    spec = parse_spec(VALID)
    a, b = Member("aaaaaaaa-0000-4000-8000-000000000001", "anna.b"), Member("bbbbbbbb-0000-4000-8000-000000000002", "anna-b")
    teams = [Team("team-a", (a, b, member("carl")))]
    refs = {r.user.user_id: r.name for r in workload_refs(spec, teams)}
    assert refs[a.user_id] == "team-a-anna-b-aaaaaaaa"
    assert refs[b.user_id] == "team-a-anna-b-bbbbbbbb"
    assert refs[member("carl").user_id] == "team-a-carl"
    pw = {name: f"pw-{name}" for name in refs.values()}
    entries = {e.user.user_id: e for e in render_access(spec, DEP, teams, SETTINGS, pw)}
    assert entries[a.user_id].password != entries[b.user_id].password
    assert entries[a.user_id].url != entries[b.user_id].url


def test_long_names_are_shortened_to_valid_dns_labels():
    long_team = Team("T" * 80, (member("u" * 80),))
    ref = workload_refs(parse_spec(VALID), [long_team])[0]
    assert len(ref.name) <= 40


def test_colliding_team_names_rejected():
    with pytest.raises(ValueError, match="collide"):
        workload_refs(parse_spec({**VALID, "scope": "team"}), [Team("A b"), Team("a-b")])


def test_missing_password_and_vm_runtime_rejected():
    spec = parse_spec(VALID)
    with pytest.raises(ValueError, match="missing passwords"):
        render(spec, DEP, TEAMS, SETTINGS, {})
    vm = parse_spec({"apiVersion": "appstore/v2", "name": "win", "runtime": "openstack-vm"})
    with pytest.raises(ValueError, match="kubernetes"):
        render(vm, DEP, TEAMS, SETTINGS, {})


def test_file_variables_become_a_secret_mounted_as_a_single_file():
    spec = parse_spec(VALID)
    refs = workload_refs(spec, TEAMS)
    pw = {r.name: "x" for r in refs}
    objs = render(spec, DEP, TEAMS, SETTINGS, pw, files={"assignment_zip": {"team-a": b"PK\x03"}})
    secrets = [o for o in of_kind(objs, "Secret") if o["metadata"]["name"].endswith("-files")]
    assert {s["metadata"]["labels"]["appstore.dhbw/workload"] for s in secrets} == {"team-a-alice", "team-a-bob"}
    sts = next(o for o in of_kind(objs, "StatefulSet") if o["metadata"]["name"] == "team-a-alice")
    mount = next(m for m in sts["spec"]["template"]["spec"]["containers"][0]["volumeMounts"] if m["name"] == "files")
    assert mount["mountPath"] == "/data/assignment.zip" and mount["readOnly"] is True
    others = [o for o in of_kind(objs, "StatefulSet") if o["metadata"]["name"].startswith("team-b")]
    assert all(
        "files" not in [m["name"] for m in s["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]] for s in others
    )


def test_per_person_slots_match_the_exact_user_name_only():
    spec = parse_spec(VALID)
    teams = [Team("Team 1", (Member("u-1", "anna.b"), Member("u-2", "anna-b")))]
    pw = {r.name: "x" for r in workload_refs(spec, teams)}
    objs = render(spec, DEP, teams, SETTINGS, pw, files={"assignment_zip": {"Team 1-anna.b": b"PK"}})
    owners = {o["metadata"]["labels"]["appstore.dhbw/workload"] for o in of_kind(objs, "Secret") if o["metadata"]["name"].endswith("-files")}
    assert len(owners) == 1  # only anna.b's workload, never anna-b's


def test_enum_variables_reach_containers_as_env():
    spec = parse_spec(VALID)
    pw = {r.name: "x" for r in workload_refs(spec, TEAMS)}
    objs = render(spec, DEP, TEAMS, SETTINGS, pw, variable_values={"cpu_class": {"team-a": "medium"}})
    envs = {
        s["metadata"]["name"]: {e["name"]: e.get("value") for e in s["spec"]["template"]["spec"]["containers"][0]["env"]}
        for s in of_kind(objs, "StatefulSet")
    }
    assert envs["team-a-alice"]["APPSTORE_VAR_CPU_CLASS"] == "medium"
    assert envs["team-b-carol"]["APPSTORE_VAR_CPU_CLASS"] == "small"  # the declared default


def test_ingress_always_has_tls_and_uses_the_default_certificate_without_a_secret():
    s = RenderSettings(zone="z.example.org", app_domain_override="z.example.org")
    ing = of_kind(build(settings=s)[1], "Ingress")[0]
    assert ing["spec"]["tls"] == [{"hosts": ["team-a-alice-123e4567.z.example.org"]}]


@pytest.mark.parametrize(
    "template",
    [
        "https://{workload}.elsewhere.example",
        "https://{workload}-{deployment}.{domain}.evil.example",
        "https://evil.example/{workload}-{deployment}.{domain}",
    ],
)
def test_access_url_must_match_the_ingress_host(template):
    spec = parse_spec({**VALID, "access": [{"type": "url", "template": template}]})
    pw = {r.name: "x" for r in workload_refs(spec, TEAMS)}
    with pytest.raises(ValueError, match="does not match"):
        render_access(spec, DEP, TEAMS, SETTINGS, pw)


def test_domain_placeholder_follows_the_app_domain():
    spec = parse_spec({**VALID, "access": [{"type": "url", "template": "https://{workload}-{deployment}.{domain}"}]})
    s = RenderSettings(zone="z.example.org", app_domain_override="z.example.org")
    pw = {r.name: "x" for r in workload_refs(spec, TEAMS)}
    assert render_access(spec, DEP, TEAMS, s, pw)[0].url == "https://team-a-alice-123e4567.z.example.org"


def test_requests_default_to_limits_and_can_be_lower():
    spec = {**VALID}
    spec["workload"] = copy.deepcopy(VALID["workload"])
    spec["workload"]["containers"][0]["resources"] = {
        "cpu": "1", "memory": "1Gi", "requests": {"cpu": "100m", "memory": "256Mi"},
    }
    objs = build(spec)[1]
    res = of_kind(objs, "StatefulSet")[0]["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert res == {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"cpu": "1", "memory": "1Gi"}}
    hard = of_kind(objs, "ResourceQuota")[0]["spec"]["hard"]
    assert hard["requests.cpu"] == "600m" and hard["limits.cpu"] == "6000m" and hard["requests.memory"] == "1536Mi"


def test_requests_above_limits_are_rejected():
    from app.k8s.spec import SpecValidationError

    bad = copy.deepcopy(VALID)
    bad["workload"]["containers"][0]["resources"] = {"cpu": "500m", "memory": "1Gi", "requests": {"cpu": "1", "memory": "1Gi"}}
    with pytest.raises(SpecValidationError, match="requests.cpu"):
        parse_spec(bad)
