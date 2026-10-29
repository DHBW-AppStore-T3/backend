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


def test_kubernetes_requires_workload():
    assert any("requires 'workload'" in m for m in errors_of(spec_with(lambda d: d.pop("workload"))).values())


def test_unknown_runtime_is_rejected():
    assert "runtime" in errors_of(spec_with(lambda d: d.update(runtime="openstack-vm")))


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
    spec = parse_spec({k: v for k, v in VALID.items() if k != "egress"})
    assert spec.egress == "none" and spec.pod_network == "none"
    assert not spec.network.internet.allowed and not spec.network.pod_network.allowed


def test_unknown_user_choice_and_pod_network_value_are_rejected():
    assert "userChoices.disk" in errors_of(_networked(disk=True))
    assert "podNetwork" in errors_of({**VALID, "podNetwork": "cluster"})


def test_network_override_sets_both_values_and_keeps_the_original():
    from app.k8s.spec import apply_network_overrides

    spec = parse_spec({**_networked(internet=True, podNetwork=True), "egress": "none"})
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


# ----------------------------------------------------------------
# network: {internet: {allowed, default}, podNetwork: {allowed, default}}
# ----------------------------------------------------------------
def _with_network(network):
    data = {k: v for k, v in copy.deepcopy(VALID).items() if k != "egress"}
    return {**data, "network": network}


@pytest.mark.parametrize(
    ("setting", "fixed_on", "offered"),
    [
        ({}, False, False),  # never available
        ({"allowed": False, "default": True}, True, False),  # always on, no choice
        ({"allowed": True, "default": False}, False, True),  # a choice, starting off
        ({"allowed": True, "default": True}, True, True),  # a choice, starting on
    ],
)
def test_network_setting_states_decide_the_value_and_whether_it_is_offered(setting, fixed_on, offered):
    from app.k8s.spec import network_choices

    spec = parse_spec(_with_network({"internet": setting}))
    assert (spec.egress == "internet") is fixed_on
    assert ("internet" in network_choices(spec)) is offered
    assert spec.pod_network == "none" and "podNetwork" not in network_choices(spec)


def test_network_block_rejects_unknown_keys_and_non_booleans():
    assert "network.dns" in errors_of(_with_network({"dns": {"allowed": True}}))
    assert "network.internet.allowed" in errors_of(_with_network({"internet": {"allowed": "yes"}}))
    assert "network.podNetwork.default" in errors_of(_with_network({"podNetwork": {"default": 1}}))
    assert "network.internet.ports" in errors_of(_with_network({"internet": {"ports": [25]}}))


def test_network_block_cannot_be_mixed_with_the_older_keys():
    for legacy in ({"egress": "internet"}, {"podNetwork": "namespace"}, {"userChoices": {"internet": True}}):
        data = {**_with_network({"internet": {"allowed": True}}), **legacy}
        assert "cannot be combined" in errors_of(data)["<root>"]


@pytest.mark.parametrize(
    ("legacy", "internet", "pod_network"),
    [
        ({}, {"allowed": False, "default": False}, {"allowed": False, "default": False}),
        ({"egress": "internet"}, {"allowed": False, "default": True}, {"allowed": False, "default": False}),
        (
            {"egress": "internet", "userChoices": {"internet": True}},
            {"allowed": True, "default": True},
            {"allowed": False, "default": False},
        ),
        (
            {"podNetwork": "namespace", "userChoices": {"podNetwork": True}},
            {"allowed": False, "default": False},
            {"allowed": True, "default": True},
        ),
    ],
)
def test_older_network_keys_read_as_the_same_network_block(legacy, internet, pod_network):
    data = {k: v for k, v in copy.deepcopy(VALID).items() if k != "egress"} | legacy
    network = parse_spec(data).network.model_dump(by_alias=True)
    assert network == {"internet": internet, "podNetwork": pod_network}


def test_the_job_payload_carries_the_network_block_and_not_the_older_keys():
    spec = parse_spec(VALID)  # says egress: internet
    dumped = spec.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert dumped["network"]["internet"] == {"allowed": False, "default": True}
    assert not {"egress", "podNetwork", "userChoices"} & set(dumped)
    assert parse_spec(dumped).egress == "internet"  # and the worker reads it back unchanged


def test_a_person_s_choice_makes_the_spec_fixed_and_keeps_the_original():
    from app.k8s.spec import apply_network_overrides

    spec = parse_spec(_with_network({"internet": {"allowed": True, "default": True}, "podNetwork": {"allowed": True}}))
    out = apply_network_overrides(spec, {"internet": False, "podNetwork": True})
    assert out.egress == "none" and out.pod_network == "namespace"
    assert not out.network.internet.allowed and not out.network.pod_network.allowed  # fixed: no second choice
    assert spec.egress == "internet" and spec.network.internet.allowed  # the reviewed spec is untouched
    # fixed values survive the round trip to the worker
    assert parse_spec(out.model_dump(mode="json", by_alias=True, exclude_none=True)).egress == "none"


def test_a_setting_that_is_always_on_cannot_be_switched_off():
    from app.k8s.spec import apply_network_overrides

    spec = parse_spec(_with_network({"internet": {"allowed": False, "default": True}}))
    with pytest.raises(SpecValidationError) as exc:
        apply_network_overrides(spec, {"internet": False})
    assert [(e["loc"], e["msg"]) for e in exc.value.errors] == [("network.internet", "this setting cannot be changed")]

# The rules that reject a malformed appstore.yaml. The spec comes from an app author (a teacher account that
# may be compromised) and is validated again by the worker before anything is created; each rule below is
# one a regression could drop silently.
# ----------------------------------------------------------------


def _first_container(data):
    return data["workload"]["containers"][0]


def _second_container(data):
    second = copy.deepcopy(_first_container(data))
    del second["expose"]  # only one container may be exposed
    return second


MALFORMED = {
    "memory is no quantity": (
        lambda d: _first_container(d)["resources"].update(memory="lots"),
        "invalid memory quantity",
    ),
    "scheduler request above the limit": (
        lambda d: _first_container(d)["resources"].update(
            requests={"cpu": "100m", "memory": "2Gi"}
        ),
        "requests.memory must not exceed memory",
    ),
    "cpu bounds the wrong way round": (
        lambda d: _first_container(d).update(
            resourceBounds={
                "cpu": {"min": "2", "max": "1"},
                "memory": {"min": "512Mi", "max": "2Gi"},
            }
        ),
        "cpu.min must not exceed cpu.max",
    ),
    "memory bounds the wrong way round": (
        lambda d: _first_container(d).update(
            resourceBounds={
                "cpu": {"min": "250m", "max": "1"},
                "memory": {"min": "2Gi", "max": "1Gi"},
            }
        ),
        "memory.min must not exceed memory.max",
    ),
    "default memory outside its bounds": (
        lambda d: _first_container(d).update(
            resourceBounds={
                "cpu": {"min": "250m", "max": "1"},
                "memory": {"min": "2Gi", "max": "4Gi"},
            }
        ),
        "resources.memory (the default) must lie within resourceBounds.memory",
    ),
    "environment variable name that is no shell name": (
        lambda d: _first_container(d)["env"].append({"name": "1-bad", "value": "x"}),
        "invalid environment variable name",
    ),
    "container name in capitals": (
        lambda d: _first_container(d).update(name="IDE"),
        "invalid container name",
    ),
    "storage size that is no size": (
        lambda d: d["workload"]["storage"].update(size="5GB"),
        "invalid storage size",
    ),
    "two containers with one name": (
        lambda d: d["workload"]["containers"].append(_second_container(d)),
        "container names must be unique",
    ),
    "app name with a space": (
        lambda d: d.update(name="Online IDE"),
        "invalid app name",
    ),
    "variable name that is no name": (
        lambda d: d["variables"][1].update(name="Bad Name!"),
        "invalid variable name",
    ),
    "two variables with one name": (
        lambda d: d["variables"].append(copy.deepcopy(d["variables"][1])),
        "variable names must be unique",
    ),
    "file variable without a mount path": (
        lambda d: d["variables"][0].pop("mountPath"),
        "file variables need an absolute mountPath",
    ),
    "file size that is no size": (
        lambda d: d["variables"][0].update(maxSize="big"),
        "invalid size",
    ),
    "enum variable without values": (
        lambda d: d["variables"][1].pop("values"),
        "enum variables need 'values'",
    ),
    "enum default that is not one of its values": (
        lambda d: d["variables"][1].update(default="huge"),
        "default must be one of 'values'",
    ),
    "url access without a template": (
        lambda d: d["access"].__setitem__(0, {"type": "url"}),
        "url access needs 'template'",
    ),
    "password access without a source": (
        lambda d: d["access"].__setitem__(1, {"type": "password", "username": "{user}"}),
        "password access needs 'from: generated-password'",
    ),
}


@pytest.mark.parametrize("mutate, message", MALFORMED.values(), ids=MALFORMED.keys())
def test_a_malformed_spec_is_rejected_with_the_rule_it_breaks(mutate, message):
    errs = errors_of(spec_with(mutate))

    assert any(message in text for text in errs.values()), errs


def test_the_unmodified_spec_these_cases_start_from_is_valid():
    # Guards the table above: a case must fail because of its one change, not because the base is broken.
    assert parse_spec(VALID).name == "online-ide"


def test_resource_choices_that_are_not_a_mapping_of_containers_are_refused():
    from app.k8s.spec import apply_resource_overrides

    spec = parse_spec(VALID)

    for nonsense in (["ide"], "ide", 42):
        with pytest.raises(SpecValidationError) as exc:
            apply_resource_overrides(spec, nonsense)
        assert exc.value.errors == [
            {"loc": "resources", "msg": "must map container names to cpu/memory"}
        ]


def test_no_resource_choice_leaves_the_spec_as_it_is():
    from app.k8s.spec import apply_resource_overrides

    spec = parse_spec(VALID)

    assert apply_resource_overrides(spec, None) is spec
    assert apply_resource_overrides(spec, {}) is spec


# ----------------------------------------------------------------
# scope / scopeChoice: one workload per person or per team, chosen per deployment
# ----------------------------------------------------------------
def _scope_spec(**over):
    return {**copy.deepcopy(VALID), **over}


def test_scope_is_fixed_unless_the_author_allows_a_choice():
    spec = parse_spec(_scope_spec())  # VALID says scope: user
    assert spec.scope == "user" and spec.scope_choice is False
    assert parse_spec(_scope_spec(scopeChoice=True)).scope_choice is True
    assert "scopeChoice" in errors_of(_scope_spec(scopeChoice="yes"))


def test_a_choice_of_scope_makes_the_spec_fixed_and_keeps_the_original():
    from app.k8s.spec import apply_scope_override

    spec = parse_spec(_scope_spec(scope="user", scopeChoice=True))
    out = apply_scope_override(spec, "team")
    assert out.scope == "team" and out.scope_choice is False
    assert spec.scope == "user" and spec.scope_choice is True  # the reviewed spec is untouched
    # the worker validates the job's spec again: a fixed spec is valid on its own
    assert parse_spec(out.model_dump(mode="json", by_alias=True, exclude_none=True)).scope == "team"


def test_no_choice_leaves_the_spec_alone_and_the_author_scope_may_be_repeated():
    from app.k8s.spec import apply_scope_override

    spec = parse_spec(_scope_spec())
    assert apply_scope_override(spec, None) is spec
    assert apply_scope_override(spec, "user") is spec  # what the wizard sends for an app without a choice


@pytest.mark.parametrize(("choice", "msg"), [("team", "cannot be changed"), ("everyone", "must be"), (1, "must be")])
def test_a_scope_the_app_does_not_allow_or_that_does_not_exist_is_refused(choice, msg):
    from app.k8s.spec import apply_scope_override

    with pytest.raises(SpecValidationError) as exc:
        apply_scope_override(parse_spec(_scope_spec()), choice)
    assert [(e["loc"], msg in e["msg"]) for e in exc.value.errors] == [("scope", True)]
