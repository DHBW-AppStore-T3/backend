"""The live status of a pod that runs but is not Ready: starting or not responding."""

from datetime import UTC, datetime, timedelta

import pytest

from app.k8s.kube import EventInfo, PodInfo, Snapshot, StatefulSetInfo
from app.k8s.render import DEPLOYMENT_LABEL, FIELD_MANAGER, MANAGED_BY_LABEL
from app.services import k8s_status

pytestmark = pytest.mark.unit

DEP = "5d2f6c1e-7a8b-4c9d-8e0f-1a2b3c4d5e6f"
NS = "dep-5d2f6c1e"
NOW = datetime.now(UTC)
READINESS = EventInfo("Pod/team-1-0", "Unhealthy", "Readiness probe failed: dial tcp 10.0.0.7:8080: connect: connection refused")


def pod(**fields) -> PodInfo:
    return PodInfo(name="team-1-0", workload="team-1", phase="Running", **fields)


def ago(seconds: float) -> datetime:
    return NOW - timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def ready_timeout(monkeypatch):
    monkeypatch.setattr(k8s_status.settings, "K8S_READY_TIMEOUT_SECONDS", 600)


@pytest.mark.parametrize(
    ("info", "phase"),
    [
        # Up for 30 s, not Ready since it came up: its app is still starting.
        (pod(started_at=ago(30), not_ready_since=ago(31)), "Starting"),
        # Never answered, longer than a deploy waits for it.
        (pod(started_at=ago(700), not_ready_since=ago(701)), "NotResponding"),
        # Answered (was Ready) and stopped answering 20 s ago.
        (pod(started_at=ago(3600), not_ready_since=ago(20)), "NotResponding"),
        # Restarted after it had been Ready: starting again.
        (pod(started_at=ago(5), not_ready_since=ago(40), restarts=1), "Starting"),
        # No times known (fake cluster): starting.
        (pod(), "Starting"),
        (pod(ready=True, started_at=ago(3600)), "Running"),
        (pod(waiting_reason="CrashLoopBackOff", restarts=4), "CrashLoopBackOff"),
    ],
)
def test_phase_of_a_running_pod(info, phase):
    snap = Snapshot(exists=True, pods=[info])
    assert k8s_status._phase(snap, "team-1", 1, NOW)[0] == phase


class _Cluster:
    def __init__(self, snap: Snapshot):
        self.snap = snap

    def snapshot(self, namespace):
        return self.snap


def _row(info: PodInfo, events: list[EventInfo]):
    snap = Snapshot(
        exists=True,
        labels={DEPLOYMENT_LABEL: DEP, MANAGED_BY_LABEL: FIELD_MANAGER},
        statefulsets=[StatefulSetInfo("team-1", 1, 0)],
        pods=[info],
        events=events,
    )
    k8s_status.set_kube_factory(lambda: _Cluster(snap))
    try:
        (row,) = k8s_status.workload_views(DEP, NS)
    finally:
        k8s_status.set_kube_factory(None)
    return row


def test_a_starting_pod_does_not_show_its_expected_readiness_failures():
    mount = EventInfo("Pod/team-1-0", "FailedMount", "volume not attached yet")
    row = _row(pod(started_at=ago(30), not_ready_since=ago(31)), [mount, READINESS])
    assert (row["phase"], row["ready"]) == ("Starting", False)
    assert row["lastEvent"] == "FailedMount: volume not attached yet"


def test_a_pod_that_does_not_respond_shows_why():
    row = _row(pod(started_at=ago(3600), not_ready_since=ago(20)), [READINESS])
    assert (row["phase"], row["ready"]) == ("NotResponding", False)
    assert row["lastEvent"].startswith("Unhealthy: Readiness probe failed: dial tcp")
