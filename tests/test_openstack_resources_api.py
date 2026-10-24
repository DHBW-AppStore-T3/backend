"""API-Level Tests für die OpenStack-Resources-Read-API.

Abgedeckt (unter ``/me/openstack-credentials/{id}/resources``):
 * GET  /flavors        (auth + payload-shape)
 * GET  /flavors        (401 ohne Auth)
 * GET  /images?status= (Visibility/Status-Filter)
 * POST /refresh        (nur der Cache dieser Credential)
 * POST /refresh        (invalidiert nachfolgende GETs)
 * GET  /subnets        (network_id-Query optional)

OpenStack wird über ``openstack_client.credential_connection`` gepatcht —
kein realer Cloud-Call.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from app.services import openstack_client
from tests.conftest import add_credential

CREDENTIAL_ID = uuid.UUID("00000000-0000-4000-8000-00000000c0de")
PREFIX = f"/me/openstack-credentials/{CREDENTIAL_ID}/resources"


# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------
def _flavor_mock(**overrides):
    f = MagicMock()
    defaults = {
        "id": "flavor-uuid-1",
        "name": "m1.small",
        "vcpus": 1,
        "ram": 2048,
        "disk": 20,
        "is_public": True,
    }
    defaults.update(overrides)
    for k, v in defaults.items():
        setattr(f, k, v)
    return f


def _image_mock(**overrides):
    img = MagicMock()
    defaults = {
        "id": "img-uuid-1",
        "name": "ubuntu-22.04",
        "status": "active",
        "visibility": "public",
        "size": 1024,
        "disk_format": "qcow2",
    }
    defaults.update(overrides)
    for k, v in defaults.items():
        setattr(img, k, v)
    return img


def _subnet_mock(**overrides):
    s = MagicMock()
    defaults = {
        "id": "subnet-uuid-1",
        "name": "subnet-a",
        "cidr": "10.0.0.0/24",
        "ip_version": 4,
        "network_id": "net-uuid-1",
        "gateway_ip": "10.0.0.1",
    }
    defaults.update(overrides)
    for k, v in defaults.items():
        setattr(s, k, v)
    return s


@pytest.fixture
def patched_user_connection(db, mock_user):
    """Legt die Credential ``CREDENTIAL_ID`` von ``mock_user`` an und patcht
    ``credential_connection`` im Resources-Router. Liefert ein MagicMock,
    das pro Test mit den passenden SDK-Antworten konfiguriert wird."""
    add_credential(db, mock_user, credential_id=CREDENTIAL_ID)
    conn = MagicMock()

    @contextmanager
    def _fake_conn(_row):
        yield conn

    with patch(
        "app.routers.openstack_resources.openstack_client.credential_connection",
        _fake_conn,
    ):
        yield conn


@pytest.fixture(autouse=True)
def _clear_openstack_cache():
    """Cache ist prozesslokal — zwischen Tests säubern, damit
    ein vorheriger ``cached_list``-Eintrag nicht den nächsten Test
    am Fetch-Pfad vorbeischleust."""
    openstack_client._cache.clear()
    yield
    openstack_client._cache.clear()


# ----------------------------------------------------------------
# 1. flavors — happy path
# ----------------------------------------------------------------
@pytest.mark.integration
def test_list_flavors_authenticated_returns_payload(
    client, db, mock_user, patched_user_connection
):
    patched_user_connection.compute.flavors.return_value = [
        _flavor_mock(id="f1", name="m1.small", vcpus=1, ram=2048, disk=20),
        _flavor_mock(id="f2", name="m1.large", vcpus=4, ram=8192, disk=80, is_public=False),
    ]

    response = client.get(f"{PREFIX}/flavors")

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert len(body) == 2
    assert body[0] == {
        "id": "f1",
        "name": "m1.small",
        "vcpus": 1,
        "ram": 2048,
        "disk": 20,
        "is_public": True,
    }
    assert body[1]["is_public"] is False
    # Compute-flavors muss mit get_extra_specs=False aufgerufen werden
    patched_user_connection.compute.flavors.assert_called_once_with(get_extra_specs=False)


# ----------------------------------------------------------------
# 2. flavors — unauthenticated 401
# ----------------------------------------------------------------
@pytest.mark.integration
def test_list_flavors_unauthenticated_401(unauth_client):
    response = unauth_client.get(f"{PREFIX}/flavors")
    assert response.status_code == 401


# ----------------------------------------------------------------
# 3. images — status filter
# ----------------------------------------------------------------
@pytest.mark.integration
def test_list_images_filters_by_visibility(
    client, db, mock_user, patched_user_connection
):
    patched_user_connection.image.images.return_value = [
        _image_mock(id="i1", name="ubuntu", status="active"),
        _image_mock(id="i2", name="debian", status="active"),
    ]

    response = client.get(f"{PREFIX}/images", params={"status": "active"})

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    assert all(item["status"] == "active" for item in body)
    # Der SDK-Call muss explizit den status-Filter durchreichen
    patched_user_connection.image.images.assert_called_once_with(status="active")


# ----------------------------------------------------------------
# 4. refresh — wirkt nur auf die eigene Credential; fremde sind 404
# ----------------------------------------------------------------
@pytest.mark.integration
def test_refresh_cache_owner_only(
    client, db, mock_user, mock_admin, patched_user_connection
):
    other = add_credential(db, mock_admin, identifier="admin-cred")
    openstack_client._cache[
        openstack_client._make_key(CREDENTIAL_ID, "flavors", None)
    ] = (float("inf"), [{"id": "f-mock-user"}])
    openstack_client._cache[
        openstack_client._make_key(other.credentialId, "flavors", None)
    ] = (float("inf"), [{"id": "f-mock-admin"}])

    assert client.post(f"{PREFIX}/refresh").status_code == 204
    # Eine fremde Credential gibt es für den Aufrufer nicht.
    assert client.post(f"/me/openstack-credentials/{other.credentialId}/resources/refresh").status_code == 404
    assert client.get(f"/me/openstack-credentials/{other.credentialId}/resources/flavors").status_code == 404

    assert openstack_client._make_key(CREDENTIAL_ID, "flavors", None) not in openstack_client._cache
    assert openstack_client._make_key(other.credentialId, "flavors", None) in openstack_client._cache


# ----------------------------------------------------------------
# 5. refresh — invalidiert nachfolgende GETs (Re-Fetch)
# ----------------------------------------------------------------
@pytest.mark.integration
def test_refresh_cache_invalidates_subsequent_list_calls(
    client, db, mock_user, patched_user_connection
):
    # Erster Call — füllt den Cache
    patched_user_connection.compute.flavors.return_value = [
        _flavor_mock(id="f1", name="initial"),
    ]
    response1 = client.get(f"{PREFIX}/flavors")
    assert response1.status_code == 200
    assert response1.json()[0]["name"] == "initial"
    assert patched_user_connection.compute.flavors.call_count == 1

    # Zweiter Call — soll aus dem Cache kommen, kein neuer SDK-Hit
    patched_user_connection.compute.flavors.return_value = [
        _flavor_mock(id="f1", name="updated"),
    ]
    response2 = client.get(f"{PREFIX}/flavors")
    assert response2.status_code == 200
    assert response2.json()[0]["name"] == "initial"  # Cache served
    assert patched_user_connection.compute.flavors.call_count == 1

    # Refresh-Knopf — invalidiert den Cache
    refresh = client.post(f"{PREFIX}/refresh")
    assert refresh.status_code == 204

    # Dritter Call — muss frisch fetchen
    response3 = client.get(f"{PREFIX}/flavors")
    assert response3.status_code == 200
    assert response3.json()[0]["name"] == "updated"
    assert patched_user_connection.compute.flavors.call_count == 2


@pytest.mark.integration
def test_refresh_by_marker_kind_drops_only_that_kind(
    client, db, mock_user, patched_user_connection
):
    """``kind`` is the marker vocabulary the UI knows; availability zones,
    cached per service, are dropped by their prefix."""
    patched_user_connection.compute.flavors.return_value = [_flavor_mock()]
    client.get(f"{PREFIX}/flavors")
    client.get(f"{PREFIX}/availability-zones")
    assert openstack_client.invalidate(CREDENTIAL_ID, "networks") == 0

    assert client.post(f"{PREFIX}/refresh", params={"kind": "availability_zone"}).status_code == 204
    with openstack_client._cache_lock:
        kinds = {key[1] for key in openstack_client._cache if key[0] == str(CREDENTIAL_ID)}
    assert kinds == {"flavors"}

    assert client.post(f"{PREFIX}/refresh", params={"kind": "flavor"}).status_code == 204
    client.get(f"{PREFIX}/flavors")
    assert patched_user_connection.compute.flavors.call_count == 2

    bad = client.post(f"{PREFIX}/refresh", params={"kind": "networks"})
    assert bad.status_code == 422
    assert bad.json()["detail"]["reason"] == "unknown_resource_kind"


# ----------------------------------------------------------------
# 6. subnets — network_id Query optional, wird aber durchgereicht
# ----------------------------------------------------------------
@pytest.mark.integration
def test_list_subnets_requires_network_id_query_param(
    client, db, mock_user, patched_user_connection
):
    # Ohne network_id: alle Subnets — kein Filter an die SDK
    patched_user_connection.network.subnets.return_value = [
        _subnet_mock(id="s1", network_id="net-1"),
        _subnet_mock(id="s2", network_id="net-2"),
    ]
    response_all = client.get(f"{PREFIX}/subnets")
    assert response_all.status_code == 200
    body_all = response_all.json()
    assert len(body_all) == 2
    patched_user_connection.network.subnets.assert_called_once_with()

    # Mit network_id: Filter wird serverseitig an OpenStack durchgereicht
    patched_user_connection.network.subnets.reset_mock()
    patched_user_connection.network.subnets.return_value = [
        _subnet_mock(id="s1", network_id="net-1"),
    ]
    response_filtered = client.get(
        f"{PREFIX}/subnets", params={"network_id": "net-1"}
    )
    assert response_filtered.status_code == 200
    body_filtered = response_filtered.json()
    assert len(body_filtered) == 1
    assert body_filtered[0]["network_id"] == "net-1"
    patched_user_connection.network.subnets.assert_called_once_with(network_id="net-1")
