"""Tests for app contract v2 (appstore.yaml) parsing and validation (shared app/k8s/spec.py)."""

import copy

import pytest

from app.k8s.spec import PlatformLimits, SpecValidationError, parse_spec

pytestmark = pytest.mark.unit

DIGEST = "sha256:" + "a" * 64

VALID = {
    "apiVersion": "appstore/v2",
    "name": "online-ide",
    "runtime": "kubernetes",
    "scope": "user",
    "workload": {
        "containers": [
            {
                "name": "ide",
                "image": f"ghcr.io/dhbw-appstore-t3/apps/code-server@{DIGEST}",
                "expose": {"port": 8080, "path": "/"},
                "resources": {"cpu": "500m", "memory": "1Gi"},
                "env": [{"name": "PASSWORD", "from": "generated-password"}, {"name": "TEAM", "from": "team-name"}],
                "writablePaths": ["/tmp"],
            }
        ],
        "storage": {"size": "5Gi", "mountPath": "/home/coder"},
    },
    "egress": "internet",
    "variables": [
        {
            "name": "assignment_zip",
            "type": "file",
            "scope": "team",
            "ext": ["zip"],
            "maxSize": "512Ki",
            "mountPath": "/data/assignment.zip",
        },
        {"name": "cpu_class", "type": "enum", "values": ["small", "medium"], "default": "small"},
    ],
    "access": [
        {"type": "url", "template": "https://{workload}-{deployment}.apps.{zone}"},
        {"type": "password", "from": "generated-password", "username": "{user}"},
    ],
}


def spec_with(mutate):
    data = copy.deepcopy(VALID)
    mutate(data)
    return data


def errors_of(data, limits=None):
    with pytest.raises(SpecValidationError) as exc:
        parse_spec(data, limits)
    return {e["loc"]: e["msg"] for e in exc.value.errors}


def test_valid_spec_parses_from_yaml_and_dict():
    import yaml

    assert parse_spec(VALID).name == "online-ide"
    assert parse_spec(yaml.safe_dump(VALID)).workload.containers[0].expose.port == 8080


def test_tag_instead_of_digest_is_rejected_with_field_path():
    data = spec_with(lambda d: d["workload"]["containers"][0].update(image="ghcr.io/dhbw-appstore-t3/apps/x:latest"))
    errs = errors_of(data)
    assert "workload.containers[0]" in errs or "workload.containers[0].image" in errs
    assert any("digest" in m for m in errs.values())


def test_foreign_registry_is_rejected():
    data = spec_with(lambda d: d["workload"]["containers"][0].update(image=f"docker.io/evil/img@{DIGEST}"))
    assert "workload.containers[0].image" in errors_of(data)


def test_registry_prefix_must_match_allowlist_exactly():
    data = spec_with(
        lambda d: d["workload"]["containers"][0].update(image=f"ghcr.io/dhbw-appstore-t3-evil/img@{DIGEST}")
    )
    assert "workload.containers[0].image" in errors_of(data)


def test_resources_above_platform_limits_are_rejected():
    def mutate(d):
        d["workload"]["containers"][0]["resources"] = {"cpu": "8", "memory": "64Gi"}
        d["workload"]["storage"]["size"] = "500Gi"

    errs = errors_of(spec_with(mutate))
    assert {
        "workload.containers[0].resources.cpu",
        "workload.containers[0].resources.memory",
        "workload.storage.size",
    } <= set(errs)


def test_custom_limits_apply():
    limits = PlatformLimits(max_cpu_millicores=100)
    assert "workload.containers[0].resources.cpu" in errors_of(VALID, limits)


def test_unknown_fields_are_rejected():
    data = spec_with(lambda d: d["workload"]["containers"][0].update(securityContext={"privileged": True}))
    assert "workload.containers[0].securityContext" in errors_of(data)
    assert "hostNetwork" in errors_of(spec_with(lambda d: d.update(hostNetwork=True)))


def test_multiple_expose_rejected():
    def mutate(d):
        second = copy.deepcopy(d["workload"]["containers"][0])
        second["name"] = "sidecar"
        d["workload"]["containers"].append(second)

    assert any("exactly one" in m for m in errors_of(spec_with(mutate)).values())


def test_no_expose_rejected():
    data = spec_with(lambda d: d["workload"]["containers"][0].pop("expose"))
    assert any("exactly one" in m for m in errors_of(data).values())


def test_more_than_three_containers_rejected():
    def mutate(d):
        c = d["workload"]["containers"][0]
        d["workload"]["containers"] = [c] + [
            {k: v for k, v in c.items() if k != "expose"} | {"name": f"s{i}"} for i in range(3)
        ]

    assert "workload.containers" in errors_of(spec_with(mutate))


def test_privileged_port_rejected():
    data = spec_with(lambda d: d["workload"]["containers"][0]["expose"].update(port=80))
    assert "workload.containers[0].expose.port" in errors_of(data)


def test_kubernetes_requires_workload_and_vm_forbids_it():
    assert any("requires 'workload'" in m for m in errors_of(spec_with(lambda d: d.pop("workload"))).values())
    assert any("not allowed" in m for m in errors_of(spec_with(lambda d: d.update(runtime="openstack-vm"))).values())


def test_openstack_vm_spec_without_workload_is_valid():
    assert parse_spec({"apiVersion": "appstore/v2", "name": "win", "runtime": "openstack-vm"}).workload is None


def test_large_file_variable_rejected():
    data = spec_with(lambda d: d["variables"][0].update(maxSize="5Mi"))
    assert "variables[0]" in errors_of(data)


def test_access_unknown_placeholder_rejected():
    data = spec_with(lambda d: d["access"][0].update(template="https://{evil}.example"))
    assert any("unknown placeholder" in m for m in errors_of(data).values())


def test_env_needs_exactly_one_source():
    data = spec_with(lambda d: d["workload"]["containers"][0]["env"].append({"name": "X"}))
    assert any("exactly one" in m for m in errors_of(data).values())


@pytest.mark.parametrize("text", ["", "- a\n- b", "key: [unclosed"])
def test_non_mapping_or_broken_yaml_rejected(text):
    with pytest.raises(SpecValidationError):
        parse_spec(text)


def test_access_urls_must_be_https():
    data = spec_with(lambda d: d["access"][0].update(template="javascript://{workload}-{deployment}.{domain}/%0a"))
    assert any("https" in m for m in errors_of(data).values())


def test_registry_prefixes_always_end_in_a_slash():
    from app.k8s.spec import limits_from, registry_prefixes

    assert registry_prefixes(" ghcr.io/dhbw-appstore-t3 , ghcr.io/other/,") == ("ghcr.io/dhbw-appstore-t3/", "ghcr.io/other/")
    limits = limits_from("ghcr.io/dhbw-appstore-t3", "2", "4Gi", "20Gi")
    evil = spec_with(
        lambda d: d["workload"]["containers"][0].update(image=f"ghcr.io/dhbw-appstore-t3-evil/img@{DIGEST}")
    )
    assert "workload.containers[0].image" in errors_of(evil, limits)
    assert (limits.max_cpu_millicores, limits.max_memory_mib, limits.max_storage_mib) == (2000, 4096, 20480)


# The ingress controller builds its routing rule from the ingress path
# (Traefik: PathPrefix(`<path>`)); a backtick could append a rule matching
# any host. Mount paths go into the pod spec.
@pytest.mark.parametrize(
    "path",
    [
        "/x`) || HostRegexp(`.+`) || PathPrefix(`/",
        "/a b",
        "/a/../b",
        "/a/./b",
        "//a",
        "/a//b",
        "/a%2fb",
        "relative",
        "/" + "a" * 300,
    ],
)
def test_ingress_path_must_be_plain_segments(path):
    data = spec_with(lambda d: d["workload"]["containers"][0]["expose"].update(path=path))
    assert "workload.containers[0].expose" in errors_of(data)


@pytest.mark.parametrize("path", ["/", "/ide", "/ide/", "/a-b/c_d/e.f"])
def test_plain_ingress_paths_are_accepted(path):
    data = spec_with(lambda d: d["workload"]["containers"][0]["expose"].update(path=path))
    assert parse_spec(data).workload.containers[0].expose.path == path


@pytest.mark.parametrize("path", ["/", "/tmp/", "/tmp/../etc", "/a`b", "tmp"])
def test_mount_paths_must_be_plain_and_not_root(path):
    writable = spec_with(lambda d: d["workload"]["containers"][0].update(writablePaths=[path]))
    assert "workload.containers[0]" in errors_of(writable)
    storage = spec_with(lambda d: d["workload"]["storage"].update(mountPath=path))
    assert "workload.storage" in errors_of(storage)
    file_var = spec_with(lambda d: d["variables"][0].update(mountPath=path))
    assert "variables[0]" in errors_of(file_var)


def test_mounts_at_the_same_path_are_rejected():
    twice = spec_with(lambda d: d["workload"]["containers"][0].update(writablePaths=["/tmp", "/tmp"]))
    assert any("unique" in m for m in errors_of(twice).values())
    over_storage = spec_with(lambda d: d["workload"]["containers"][0].update(writablePaths=["/home/coder"]))
    assert any("storage.mountPath" in m for m in errors_of(over_storage).values())
    file_over_tmp = spec_with(lambda d: d["variables"][0].update(mountPath="/tmp"))
    assert any("mountPath" in m for m in errors_of(file_over_tmp).values())

    def two_files(d):
        d["variables"].append({**d["variables"][0], "name": "second_zip"})

    assert any("distinct" in m for m in errors_of(spec_with(two_files)).values())


# ----------------------------------------------------------------
# resourceBounds / apply_resource_overrides (opt-in cpu/memory choice)
# ----------------------------------------------------------------
def _tunable(**bounds):
    spec = copy.deepcopy(VALID)
    c = spec["workload"]["containers"][0]
    c["resources"] = {"cpu": "2", "memory": "4Gi", "requests": {"cpu": "500m", "memory": "1Gi"}}
    c["resourceBounds"] = bounds or {"cpu": {"min": "1", "max": "4"}, "memory": {"min": "2Gi", "max": "8Gi"}}
    return spec


BIG = PlatformLimits(max_cpu_millicores=4000, max_memory_mib=8192)


def test_the_default_must_lie_within_the_bounds():
    spec = _tunable(cpu={"min": "3", "max": "4"}, memory={"min": "2Gi", "max": "8Gi"})
    with pytest.raises(SpecValidationError, match="resources.cpu"):
        parse_spec(spec, BIG)


def test_bounds_may_not_exceed_the_platform_limits():
    with pytest.raises(SpecValidationError) as exc:
        parse_spec(_tunable(), PlatformLimits(max_cpu_millicores=2000, max_memory_mib=4096))
    assert {e["loc"] for e in exc.value.errors} == {
        "workload.containers[0].resourceBounds.cpu.max",
        "workload.containers[0].resourceBounds.memory.max",
    }


def test_a_choice_replaces_the_limits_and_scales_the_requests():
    from app.k8s.spec import apply_resource_overrides

    spec = parse_spec(_tunable(), BIG)
    out = apply_resource_overrides(spec, {"ide": {"cpu": "4", "memory": "8Gi"}}, BIG)
    r = out.workload.containers[0].resources
    assert (r.cpu, r.memory) == ("4", "8Gi")
    assert (r.requests.cpu, r.requests.memory) == ("1", "2Gi")
    # the reviewed spec is untouched
    assert spec.workload.containers[0].resources.cpu == "2"


@pytest.mark.parametrize(
    ("choice", "loc"),
    [
        ({"ide": {"cpu": "8"}}, "resources.ide.cpu"),
        ({"ide": {"memory": "512Mi"}}, "resources.ide.memory"),
        ({"ide": {"cpu": "lots"}}, "resources.ide"),
        ({"ide": {"disk": "1Gi"}}, "resources.ide"),
        ({"other": {"cpu": "2"}}, "resources.other"),
    ],
)
def test_a_choice_outside_the_bounds_is_refused(choice, loc):
    from app.k8s.spec import apply_resource_overrides

    spec = parse_spec(_tunable(), BIG)
    with pytest.raises(SpecValidationError) as exc:
        apply_resource_overrides(spec, choice, BIG)
    assert exc.value.errors[0]["loc"] == loc


def test_a_container_without_bounds_cannot_be_changed():
    from app.k8s.spec import apply_resource_overrides

    spec = parse_spec(VALID)
    with pytest.raises(SpecValidationError, match="cannot be changed"):
        apply_resource_overrides(spec, {"ide": {"cpu": "1"}})


def test_resource_choices_list_default_and_range():
    from app.k8s.spec import resource_choices

    spec = parse_spec(_tunable(), BIG)
    assert resource_choices(spec, PlatformLimits(max_cpu_millicores=3000, max_memory_mib=8192)) == [
        {
            "container": "ide",
            "cpu": {"default": "2", "min": "1", "max": "3"},
            "memory": {"default": "4Gi", "min": "2Gi", "max": "8Gi"},
        }
    ]
    assert resource_choices(parse_spec(VALID)) == []


# ----------------------------------------------------------------
# podNetwork / userChoices / apply_network_overrides (opt-in network choice)
# ----------------------------------------------------------------
def _networked(**choices):
    return {**copy.deepcopy(VALID), "userChoices": choices}


def test_network_defaults_are_off_and_not_changeable():
    spec = parse_spec(VALID)
    assert spec.pod_network == "none" and spec.user_choices is None


def test_unknown_user_choice_and_pod_network_value_are_rejected():
    assert "userChoices.disk" in errors_of(_networked(disk=True))
    assert "podNetwork" in errors_of({**VALID, "podNetwork": "cluster"})


def test_network_override_sets_both_values_and_keeps_the_original():
    from app.k8s.spec import apply_network_overrides

    spec = parse_spec(_networked(internet=True, podNetwork=True))
    spec.egress = "none"
    out = apply_network_overrides(spec, {"internet": True, "podNetwork": True})
    assert out.egress == "internet" and out.pod_network == "namespace"
    assert spec.egress == "none" and spec.pod_network == "none"


def test_network_override_can_switch_the_author_default_off():
    from app.k8s.spec import apply_network_overrides

    spec = parse_spec(_networked(internet=True))
    assert spec.egress == "internet"
    assert apply_network_overrides(spec, {"internet": False}).egress == "none"


def test_network_override_leaves_unreleased_keys_alone_when_not_sent():
    from app.k8s.spec import apply_network_overrides

    spec = parse_spec({**_networked(internet=True), "podNetwork": "namespace"})
    out = apply_network_overrides(spec, {"internet": True})
    assert out.pod_network == "namespace"


@pytest.mark.parametrize(
    ("choices", "choice", "loc", "msg"),
    [
        ({}, {"internet": True}, "network.internet", "cannot be changed"),
        ({"internet": True}, {"podNetwork": True}, "network.podNetwork", "cannot be changed"),
        ({"internet": True}, {"internet": "yes"}, "network.internet", "true or false"),
        ({"internet": True}, {"internet": 1}, "network.internet", "true or false"),
        ({"internet": True}, {"dns": True}, "network.dns", "unknown setting"),
    ],
)
def test_network_override_errors_carry_the_field_path(choices, choice, loc, msg):
    from app.k8s.spec import apply_network_overrides

    with pytest.raises(SpecValidationError) as exc:
        apply_network_overrides(parse_spec(_networked(**choices)), choice)
    assert [(e["loc"], msg in e["msg"]) for e in exc.value.errors] == [(loc, True)]


def test_network_override_without_user_choices_is_refused_and_empty_choice_is_a_noop():
    from app.k8s.spec import apply_network_overrides

    spec = parse_spec(VALID)
    assert apply_network_overrides(spec, None) is spec and apply_network_overrides(spec, {}) is spec
    with pytest.raises(SpecValidationError):
        apply_network_overrides(spec, {"internet": True})
    with pytest.raises(SpecValidationError, match="network"):
        apply_network_overrides(spec, ["internet"])


def test_network_choices_list_only_released_keys_with_the_author_default():
    from app.k8s.spec import network_choices

    assert network_choices(parse_spec(VALID)) == {}
    assert network_choices(parse_spec(_networked(podNetwork=True))) == {"podNetwork": {"default": False}}
    both = parse_spec({**_networked(internet=True, podNetwork=True), "egress": "internet", "podNetwork": "namespace"})
    assert network_choices(both) == {"internet": {"default": True}, "podNetwork": {"default": True}}


def test_user_choices_must_be_real_booleans():
    assert "userChoices.internet" in errors_of(_networked(internet=1))
    assert "userChoices.podNetwork" in errors_of(_networked(podNetwork="yes"))


@pytest.mark.parametrize("choice", [[], 0, "", False])
def test_network_override_refuses_a_falsy_non_mapping(choice):
    from app.k8s.spec import apply_network_overrides

    with pytest.raises(SpecValidationError) as exc:
        apply_network_overrides(parse_spec(_networked(internet=True)), choice)
    assert exc.value.errors[0]["loc"] == "network"
