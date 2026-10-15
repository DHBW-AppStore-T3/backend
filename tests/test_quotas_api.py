"""API-level tests for ``GET /me/openstack-credentials/{id}/quota``.

The endpoint asks OpenStack, with the caller's own credential, for the
compute/storage/network quota of that credential's project.

OpenStack is mocked on the connection-level: ``openstack.connect`` is
patched in ``openstack_client`` so the tests never reach a real cloud.
A fully configured ``MagicMock`` plays the role of the live connection
(``conn.compute.*``, ``conn.volume.*``, ``conn.network.*``) and lets each
test control success / failure modes without spinning up a deployment.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import TEST_PROJECT, add_credential

CONNECT = "app.services.openstack_client.openstack.connect"


# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------
def _make_quota_conn(project_id: str = TEST_PROJECT) -> MagicMock:
    """Build a MagicMock shaped like an OpenStack ``Connection`` that the
    quotas endpoint can iterate without exploding.

    The numeric fields are deliberately small + stable so assertions can
    pin exact values."""
    conn = MagicMock()
    conn.current_project_id = project_id

    # === COMPUTE ===
    compute_limits = MagicMock()
    compute_limits.instances = 10
    compute_limits.cores = 20
    compute_limits.ram = 40960  # MB
    conn.compute.get_quota_set.return_value = compute_limits

    compute_usage = MagicMock()
    compute_usage.absolute.total_instances_used = 2
    compute_usage.absolute.total_cores_used = 4
    compute_usage.absolute.total_ram_used = 8192
    conn.compute.get_limits.return_value = compute_usage

    # === STORAGE ===
    volume_limits = MagicMock()
    volume_limits.volumes = 10
    volume_limits.snapshots = 10
    volume_limits.gigabytes = 1000
    conn.volume.get_quota_set.return_value = volume_limits

    vol = MagicMock()
    vol.size = 20
    conn.volume.volumes.return_value = [vol]
    conn.volume.snapshots.return_value = []

    # === NETWORK ===
    network_limits = MagicMock()
    network_limits.floatingip = 50
    network_limits.security_group = 10
    network_limits.security_group_rule = 100
    network_limits.network = 100
    network_limits.port = 500
    network_limits.router = 10
    conn.network.get_quota.return_value = network_limits

    # Empty live resources keep the arithmetic predictable.
    conn.network.ips.return_value = []
    sg = MagicMock()
    sg.id = "sg-1"
    conn.network.security_groups.return_value = [sg]
    conn.network.networks.return_value = []
    conn.network.ports.return_value = []
    conn.network.routers.return_value = []
    conn.network.security_group_rules.return_value = []

    return conn


@pytest.mark.integration
def test_quota_of_the_credentials_project(student_client, db, mock_student):
    """Anyone may look at the quota of their own projects, students included."""
    row = add_credential(db, mock_student)
    conn = _make_quota_conn()

    with patch(CONNECT, return_value=conn):
        response = student_client.get(f"/me/openstack-credentials/{row.credentialId}/quota")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["compute"]["instances"] == {"used": 2, "limit": 10, "available": 8, "unit": None}
    assert body["compute"]["ram"]["unit"] == "MB"
    assert body["storage"]["volumes"]["used"] == 1
    assert body["storage"]["gigabytes"]["used"] == 20
    assert body["network"]["floating_ips"]["limit"] == 50
    # Quota and usage are asked for the credential's project; only its own
    # networks count, not shared or external ones.
    conn.compute.get_quota_set.assert_called_once_with(TEST_PROJECT)
    conn.network.networks.assert_called_once_with(project_id=TEST_PROJECT)


@pytest.mark.integration
def test_quota_unauthenticated_401(unauth_client):
    assert unauth_client.get("/me/openstack-credentials/00000000-0000-4000-8000-000000000000/quota").status_code == 401


@pytest.mark.integration
def test_quota_openstack_unreachable_is_502(client, db, mock_user):
    row = add_credential(db, mock_user)
    conn = _make_quota_conn()
    conn.compute.get_quota_set.side_effect = ConnectionError("keystone down")

    with patch(CONNECT, return_value=conn):
        response = client.get(f"/me/openstack-credentials/{row.credentialId}/quota")

    assert response.status_code == 502
    assert response.json()["detail"] == {"reason": "openstack_unavailable"}


@pytest.mark.integration
def test_unlimited_quota_has_no_available(student_client, db, mock_student):
    """OpenStack's -1 means unlimited; ``-1 - used`` would look over quota."""
    row = add_credential(db, mock_student)
    conn = _make_quota_conn()
    conn.volume.get_quota_set.return_value.gigabytes = -1

    with patch(CONNECT, return_value=conn):
        response = student_client.get(f"/me/openstack-credentials/{row.credentialId}/quota")

    assert response.status_code == 200, response.text
    assert response.json()["storage"]["gigabytes"] == {"used": 20, "limit": -1, "available": None, "unit": "GB"}
