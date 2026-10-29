"""Apps that run as pods (appstore.yaml): submit, approve, deploy, lifecycle, access (.github#4)."""

from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime

import pytest
import yaml

from app.k8s.fake import FakeKube
from app.k8s.render import (
    DeploymentCtx,
    Member,
    RenderSettings,
    Team,
    render,
    render_vm,
    workload_refs,
)
from app.k8s.spec import parse_spec
from app.models import (
    App,
    AppVersionApproval,
    AppVersionApprovalStatus,
    Deployment,
    Task,
    TaskStatus,
    TaskType,
    User,
    UserRole,
    UserToTeam,
)
from app.models import Team as TeamRow
from app.services import k8s_status
from tests.conftest import _make_client

pytestmark = pytest.mark.integration

DIGEST = "sha256:" + "a" * 64
SPEC = {
    "apiVersion": "appstore/v2",
    "name": "online-ide",
    "runtime": "kubernetes",
    "scope": "user",
    "workload": {
        "containers": [
            {
                "name": "ide",
                "image": f"ghcr.io/dhbw-appstore-t3/apps/code-server@{DIGEST}",
                "expose": {"port": 8080},
                "resources": {"cpu": "500m", "memory": "1Gi"},
                "env": [{"name": "PASSWORD", "from": "generated-password"}],
            }
        ],
        "storage": {"size": "5Gi", "mountPath": "/home/coder"},
    },
    "egress": "internet",
    "variables": [
        {"name": "cpu_class", "type": "enum", "values": ["small", "medium"], "default": "small"}
    ],
    "access": [
        {"type": "url", "template": "https://{workload}-{deployment}.apps.{zone}"},
        {"type": "password", "from": "generated-password", "username": "{user}"},
    ],
}


@pytest.fixture
def pod_repo(fake_git):
    """Every clone of an app commit holds ``appstore.yaml`` (pass a new spec to change it)."""

    def write(spec):
        from app.routers import apps as apps_router

        fake_git.files["appstore.yaml"] = yaml.safe_dump(spec)
        apps_router.clear_variable_cache()

    write(SPEC)
    return write


def _app(db, owner, *, is_private=False) -> App:
    app = App(
        name="ide",
        userId=owner.userId,
        git_link="https://github.com/example/ide",
        is_private=is_private,
    )
    db.add(app)
    db.commit()
    return app


def _student(db, name: str) -> User:
    user = User(
        keycloak_id=f"kc-{name}",
        email=f"{name}@student.dhbw.de",
        username=name,
        firstName=name,
        lastName="S",
        role=UserRole.STUDENT,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _deploy(client, app, *, teams=None, tag="v1.0"):
    body = {"name": "d", "appId": str(app.appId), "releaseTag": tag, "teams": teams or []}
    return client.post("/deployments/", json=body)


def _submit_and_approve(client, db, app, tag="v1.0"):
    response = client.post(f"/apps/{app.appId}/versions/{tag}/submit", json={})
    assert response.status_code == 201, response.text
    db.expire_all()
    row = db.query(AppVersionApproval).filter_by(appId=app.appId, version_tag=tag).one()
    row.status = AppVersionApprovalStatus.APPROVED
    db.commit()
    return response.json()


def _team(name, *members):
    return {"name": name, "userIds": [str(m.userId) for m in members]}


# ----------------------------------------------------------------
# versions
# ----------------------------------------------------------------
def test_submit_records_runtime_spec_hash_and_digests(client, db, mock_user, pod_repo, fake_git):
    app = _app(db, mock_user)

    body = _submit_and_approve(client, db, app)

    assert body["runtime"] == "kubernetes"
    assert body["image_digests"] == [DIGEST]
    assert len(body["spec_sha256"]) == 64


@pytest.mark.parametrize(
    ("mutate", "path"),
    [
        (
            lambda s: s["workload"]["containers"][0].update(
                image="ghcr.io/dhbw-appstore-t3/apps/x:latest"
            ),
            "workload.containers[0]",
        ),
        (
            lambda s: s["workload"]["containers"][0].update(image=f"docker.io/evil/x@{DIGEST}"),
            "workload.containers[0].image",
        ),
        (
            lambda s: s["workload"]["containers"][0].update(
                image=f"ghcr.io/dhbw-appstore-t3-evil/x@{DIGEST}"
            ),
            "workload.containers[0].image",
        ),
        (
            lambda s: s["workload"]["containers"][0].update(
                resources={"cpu": "64", "memory": "1Gi"}
            ),
            "workload.containers[0].resources.cpu",
        ),
        (lambda s: s.update(privileged=True), "privileged"),
        (
            lambda s: s["access"][0].update(template="http://{workload}-{deployment}.apps.{zone}"),
            "access[0]",
        ),
    ],
)
def test_an_invalid_spec_is_refused_with_a_field_path_and_cannot_be_approved(
    client, db, mock_user, admin_client, pod_repo, mutate, path
):
    spec = copy.deepcopy(SPEC)
    mutate(spec)
    pod_repo(spec)
    app = _app(db, mock_user)

    response = client.post(f"/apps/{app.appId}/versions/v1.0/submit", json={})

    assert response.status_code == 422
    assert path in {e["loc"] for e in response.json()["detail"]["errors"]}
    assert admin_client.post(f"/admin/apps/{app.appId}/versions/v1.0/approve").status_code in (
        404,
        422,
    )


def test_variables_and_runtime_come_from_the_spec(client, db, mock_user, pod_repo):
    app = _app(db, mock_user)

    variables = client.get(f"/apps/{app.appId}/variables", params={"version": "v1.0"}).json()
    runtime = client.get(f"/apps/{app.appId}/runtime", params={"version": "v1.0"}).json()

    assert [(v["name"], v["osType"], v["values"]) for v in variables] == [
        ("cpu_class", "enum", ["small", "medium"])
    ]
    assert (runtime["runtime"], runtime["scope"], runtime["egress"]) == (
        "kubernetes",
        "user",
        "internet",
    )


def test_a_version_without_an_appstore_yaml_is_unusable(client, db, mock_user, fake_git):
    del fake_git.files["appstore.yaml"]
    app = _app(db, mock_user)

    for response in (
        client.get(f"/apps/{app.appId}/runtime", params={"version": "v1.0"}),
        client.get(f"/apps/{app.appId}/variables", params={"version": "v1.0"}),
        client.post(f"/apps/{app.appId}/versions/v1.0/submit", json={}),
    ):
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["reason"] == "appstore_yaml_missing"


def test_an_unknown_runtime_is_refused(client, db, mock_user, pod_repo):
    pod_repo({"apiVersion": "appstore/v2", "name": "windows", "runtime": "openstack-vm", "scope": "user"})
    app = _app(db, mock_user)

    for response in (
        client.get(f"/apps/{app.appId}/runtime", params={"version": "v1.0"}),
        client.post(f"/apps/{app.appId}/versions/v1.0/submit", json={}),
    ):
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["reason"] == "invalid_app_spec"


def test_approving_checks_the_spec_is_the_submitted_one(
    client, db, mock_user, admin_client, pod_repo
):
    app = _app(db, mock_user)
    assert client.post(f"/apps/{app.appId}/versions/v1.0/submit", json={}).status_code == 201
    db.expire_all()
    row = db.query(AppVersionApproval).filter_by(appId=app.appId).one()
    row.spec_sha256 = "0" * 64
    db.commit()

    response = admin_client.post(f"/admin/apps/{app.appId}/versions/v1.0/approve")

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "spec_changed"


# ----------------------------------------------------------------
# deploying
# ----------------------------------------------------------------
def test_a_pod_app_deploys(client, db, mock_user, pod_repo, queued):
    alice, bob = _student(db, "alice"), _student(db, "bob")
    app = _app(db, mock_user)
    _submit_and_approve(client, db, app)

    response = _deploy(client, app, teams=[_team("Team-1", alice, bob)])

    assert response.status_code == 201, response.text
    deployment_id = uuid.UUID(response.json()["deploymentId"])
    name, kwargs = queued.last()
    assert name == "pods.deploy_application"
    assert "openstack_envelope" not in kwargs and "state_token_enc" not in kwargs
    assert kwargs["spec"]["workload"]["containers"][0]["image"].endswith(DIGEST)
    assert kwargs["owner"] == str(mock_user.userId)
    assert {m["user_id"] for m in kwargs["teams"]["Team-1"]} == {str(alice.userId), str(bob.userId)}
    row = db.get(Deployment, deployment_id)
    assert (row.runtime, row.k8s_workloads) == ("kubernetes", 2)
    assert row.k8s_namespace == f"dep-{deployment_id.hex[:8]}"
    assert response.json()["runtime"] == "kubernetes"


KUBEVIRT_SPEC = {
    "apiVersion": "appstore/v2",
    "name": "windows-matlab-app",
    "runtime": "kubevirt-vm",
    "scope": "user",
    "vm": {"cpuCores": 4, "memoryGiB": 8, "diskGiB": 90, "imageRef": "windows11-matlab-golden"},
    "egress": "internet",
    "access": [
        {"type": "url", "template": "https://{workload}-{deployment}.apps.{zone}"},
        {"type": "password", "from": "generated-password", "username": "{user}"},
    ],
}


def test_a_kubevirt_app_is_submitted_deployed_and_kept_as_a_cluster_app(
    client, db, mock_user, pod_repo, queued
):
    pod_repo(KUBEVIRT_SPEC)
    alice = _student(db, "alice")
    app = _app(db, mock_user)

    runtime = client.get(f"/apps/{app.appId}/runtime", params={"version": "v1.0"}).json()
    submitted = _submit_and_approve(client, db, app)
    response = _deploy(client, app, teams=[_team("Team-1", alice)])

    assert (runtime["runtime"], runtime["scope"]) == ("kubevirt-vm", "user")
    assert submitted["runtime"] == "kubevirt-vm" and submitted["spec_sha256"]
    assert response.status_code == 201, response.text
    name, kwargs = queued.last()
    assert name == "pods.deploy_vm_application"
    assert "openstack_envelope" not in kwargs and "state_token_enc" not in kwargs
    assert kwargs["spec"]["vm"]["imageRef"] == "windows11-matlab-golden"
    deployment_id = uuid.UUID(response.json()["deploymentId"])
    row = db.get(Deployment, deployment_id)
    assert (row.runtime, row.k8s_workloads) == ("kubevirt-vm", 1)
    # Without its namespace the orphan sweep would delete the running VM.
    assert row.k8s_namespace == f"dep-{deployment_id.hex[:8]}"
    assert response.json()["runtime"] == "kubevirt-vm"


KUBEVIRT_CONFIGURABLE = {
    **KUBEVIRT_SPEC,
    "vm": {
        **KUBEVIRT_SPEC["vm"],
        "bounds": {"cpuCores": {"min": 2, "max": 8}, "memoryGiB": {"min": 4, "max": 16}},
    },
    "userChoices": {"internet": True, "podNetwork": True},
}


def test_a_kubevirt_app_offers_vcpu_ram_and_network_to_the_wizard(client, db, mock_user, pod_repo):
    pod_repo(KUBEVIRT_CONFIGURABLE)
    app = _app(db, mock_user)

    runtime = client.get(f"/apps/{app.appId}/runtime", params={"version": "v1.0"}).json()

    assert runtime["runtime"] == "kubevirt-vm"
    assert runtime["resources"] == [
        {
            "container": "vm",
            "cpu": {"default": "4", "min": "2", "max": "8"},
            "memory": {"default": "8Gi", "min": "4Gi", "max": "16Gi"},
        }
    ]
    assert runtime["network"] == {"internet": {"default": True}, "podNetwork": {"default": False}}


def test_a_chosen_vm_size_and_network_reach_the_job_but_not_the_review(
    client, db, mock_user, pod_repo, queued
):
    pod_repo(KUBEVIRT_CONFIGURABLE)
    app = _app(db, mock_user)
    submitted = _submit_and_approve(client, db, app)
    body = {
        "name": "d",
        "appId": str(app.appId),
        "releaseTag": "v1.0",
        "teams": [_team("Team-1", _student(db, "alice"))],
        "userInputVar": {
            "resources": {"vm": {"cpu": "6", "memory": "12Gi"}},
            "network": {"internet": False, "podNetwork": True},
        },
    }

    response = client.post("/deployments/", json=body)

    assert response.status_code == 201, response.text
    _, kwargs = queued.last()
    assert (kwargs["spec"]["vm"]["cpuCores"], kwargs["spec"]["vm"]["memoryGiB"]) == (6, 12)
    # The job carries what was chosen as a fixed spec: the worker renders exactly that.
    assert kwargs["spec"]["network"] == {
        "internet": {"allowed": False, "default": False},
        "podNetwork": {"allowed": False, "default": True},
    }
    db.expire_all()
    assert (
        db.query(AppVersionApproval).filter_by(appId=app.appId).one().spec_sha256
        == submitted["spec_sha256"]
    )


def test_a_vm_size_outside_the_bounds_is_refused(client, db, mock_user, pod_repo, queued):
    pod_repo(KUBEVIRT_CONFIGURABLE)
    app = _app(db, mock_user)
    _submit_and_approve(client, db, app)
    body = {
        "name": "d",
        "appId": str(app.appId),
        "releaseTag": "v1.0",
        "teams": [_team("Team-1", _student(db, "alice"))],
        "userInputVar": {"resources": {"vm": {"cpu": "12"}}},
    }

    response = client.post("/deployments/", json=body)

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "invalid_resources"
    assert queued() == []


def test_a_changed_spec_after_approval_is_not_deployable(client, db, mock_user, pod_repo, queued):
    app = _app(db, mock_user)
    _submit_and_approve(client, db, app)
    changed = copy.deepcopy(SPEC)
    changed["workload"]["containers"][0]["resources"]["cpu"] = "1"
    pod_repo(changed)

    response = _deploy(client, app, teams=[_team("T", _student(db, "c"))])

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "version_not_approved"
    assert queued() == []


@pytest.mark.parametrize(
    ("teams", "reason"),
    [
        ([], "no_workloads"),
        (
            [{"name": "Team A", "userIds": []}, {"name": "team-a", "userIds": []}],
            "team_names_collide",
        ),
    ],
)
def test_workloads_must_make_sense(client, db, mock_user, pod_repo, queued, teams, reason):
    pod_repo({**SPEC, "scope": "team"})
    app = _app(db, mock_user, is_private=True)

    response = _deploy(client, app, teams=teams)

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == reason
    assert db.query(Deployment).count() == 0


def test_the_workload_limits(client, db, mock_user, pod_repo, queued, monkeypatch):
    from app.config import settings

    app = _app(db, mock_user, is_private=True)
    students = [_student(db, f"s{i}") for i in range(3)]
    monkeypatch.setattr(settings, "K8S_MAX_WORKLOADS_PER_DEPLOYMENT", 2)
    too_many = _deploy(client, app, teams=[_team("T", *students)])
    assert (
        too_many.status_code == 422 and too_many.json()["detail"]["reason"] == "too_many_workloads"
    )

    monkeypatch.setattr(settings, "K8S_MAX_WORKLOADS_PER_DEPLOYMENT", 60)
    monkeypatch.setattr(settings, "K8S_MAX_WORKLOADS_PER_OWNER", 4)
    assert _deploy(client, app, teams=[_team("T", *students)]).status_code == 201
    over_budget = _deploy(client, app, teams=[_team("T", students[0], students[1])])
    assert over_budget.status_code == 409
    assert over_budget.json()["detail"] == {
        "reason": "workload_budget_exceeded",
        "in_use": 3,
        "requested": 2,
        "budget": 4,
    }


def test_a_new_pod_deployment_never_gets_a_namespace_that_was_used(
    client, db, mock_user, pod_repo, queued, monkeypatch
):
    app = _app(db, mock_user, is_private=True)
    first = uuid.UUID(
        _deploy(client, app, teams=[_team("T", _student(db, "a"))]).json()["deploymentId"]
    )
    same_prefix = uuid.UUID(first.hex[:8] + "0" * 24)
    fresh = uuid.uuid4()
    ids = iter([same_prefix, fresh])
    monkeypatch.setattr("app.crud.deployments._new_id", lambda: next(ids))

    second = _deploy(client, app, teams=[_team("T", _student(db, "b"))])

    assert second.status_code == 201, second.text
    assert uuid.UUID(second.json()["deploymentId"]) == fresh
    assert db.get(Deployment, fresh).k8s_namespace == f"dep-{fresh.hex[:8]}"


# ----------------------------------------------------------------
# lifecycle
# ----------------------------------------------------------------
@pytest.fixture
def live_pod_deployment(client, db, mock_user, pod_repo, queued):
    """A deployed pod app with one team of two (Anna, Ben) and its namespace in a FakeKube."""
    anna, ben = _student(db, "anna"), _student(db, "ben")
    app = _app(db, mock_user, is_private=True)
    deployment_id = uuid.UUID(
        _deploy(client, app, teams=[_team("Team-1", anna, ben)]).json()["deploymentId"]
    )
    db.query(Task).update({"status": TaskStatus.SUCCESS})
    db.commit()
    queued.reset()

    kube = FakeKube(ready_after_polls=0)
    spec = parse_spec(SPEC)
    teams = [Team("Team-1", (Member(str(anna.userId), "anna"), Member(str(ben.userId), "ben")))]
    passwords = {r.name: "x" for r in workload_refs(spec, teams)}
    for obj in render(
        spec,
        DeploymentCtx(id=str(deployment_id)),
        teams,
        RenderSettings(zone="z.example.org"),
        passwords,
    ):
        kube.apply(obj)
    k8s_status.set_kube_factory(lambda: kube)
    yield deployment_id, kube, (anna, ben)
    k8s_status.set_kube_factory(None)


def test_pause_resume_destroy_dispatch_pod_jobs(client, db, live_pod_deployment, queued):
    deployment_id, _kube, _ = live_pod_deployment

    response = client.post(f"/deployments/{deployment_id}/pause")

    assert response.status_code == 202, response.text
    name, kwargs = queued.last()
    assert name == "pods.pause_deployment"
    assert "openstack_envelope" not in kwargs and "state_token_enc" not in kwargs


def test_admins_operate_pod_deployments(db, live_pod_deployment, admin_client, queued):
    deployment_id, _kube, _ = live_pod_deployment
    assert admin_client.post(f"/deployments/{deployment_id}/pause").status_code == 202


# ----------------------------------------------------------------
# an app an admin deactivated (review S7)
# ----------------------------------------------------------------
def _deactivate(db, app):
    db.expire_all()
    row = db.get(App, app.appId)
    row.hidden_by_admin, row.is_private = True, True
    db.commit()


def test_the_owner_cannot_deploy_an_app_an_admin_deactivated(client, db, mock_user, pod_repo, queued):
    alice = _student(db, "alice")
    app = _app(db, mock_user)
    _submit_and_approve(client, db, app)
    _deactivate(db, app)

    response = _deploy(client, app, teams=[_team("Team-1", alice)])

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "app_deactivated"
    assert db.query(Deployment).count() == 0
    assert queued() == []


def test_an_admin_can_still_deploy_a_deactivated_app(admin_client, db, mock_user, pod_repo, queued):
    alice = _student(db, "alice")
    app = _app(db, mock_user)
    _deactivate(db, app)

    response = _deploy(admin_client, app, teams=[_team("Team-1", alice)])

    assert response.status_code == 201, response.text


def test_a_paused_deployment_of_a_deactivated_app_cannot_be_resumed(
    client, db, live_pod_deployment, queued
):
    deployment_id, _kube, _ = live_pod_deployment
    assert client.post(f"/deployments/{deployment_id}/pause").status_code == 202
    db.query(Task).update({"status": TaskStatus.SUCCESS})
    db.commit()
    _deactivate(db, db.get(Deployment, deployment_id).app)
    queued.reset()

    response = client.post(f"/deployments/{deployment_id}/resume")

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "app_deactivated"
    assert queued() == []


def test_resources_of_a_pod_deployment_list_its_workloads(client, live_pod_deployment, monkeypatch):
    from app.config import settings

    deployment_id, _kube, (anna, _ben) = live_pod_deployment
    monkeypatch.setattr(settings, "K8S_ZONE", "z.example.org")

    body = client.get(f"/deployments/{deployment_id}/resources").json()

    assert body["runtime"] == "kubernetes" and body["live"] is True and "resources" not in body
    rows = {w["workload"]: w for w in body["workloads"]}
    assert set(rows) == {"team-1-anna", "team-1-ben"}
    assert rows["team-1-anna"]["ready"] is True and rows["team-1-anna"]["phase"] == "Running"
    assert rows["team-1-anna"]["team"] == "Team-1" and rows["team-1-anna"]["user_id"] == str(
        anna.userId
    )
    assert (
        rows["team-1-anna"]["url"]
        == f"https://team-1-anna-{deployment_id.hex[:8]}.apps.z.example.org"
    )


def test_resources_show_why_a_workload_does_not_start(client, live_pod_deployment):
    deployment_id, kube, _ = live_pod_deployment
    kube.ready_after_polls = 10
    kube.fail_reason = "ImagePullBackOff"
    rows = client.get(f"/deployments/{deployment_id}/resources").json()["workloads"]
    assert {w["phase"] for w in rows} == {"ImagePullBackOff"} and not any(w["ready"] for w in rows)


def test_a_namespace_of_another_deployment_shows_nothing(client, db, live_pod_deployment):
    deployment_id, kube, _ = live_pod_deployment
    namespace = db.get(Deployment, deployment_id).k8s_namespace
    kube.namespace_labels[namespace]["appstore.dhbw/deployment-id"] = str(uuid.uuid4())

    body = client.get(f"/deployments/{deployment_id}/resources").json()

    assert body["workloads"] == []


@pytest.mark.parametrize(
    ("action", "address"), [("restart", "team-1-ben"), ("reset", "reset:team-1-ben")]
)
def test_restart_and_reset_of_one_workload(client, live_pod_deployment, queued, action, address):
    deployment_id, _kube, _ = live_pod_deployment

    response = client.post(f"/deployments/{deployment_id}/workloads/team-1-ben/{action}")

    assert response.status_code == 202, response.text
    name, kwargs = queued.last()
    assert name == "pods.redeploy_resource" and kwargs["resource_address"] == address


@pytest.mark.parametrize(
    ("workload", "code"), [("unknown-one", 404), ("Team_1", 422), ("a;b", 422), ("-x", 422)]
)
def test_restart_refuses_unknown_or_malformed_workloads(
    client, live_pod_deployment, queued, workload, code
):
    deployment_id, _kube, _ = live_pod_deployment
    assert (
        client.post(f"/deployments/{deployment_id}/workloads/{workload}/restart").status_code
        == code
    )
    assert queued() == []


@pytest.fixture
def live_vm_deployment(client, db, mock_user, pod_repo, queued):
    """A deployed kubevirt-vm app with one person (Anna) and its namespace in a FakeKube."""
    pod_repo(KUBEVIRT_SPEC)
    anna = _student(db, "anna")
    app = _app(db, mock_user, is_private=True)
    _submit_and_approve(client, db, app)
    deployment_id = uuid.UUID(
        _deploy(client, app, teams=[_team("Team-1", anna)]).json()["deploymentId"]
    )
    db.query(Task).update({"status": TaskStatus.SUCCESS})
    db.commit()
    queued.reset()

    kube = FakeKube(ready_after_polls=0)
    spec = parse_spec(KUBEVIRT_SPEC)
    teams = [Team("Team-1", (Member(str(anna.userId), "anna"),))]
    passwords = {r.name: "x" for r in workload_refs(spec, teams)}
    kube.namespace_labels["vm-poc"] = {}
    for obj in render_vm(
        spec, DeploymentCtx(id=str(deployment_id)), teams, RenderSettings(zone="z.example.org"), passwords
    ):
        kube.apply(obj)
    k8s_status.set_kube_factory(lambda: kube)
    yield deployment_id, kube, anna
    k8s_status.set_kube_factory(None)


def test_admins_operate_vm_deployments(db, live_vm_deployment, admin_client, queued):
    deployment_id, _kube, _ = live_vm_deployment

    response = admin_client.post(f"/deployments/{deployment_id}/pause")

    assert response.status_code == 202, response.text
    name, _kwargs = queued.last()
    assert name == "pods.pause_deployment"


def test_the_owner_pauses_a_vm_deployment(client, live_vm_deployment, queued):
    deployment_id, _kube, _ = live_vm_deployment

    assert client.post(f"/deployments/{deployment_id}/pause").status_code == 202
    name, _kwargs = queued.last()
    assert name == "pods.pause_deployment"


def test_a_vm_is_restarted_by_name(client, live_vm_deployment, queued):
    deployment_id, kube, _ = live_vm_deployment
    (vm,) = [v.name for v in kube.snapshot(f"dep-{deployment_id.hex[:8]}").vmis]

    response = client.post(f"/deployments/{deployment_id}/workloads/{vm}/restart")

    assert response.status_code == 202, response.text
    name, kwargs = queued.last()
    assert name == "pods.redeploy_resource" and kwargs["resource_address"] == vm


def test_a_vm_cannot_be_reset(client, live_vm_deployment, queued):
    deployment_id, kube, _ = live_vm_deployment
    (vm,) = [v.name for v in kube.snapshot(f"dep-{deployment_id.hex[:8]}").vmis]

    response = client.post(f"/deployments/{deployment_id}/workloads/{vm}/reset")

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "reset_not_supported"
    assert queued() == []


def test_a_pod_workload_can_still_be_reset(client, live_pod_deployment, queued):
    deployment_id, _kube, _ = live_pod_deployment
    assert client.post(f"/deployments/{deployment_id}/workloads/team-1-ben/reset").status_code == 202




def test_the_vm_resource_endpoints_are_gone(client, live_pod_deployment):
    deployment_id, _kube, _ = live_pod_deployment
    assert client.get(f"/deployments/{deployment_id}/resources/x.y").status_code in (404, 405)
    assert client.post(f"/deployments/{deployment_id}/resources/x.y/redeploy").status_code in (404, 405)


def test_deleting_a_row_of_the_removed_vm_path_just_hides_it(client, db, mock_user, pod_repo, queued):
    app = _app(db, mock_user, is_private=True)
    row = Deployment(name="old", appId=app.appId, userId=mock_user.userId, runtime="openstack-vm", releaseTag="v1")
    db.add(row)
    db.commit()

    response = client.delete(f"/deployments/{row.deploymentId}")

    assert response.status_code == 204, response.text
    db.expire_all()
    assert db.get(Deployment, row.deploymentId).deleted_at is not None
    assert queued() == []


def test_cancel_ends_the_deploy_and_queues_the_cleanup(client, db, mock_user, pod_repo, queued):
    app = _app(db, mock_user, is_private=True)
    deployment_id = uuid.UUID(
        _deploy(client, app, teams=[_team("T", _student(db, "a"))]).json()["deploymentId"]
    )
    queued.reset()

    response = client.post(f"/deployments/{deployment_id}/cancel")

    assert response.status_code == 202, response.text
    db.expire_all()
    tasks = {t.type: t for t in db.query(Task).filter(Task.deploymentId == deployment_id)}
    assert tasks[TaskType.DEPLOY].status == TaskStatus.CANCELLED
    assert tasks[TaskType.DESTROY].status == TaskStatus.PENDING
    name, kwargs = queued.last()
    assert name == "pods.destroy_deployment" and "sweep_build_artifacts" not in kwargs


def test_deleting_a_cancelled_pod_deployment_still_destroys_its_namespace(
    client, db, mock_user, pod_repo, queued
):
    app = _app(db, mock_user, is_private=True)
    deployment_id = uuid.UUID(
        _deploy(client, app, teams=[_team("T", _student(db, "a"))]).json()["deploymentId"]
    )
    db.query(Task).update({"status": TaskStatus.CANCELLED})
    db.commit()
    queued.reset()

    response = client.delete(f"/deployments/{deployment_id}")

    assert response.status_code == 202, response.text
    assert queued.last()[0] == "pods.destroy_deployment"
    assert db.get(Deployment, deployment_id).deleted_at is None


# ----------------------------------------------------------------
# access: each person gets exactly their own entry
# ----------------------------------------------------------------
def _pod_outputs(deployment_id, teams_with_ids, *, scope="user"):
    """The outputs the pod worker writes (``pod_tasks.build_outputs``)."""
    accounts, team_vms = {}, {}
    for team, members in teams_with_ids.items():
        team_url = (
            f"https://{team.lower()}-{deployment_id.hex[:8]}.apps.z" if scope == "team" else None
        )
        team_vms[team] = {"url": team_url, "workloads": []}
        for user in members:
            workload = f"{team.lower()}-{user.username}" if scope == "user" else team.lower()
            team_vms[team]["workloads"].append(workload)
            accounts[f"{team}-{user.userId}"] = {
                "type": "password",
                "username": user.username,
                "auth": f"pw-of-{user.username}" if scope == "user" else "team-pw",
                "url": f"https://{workload}-{deployment_id.hex[:8]}.apps.z",
                "user_id": str(user.userId),
                "team": team,
                "workload": workload,
            }
    return {
        "team_vms": {"value": team_vms, "type": ["map", "dynamic"], "sensitive": False},
        "user_accounts": {"value": accounts, "type": ["map", "dynamic"], "sensitive": True},
    }


def _seed_pod_deployment(db, owner, teams_with_users, outputs):
    app = App(name="ide", userId=owner.userId, git_link="https://github.com/example/ide")
    db.add(app)
    db.flush()
    dep = Deployment(
        name="d", appId=app.appId, userId=owner.userId, runtime="kubernetes", releaseTag="v1"
    )
    db.add(dep)
    db.flush()
    db.add(
        Task(
            deploymentId=dep.deploymentId,
            type=TaskType.DEPLOY,
            status=TaskStatus.SUCCESS,
            outputs=json.dumps(outputs(dep.deploymentId)),
            created_at=datetime.utcnow(),
        )
    )
    for team_name, members in teams_with_users.items():
        team = TeamRow(name=team_name, deploymentId=dep.deploymentId)
        db.add(team)
        db.flush()
        for m in members:
            db.add(UserToTeam(userId=m.userId, teamId=team.teamId))
    db.commit()
    return dep


@pytest.mark.parametrize("scope", ["user", "team"])
def test_every_member_gets_exactly_their_own_access(db, mock_user, scope):
    from app.main import app as fastapi_app

    # Two people whose e-mail local parts normalise alike, plus a third.
    anna_dot = _student(db, "anna.b")
    anna_dash = _student(db, "anna-b")
    carl = _student(db, "carl")
    teams = {"Team-1": [anna_dot, anna_dash, carl]}
    dep = _seed_pod_deployment(
        db, mock_user, teams, lambda dep_id: _pod_outputs(dep_id, teams, scope=scope)
    )

    seen = {}
    try:
        for user in (anna_dot, anna_dash, carl):
            body = _make_client(user).get(f"/deployments/{dep.deploymentId}/my-access").json()
            [(key, entry)] = body["user_accounts"].items()
            assert entry["user_id"] == str(user.userId) and key == f"Team-1-{user.userId}"
            seen[user.username] = entry
            if scope == "user":
                # The team slice never carries someone else's link.
                assert body["team_vms"]["Team-1"]["url"] is None
    finally:
        fastapi_app.dependency_overrides.clear()
    if scope == "user":
        assert len({e["auth"] for e in seen.values()}) == 3
        assert len({e["url"] for e in seen.values()}) == 3


# ----------------------------------------------------------------
# namespaces without a live deployment
# ----------------------------------------------------------------
def test_namespaces_without_a_live_deployment_are_reported(db, mock_user):
    from app.k8s_orphans import find_orphans
    from app.utils.time import utcnow

    app = _app(db, mock_user, is_private=True)
    kube = FakeKube()
    rows = {}
    for name in ("live", "hidden", "destroyed"):
        dep_id = uuid.uuid4()
        dep = Deployment(
            deploymentId=dep_id,
            name=name,
            appId=app.appId,
            userId=mock_user.userId,
            runtime="kubernetes",
            k8s_namespace=f"dep-{dep_id.hex[:8]}",
        )
        db.add(dep)
        rows[name] = dep
        kube.apply(
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {
                    "name": f"dep-{dep_id.hex[:8]}",
                    "labels": {
                        "app.kubernetes.io/managed-by": "appstore",
                        "appstore.dhbw/deployment-id": str(dep_id),
                    },
                },
            }
        )
    db.flush()
    rows["hidden"].deleted_at = utcnow()
    db.add(
        Task(
            deploymentId=rows["destroyed"].deploymentId,
            type=TaskType.DESTROY,
            status=TaskStatus.SUCCESS,
        )
    )
    kube.apply(
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": "dep-ffffffff",
                "labels": {"app.kubernetes.io/managed-by": "appstore"},
            },
        }
    )
    db.commit()

    orphans = dict(find_orphans(kube, db))

    assert set(orphans) == {
        rows["hidden"].k8s_namespace,
        rows["destroyed"].k8s_namespace,
        "dep-ffffffff",
    }
    assert orphans["dep-ffffffff"] == ""


def test_the_orphan_check_ignores_namespaces_of_other_installations(db, mock_user):
    """Staging and prod share the cluster: each one's deployments live in its own database,
    so a namespace of the other installation is not an orphan of this one."""
    from app.k8s_orphans import find_orphans

    kube = FakeKube()
    for name, environment in (
        ("dep-0000000a", "appstore-staging"),
        ("dep-0000000b", "appstore-prod"),
    ):
        kube.apply(
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {
                    "name": name,
                    "labels": {
                        "app.kubernetes.io/managed-by": "appstore",
                        "appstore.dhbw/deployment-id": str(uuid.uuid4()),
                        "appstore.dhbw/environment": environment,
                    },
                },
            }
        )

    assert [ns for ns, _ in find_orphans(kube, db, "appstore-staging")] == ["dep-0000000a"]
    assert [ns for ns, _ in find_orphans(kube, db, "appstore-prod")] == ["dep-0000000b"]


def test_a_namespace_the_api_cannot_read_is_reported_and_the_others_still_checked(db):
    """A deploy that failed before it granted the API read access leaves a
    namespace the API may list but not look into (403)."""
    from app.k8s_orphans import find_orphans

    kube = FakeKube()
    for name in ("dep-0000000c", "dep-0000000d"):
        kube.apply(
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {
                    "name": name,
                    "labels": {
                        "app.kubernetes.io/managed-by": "appstore",
                        "appstore.dhbw/deployment-id": str(uuid.uuid4()),
                    },
                },
            }
        )
    real_snapshot = kube.snapshot

    def snapshot(namespace):
        if namespace == "dep-0000000c":
            raise RuntimeError("Forbidden: cannot list statefulsets")
        return real_snapshot(namespace)

    kube.snapshot = snapshot

    assert dict(find_orphans(kube, db)) == {
        "dep-0000000c": "unreadable",
        "dep-0000000d": kube.deployment_label("dep-0000000d"),
    }


# ----------------------------------------------------------------
# registration with ``submit_all_versions``
# ----------------------------------------------------------------
def test_auto_submitted_pod_versions_carry_their_spec_and_can_be_approved(
    db, mock_user, mock_admin, pod_repo, fake_git
):
    """Registering with ``submit_all_versions`` records what each review covers,
    as a manual submission does; before, a pod version was stored as a VM
    version and its approval failed with 409 ``spec_changed`` for good."""
    fake_git.tags = {"v1.0": fake_git.sha_for("v1.0"), "v2.0": fake_git.sha_for("v2.0")}
    response = _make_client(mock_user).post(
        "/apps/",
        json={
            "name": "ide",
            "git_link": "https://github.com/example/ide",
            "submit_all_versions": True,
        },
    )
    assert response.status_code == 201, response.text
    app_id = response.json()["appId"]
    rows = db.query(AppVersionApproval).filter_by(appId=uuid.UUID(app_id)).all()
    assert {(r.version_tag, r.runtime) for r in rows} == {
        ("v1.0", "kubernetes"),
        ("v2.0", "kubernetes"),
    }
    assert all(len(r.spec_sha256) == 64 and json.loads(r.image_digests) == [DIGEST] for r in rows)

    assert (
        _make_client(mock_admin).post(f"/admin/apps/{app_id}/versions/v1.0/approve").status_code
        == 200
    )


def test_auto_submit_skips_versions_with_an_invalid_spec(client, db, mock_user, pod_repo, fake_git):
    bad = copy.deepcopy(SPEC)
    bad["workload"]["containers"][0]["image"] = "docker.io/evil/x@" + DIGEST
    pod_repo(bad)
    fake_git.tags = {"v1.0": fake_git.sha_for("v1.0")}

    response = client.post(
        "/apps/",
        json={
            "name": "ide",
            "git_link": "https://github.com/example/ide",
            "submit_all_versions": True,
        },
    )

    assert response.status_code == 201, response.text
    assert db.query(AppVersionApproval).count() == 0


# ----------------------------------------------------------------
# uploaded files
# ----------------------------------------------------------------
def _file(content: bytes, name="a.zip"):
    import base64

    return {"name": name, "content_b64": base64.b64encode(content).decode(), "size": len(content)}


def _zip(size: int) -> bytes:
    """A valid ZIP whose single stored entry makes the archive about ``size`` bytes long."""
    import io
    import os
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("data.bin", os.urandom(size))
    return buf.getvalue()


def test_team_files_of_a_pod_app_are_refused_beyond_the_secret_limit(
    client, db, mock_user, pod_repo, queued
):
    spec = {
        **SPEC,
        "scope": "team",
        "variables": [
            {
                "name": "assignment",
                "type": "file",
                "scope": "team",
                "ext": ["zip"],
                "maxSize": "1Mi",
                "mountPath": "/data/a.zip",
            },
            {"name": "readme", "type": "file", "ext": ["md"], "mountPath": "/data/README.md"},
        ],
    }
    pod_repo(spec)
    app = _app(db, mock_user, is_private=True)
    alice = _student(db, "alice")
    body = {
        "name": "d",
        "appId": str(app.appId),
        "releaseTag": "v1.0",
        "teams": [_team("T", alice)],
        "files": {
            "assignment": {"T": _file(_zip(900 * 1024))},
            "readme": {"all": _file(b"y" * (200 * 1024), "README.md")},
        },
    }

    response = client.post("/deployments/", json=body)

    assert response.status_code == 413
    assert response.json()["detail"]["reason"] == "deployment_files_too_large"
    assert queued() == []


def test_team_files_of_a_pod_app_reach_the_job(client, db, mock_user, pod_repo, queued):
    spec = {
        **SPEC,
        "scope": "team",
        "variables": [
            {
                "name": "assignment",
                "type": "file",
                "scope": "team",
                "ext": ["zip"],
                "mountPath": "/data/a.zip",
            },
        ],
    }
    pod_repo(spec)
    app = _app(db, mock_user, is_private=True)
    alice = _student(db, "alice")
    body = {
        "name": "d",
        "appId": str(app.appId),
        "releaseTag": "v1.0",
        "teams": [_team("T", alice)],
        "files": {"assignment": {"T": _file(_zip(64), "../../etc/a.zip")}},
    }

    response = client.post("/deployments/", json=body)

    assert response.status_code == 201, response.text
    stored = queued.last()[1]["user_vars"]["terraform"]["assignment"]["T"]
    entry = stored.get("uploaded", stored)
    assert entry["name"] == "a.zip"  # no directories in a stored name


# ----------------------------------------------------------------
# what the detail tells the UI
# ----------------------------------------------------------------
def test_the_detail_carries_permissions_and_allowed_actions(client, db, mock_user, pod_repo, queued):
    alice = _student(db, "alice")
    app = _app(db, mock_user, is_private=True)
    deployment_id = _deploy(client, app, teams=[_team("T", alice)]).json()["deploymentId"]
    task = db.query(Task).filter(Task.deploymentId == uuid.UUID(deployment_id)).one()
    task.status = TaskStatus.SUCCESS
    db.commit()

    owner_view = client.get(f"/deployments/{deployment_id}").json()
    as_alice = _make_client(alice)
    member_view = as_alice.get(f"/deployments/{deployment_id}").json()

    assert owner_view["permissions"] == {"owner_view": True, "operate": True, "member": False}
    assert set(owner_view["actions"]) == {"destroy", "pause", "redeploy"}
    assert member_view["permissions"] == {"owner_view": False, "operate": False, "member": True}
    assert member_view["actions"] == []


# ----------------------------------------------------------------
# cpu/memory chosen in the wizard (resourceBounds)
# ----------------------------------------------------------------
def _tunable_spec():
    spec = copy.deepcopy(SPEC)
    c = spec["workload"]["containers"][0]
    c["resources"] = {"cpu": "1", "memory": "1Gi"}
    c["resourceBounds"] = {
        "cpu": {"min": "500m", "max": "2"},
        "memory": {"min": "1Gi", "max": "4Gi"},
    }
    return spec


def _deploy_with_resources(client, app, resources, teams):
    body = {
        "name": "d",
        "appId": str(app.appId),
        "releaseTag": "v1.0",
        "teams": teams,
        "userInputVar": {"resources": resources},
    }
    return client.post("/deployments/", json=body)


def test_the_runtime_offers_the_resource_choices(client, db, mock_user, pod_repo):
    pod_repo(_tunable_spec())
    app = _app(db, mock_user)
    response = client.get(f"/apps/{app.appId}/runtime", params={"version": "v1.0"})
    assert response.status_code == 200, response.text
    assert response.json()["resources"] == [
        {
            "container": "ide",
            "cpu": {"default": "1", "min": "500m", "max": "2"},
            "memory": {"default": "1Gi", "min": "1Gi", "max": "4Gi"},
        }
    ]


def test_a_chosen_size_reaches_the_job_but_not_the_review(client, db, mock_user, pod_repo, queued):
    pod_repo(_tunable_spec())
    alice = _student(db, "alice")
    app = _app(db, mock_user)
    _submit_and_approve(client, db, app)

    response = _deploy_with_resources(
        client, app, {"ide": {"cpu": "2", "memory": "3Gi"}}, [_team("T", alice)]
    )

    assert response.status_code == 201, response.text
    _, kwargs = queued.last()
    assert kwargs["spec"]["workload"]["containers"][0]["resources"] == {"cpu": "2", "memory": "3Gi"}


def test_a_size_outside_the_bounds_is_refused(client, db, mock_user, pod_repo, queued):
    pod_repo(_tunable_spec())
    alice = _student(db, "alice")
    app = _app(db, mock_user)
    _submit_and_approve(client, db, app)

    response = _deploy_with_resources(client, app, {"ide": {"memory": "64Gi"}}, [_team("T", alice)])

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "invalid_resources"
    assert queued() == []


def test_an_app_without_bounds_ignores_no_choice(client, db, mock_user, pod_repo, queued):
    alice = _student(db, "alice")
    app = _app(db, mock_user)
    _submit_and_approve(client, db, app)

    response = _deploy_with_resources(client, app, {"ide": {"cpu": "1"}}, [_team("T", alice)])

    assert response.status_code == 422
    assert (
        response.json()["detail"]["errors"][0]["msg"]
        == "this container's resources cannot be changed"
    )


# ----------------------------------------------------------------
# setup progress of an app (log line) and the last event of a ready workload
# ----------------------------------------------------------------
def test_parse_progress_takes_the_last_valid_line_and_checks_it():
    log = "\n".join(
        [
            'appstore-progress {"percent": 10, "step": "Download"}',
            "noise",
            'appstore-progress {"percent": 250}',  # out of range: ignored
            'appstore-progress {"percent": 42, "step": "Install\\nMATLAB", "etaSeconds": 600}',
            "appstore-progress not json",
        ]
    )
    assert k8s_status.parse_progress(log) == {
        "percent": 42,
        "step": "Install MATLAB",
        "etaSeconds": 600,
    }
    assert k8s_status.parse_progress("nothing here") is None
    assert k8s_status.parse_progress('appstore-progress {"percent": true}') is None
    assert (
        k8s_status.parse_progress('appstore-progress {"percent": 5, "etaSeconds": -1}')[
            "etaSeconds"
        ]
        is None
    )


class _Core:
    def __init__(self, log: str):
        self.log, self.calls = log, []

    def read_namespaced_pod_log(self, pod, namespace, **kwargs):
        self.calls.append((pod, namespace, kwargs))
        return self.log


def test_a_running_workload_shows_its_setup_progress(live_pod_deployment):
    deployment_id, kube, _ = live_pod_deployment
    kube._core = _Core('appstore-progress {"percent": 37, "step": "MATLAB", "etaSeconds": 900}')
    namespace = f"dep-{deployment_id.hex[:8]}"

    rows = k8s_status.workload_views(deployment_id, namespace)

    assert {r["workload"] for r in rows} and all(
        r["setup"] == {"percent": 37, "step": "MATLAB", "etaSeconds": 900} for r in rows
    )
    assert kube._core.calls[0][2]["tail_lines"] == 100


def test_a_finished_setup_shows_no_bar(live_pod_deployment):
    deployment_id, kube, _ = live_pod_deployment
    kube._core = _Core('appstore-progress {"percent": 100, "step": "done"}')
    rows = k8s_status.workload_views(deployment_id, f"dep-{deployment_id.hex[:8]}")
    assert all(r["setup"] is None for r in rows)


def test_a_ready_workload_does_not_show_an_old_warning(live_pod_deployment):
    from app.k8s.kube import EventInfo

    deployment_id, kube, _ = live_pod_deployment
    namespace = f"dep-{deployment_id.hex[:8]}"
    original = kube.snapshot

    def with_warning(ns):
        snap = original(ns)
        snap.events = [
            EventInfo(
                object=f"pod/{p.name}",
                reason="Unhealthy",
                message="Readiness probe failed: dial tcp",
            )
            for p in snap.pods
        ]
        return snap

    kube.snapshot = with_warning
    rows = k8s_status.workload_views(deployment_id, namespace)
    assert rows and all(r["ready"] and r["lastEvent"] is None for r in rows)


# internet / pod network chosen in the wizard (userChoices)
# ----------------------------------------------------------------
def _networked_spec(**choices):
    return {**copy.deepcopy(SPEC), "userChoices": choices}


def _deploy_with_network(client, app, network, teams):
    body = {
        "name": "d",
        "appId": str(app.appId),
        "releaseTag": "v1.0",
        "teams": teams,
        "userInputVar": {"network": network},
    }
    return client.post("/deployments/", json=body)


def test_the_runtime_offers_only_the_released_network_choices(client, db, mock_user, pod_repo):
    pod_repo(_networked_spec(podNetwork=True))
    app = _app(db, mock_user)
    response = client.get(f"/apps/{app.appId}/runtime", params={"version": "v1.0"})
    assert response.status_code == 200, response.text
    assert response.json()["network"] == {"podNetwork": {"default": False}}


def test_the_runtime_offers_no_network_choice_without_user_choices(client, db, mock_user, pod_repo):
    pod_repo(SPEC)
    app = _app(db, mock_user)
    assert (
        client.get(f"/apps/{app.appId}/runtime", params={"version": "v1.0"}).json()["network"] == {}
    )


def test_a_chosen_network_reaches_the_job(client, db, mock_user, pod_repo, queued):
    pod_repo(_networked_spec(internet=True, podNetwork=True))
    alice = _student(db, "alice")
    app = _app(db, mock_user)
    _submit_and_approve(client, db, app)

    response = _deploy_with_network(
        client, app, {"internet": True, "podNetwork": True}, [_team("T", alice)]
    )

    assert response.status_code == 201, response.text
    _, kwargs = queued.last()
    assert kwargs["spec"]["network"] == {
        "internet": {"allowed": False, "default": True},
        "podNetwork": {"allowed": False, "default": True},
    }


def test_a_network_choice_without_release_is_refused(client, db, mock_user, pod_repo, queued):
    pod_repo(_networked_spec(podNetwork=True))
    alice = _student(db, "alice")
    app = _app(db, mock_user)
    _submit_and_approve(client, db, app)

    response = _deploy_with_network(client, app, {"internet": True}, [_team("T", alice)])

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["reason"] == "invalid_network"
    assert detail["errors"][0]["loc"] == "network.internet"
    assert queued() == []


def test_a_non_boolean_network_choice_is_refused(client, db, mock_user, pod_repo, queued):
    pod_repo(_networked_spec(internet=True))
    alice = _student(db, "alice")
    app = _app(db, mock_user)
    _submit_and_approve(client, db, app)

    response = _deploy_with_network(client, app, {"internet": "yes"}, [_team("T", alice)])

    assert response.status_code == 422 and response.json()["detail"]["reason"] == "invalid_network"


def _body(app, network, teams):
    return {
        "name": "d",
        "appId": str(app.appId),
        "releaseTag": "v1.0",
        "teams": teams,
        "userInputVar": {} if network is None else {"network": network},
    }


def test_the_job_payload_of_an_old_style_spec_carries_the_network_block_not_the_old_keys(
    client, db, mock_user, pod_repo, queued
):
    # Deploy the worker first: one that predates the network block forbids unknown keys.
    pod_repo(SPEC)
    alice = _student(db, "alice")
    app = _app(db, mock_user)
    _submit_and_approve(client, db, app)

    assert (
        client.post("/deployments/", json=_body(app, None, [_team("T", alice)])).status_code == 201
    )
    _, kwargs = queued.last()
    assert not {"egress", "podNetwork", "userChoices"} & set(kwargs["spec"])
    assert kwargs["spec"]["network"]["internet"] == {"allowed": False, "default": True}  # SPEC says egress: internet


def test_the_job_payload_carries_pod_network_when_the_author_sets_it(
    client, db, mock_user, pod_repo, queued
):
    pod_repo({**copy.deepcopy(SPEC), "podNetwork": "namespace"})
    alice = _student(db, "alice")
    app = _app(db, mock_user)
    _submit_and_approve(client, db, app)

    assert (
        client.post("/deployments/", json=_body(app, None, [_team("T", alice)])).status_code == 201
    )
    assert queued.last()[1]["spec"]["network"]["podNetwork"] == {"allowed": False, "default": True}


def test_a_network_choice_keeps_the_approved_hash_and_is_stored(
    client, db, mock_user, pod_repo, queued
):
    pod_repo(_networked_spec(podNetwork=True))
    alice = _student(db, "alice")
    app = _app(db, mock_user)
    _submit_and_approve(client, db, app)
    before = db.query(AppVersionApproval).filter_by(appId=app.appId).one().spec_sha256

    response = _deploy_with_network(client, app, {"podNetwork": True}, [_team("T", alice)])

    assert response.status_code == 201, response.text
    _, kwargs = queued.last()
    assert kwargs["spec"]["network"]["podNetwork"] == {"allowed": False, "default": True}
    db.expire_all()
    assert db.query(AppVersionApproval).filter_by(appId=app.appId).one().spec_sha256 == before
    row = db.get(Deployment, uuid.UUID(response.json()["deploymentId"]))
    stored = row.userInputVar
    assert (json.loads(stored) if isinstance(stored, str) else stored)["network"] == {
        "podNetwork": True
    }


def test_a_network_choice_for_an_app_without_user_choices_is_refused(
    client, db, mock_user, pod_repo, queued
):
    pod_repo(SPEC)
    alice = _student(db, "alice")
    app = _app(db, mock_user)
    _submit_and_approve(client, db, app)

    response = _deploy_with_network(client, app, {"internet": True}, [_team("T", alice)])

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "invalid_network"
    assert response.json()["detail"]["errors"][0]["loc"] == "network.internet"
    assert queued() == []


def test_valid_resources_do_not_save_an_invalid_network_choice(
    client, db, mock_user, pod_repo, queued
):
    spec = _tunable_spec()
    spec["userChoices"] = {"podNetwork": True}
    pod_repo(spec)
    alice = _student(db, "alice")
    app = _app(db, mock_user)
    _submit_and_approve(client, db, app)
    body = {
        "name": "d",
        "appId": str(app.appId),
        "releaseTag": "v1.0",
        "teams": [_team("T", alice)],
        "userInputVar": {"resources": {"ide": {"cpu": "2"}}, "network": {"internet": True}},
    }

    response = client.post("/deployments/", json=body)

    assert response.status_code == 422 and response.json()["detail"]["reason"] == "invalid_network"
    assert queued() == []


# ----------------------------------------------------------------
# network: {internet, podNetwork: {allowed, default}} of appstore.yaml
# ----------------------------------------------------------------
def _network_spec(**network):
    return {**{k: v for k, v in copy.deepcopy(SPEC).items() if k != "egress"}, "network": network}


def test_an_app_decides_what_the_wizard_offers_and_where_it_starts(client, db, mock_user, pod_repo):
    pod_repo(
        _network_spec(
            internet={"allowed": True, "default": True},
            podNetwork={"allowed": True, "default": False},
        )
    )
    app = _app(db, mock_user)
    runtime = client.get(f"/apps/{app.appId}/runtime", params={"version": "v1.0"}).json()
    assert runtime["network"] == {"internet": {"default": True}, "podNetwork": {"default": False}}
    assert runtime["egress"] == "internet"  # what a deployment gets when nobody changes anything


def test_a_setting_the_app_does_not_allow_is_not_offered_and_cannot_be_chosen(
    client, db, mock_user, pod_repo, queued
):
    # No internet for this app (nothing to download), and nobody can switch it on.
    pod_repo(_network_spec(internet={"allowed": False}, podNetwork={"allowed": True}))
    alice = _student(db, "alice")
    app = _app(db, mock_user)
    _submit_and_approve(client, db, app)
    runtime = client.get(f"/apps/{app.appId}/runtime", params={"version": "v1.0"}).json()
    assert runtime["network"] == {"podNetwork": {"default": False}}

    refused = _deploy_with_network(client, app, {"internet": True}, [_team("T", alice)])
    assert refused.status_code == 422
    assert refused.json()["detail"]["errors"] == [
        {"loc": "network.internet", "msg": "this setting cannot be changed"}
    ]
    assert queued() == []  # nothing was started


def test_the_start_values_of_the_app_reach_the_job_when_nobody_chooses(
    client, db, mock_user, pod_repo, queued
):
    pod_repo(
        _network_spec(
            internet={"allowed": True, "default": True},
            podNetwork={"allowed": True, "default": True},
        )
    )
    alice = _student(db, "alice")
    app = _app(db, mock_user)
    _submit_and_approve(client, db, app)
    assert (
        client.post("/deployments/", json=_body(app, None, [_team("T", alice)])).status_code == 201
    )
    assert queued.last()[1]["spec"]["network"] == {
        "internet": {"allowed": True, "default": True},
        "podNetwork": {"allowed": True, "default": True},
    }


def test_a_choice_overrides_the_start_value_and_fixes_the_job(client, db, mock_user, pod_repo, queued):
    pod_repo(
        _network_spec(
            internet={"allowed": True, "default": True},
            podNetwork={"allowed": True, "default": False},
        )
    )
    alice = _student(db, "alice")
    app = _app(db, mock_user)
    _submit_and_approve(client, db, app)

    response = _deploy_with_network(
        client, app, {"internet": False, "podNetwork": True}, [_team("T", alice)]
    )

    assert response.status_code == 201, response.text
    assert queued.last()[1]["spec"]["network"] == {
        "internet": {"allowed": False, "default": False},
        "podNetwork": {"allowed": False, "default": True},
    }


# ----------------------------------------------------------------
# scopeChoice: per person or per team, chosen in the wizard
# ----------------------------------------------------------------
def _scope_choice_spec(**over):
    return {**copy.deepcopy(SPEC), "scope": "user", "scopeChoice": True, **over}


def _deploy_with_scope(client, app, scope, teams):
    body = {
        "name": "d",
        "appId": str(app.appId),
        "releaseTag": "v1.0",
        "teams": teams,
        "userInputVar": {} if scope is None else {"scope": scope},
    }
    return client.post("/deployments/", json=body)


def test_the_runtime_tells_the_wizard_whether_the_scope_can_be_chosen(client, db, mock_user, pod_repo):
    pod_repo(_scope_choice_spec())
    app = _app(db, mock_user)
    runtime = client.get(f"/apps/{app.appId}/runtime", params={"version": "v1.0"}).json()
    assert (runtime["scope"], runtime["scope_choice"]) == ("user", True)
    pod_repo(SPEC)
    fixed = client.get(f"/apps/{app.appId}/runtime", params={"version": "v1.0"}).json()
    assert fixed["scope_choice"] is False


def test_a_chosen_scope_reaches_the_job_as_a_fixed_spec(client, db, mock_user, pod_repo, queued):
    pod_repo(_scope_choice_spec())
    alice = _student(db, "alice")
    app = _app(db, mock_user)
    _submit_and_approve(client, db, app)

    assert _deploy_with_scope(client, app, "team", [_team("T", alice)]).status_code == 201
    spec = queued.last()[1]["spec"]
    assert (spec["scope"], spec["scopeChoice"]) == ("team", False)


def test_without_a_choice_the_authors_scope_is_used(client, db, mock_user, pod_repo, queued):
    pod_repo(_scope_choice_spec())
    alice = _student(db, "alice")
    app = _app(db, mock_user)
    _submit_and_approve(client, db, app)

    assert _deploy_with_scope(client, app, None, [_team("T", alice)]).status_code == 201
    assert queued.last()[1]["spec"]["scope"] == "user"


def test_a_scope_choice_the_app_does_not_allow_is_refused(client, db, mock_user, pod_repo, queued):
    pod_repo({**copy.deepcopy(SPEC), "scope": "user"})
    alice = _student(db, "alice")
    app = _app(db, mock_user)
    _submit_and_approve(client, db, app)

    refused = _deploy_with_scope(client, app, "team", [_team("T", alice)])
    assert refused.status_code == 422
    assert refused.json()["detail"]["reason"] == "invalid_scope"
    assert refused.json()["detail"]["errors"] == [{"loc": "scope", "msg": "this setting cannot be changed"}]
    assert queued() == []
