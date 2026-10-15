"""Unit tests for :mod:`app.services.openstack_validator` and :mod:`app.keystone_urls`.

``openstack.connect`` is mocked, no real Keystone is required. The
API-level behaviour (what is stored, which project counts) is covered in
``tests/test_openstack_credentials_api.py``.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from keystoneauth1.identity.base import BaseIdentityPlugin
from pydantic import ValidationError

from app import keystone_urls
from app.models import OpenStackAuthType
from app.schemas import OpenStackCredentialUpsert
from app.services import openstack_validator

pytestmark = pytest.mark.unit


def _password_payload(**overrides) -> OpenStackCredentialUpsert:
    base = {
        "auth_type": OpenStackAuthType.PASSWORD,
        "auth_url": "https://keystone.example/v3",
        "region_name": "RegionOne",
        "identifier": "alice",
        "secret": "s3cret",
        "project_name": "demo",
        "user_domain_name": "Default",
    }
    base.update(overrides)
    return OpenStackCredentialUpsert(**base)


def _connection(project_id="p-1", project_name="Demo") -> MagicMock:
    auth = MagicMock(spec=BaseIdentityPlugin)
    auth.get_access.return_value = MagicMock(project_id=project_id, project_name=project_name)
    conn = MagicMock()
    conn.session.auth = auth
    return conn


def test_password_login_maps_to_v3password_and_closes_the_connection():
    conn = _connection()
    with patch.object(openstack_validator.openstack, "connect", return_value=conn) as connect:
        scope = openstack_validator.validate(_password_payload())

    assert scope == openstack_validator.Scope(project_id="p-1", project_name="Demo")
    kwargs = connect.call_args.kwargs
    assert kwargs["auth_type"] == "password"
    assert (kwargs["username"], kwargs["password"]) == ("alice", "s3cret")
    assert kwargs["project_name"] == "demo"
    assert kwargs["project_domain_name"] == "Default"  # falls back to the user domain
    assert kwargs["api_timeout"] == openstack_validator.TIMEOUT_SECONDS
    conn.close.assert_called_once()


def test_no_process_wide_socket_timeout_is_touched():
    import socket

    before = socket.getdefaulttimeout()
    with patch.object(openstack_validator.openstack, "connect", return_value=_connection()):
        openstack_validator.validate(_password_payload())
    assert socket.getdefaulttimeout() == before


def test_missing_password_fields_fail_in_the_schema():
    with pytest.raises(ValidationError):
        _password_payload(user_domain_name=None)


def test_a_url_outside_the_allowlist_never_connects(monkeypatch):
    monkeypatch.setattr(openstack_validator.settings, "APP_ENV", "production")
    monkeypatch.setattr(openstack_validator.settings, "OPENSTACK_ALLOWED_AUTH_URLS", "https://keystone.example/v3")
    with patch.object(openstack_validator.openstack, "connect") as connect, pytest.raises(
        openstack_validator.CredentialRejected
    ) as exc:
        openstack_validator.validate(_password_payload(auth_url="https://evil.example/v3"))
    assert exc.value.reason == "auth_url_not_allowed"
    connect.assert_not_called()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://Keystone.Example:5000/v3/", "https://keystone.example:5000"),
        ("https://keystone.example/identity/v3", "https://keystone.example/identity"),
        ("https://keystone.example", "https://keystone.example"),
        (" https://keystone.example/v3 ", "https://keystone.example"),
        ("http://keystone.example/v3", None),
        ("https://u:p@keystone.example/v3", None),
        ("https://keystone.example/v3?x=1", None),
        ("https://keystone.example:99999/v3", None),
        ("ftp://keystone.example", None),
        ("keystone.example", None),
    ],
)
def test_normalised(url, expected):
    assert keystone_urls.normalised(url) == expected


def test_allowlist_matching():
    allowlist = "https://keystone.example:5000/v3, not a url ,https://other.example"
    assert keystone_urls.auth_url_allowed("https://keystone.example:5000", allowlist, development=False)
    assert keystone_urls.auth_url_allowed("https://other.example/v3", allowlist, development=False)
    assert not keystone_urls.auth_url_allowed("https://keystone.example", allowlist, development=True)
    assert not keystone_urls.auth_url_allowed(None, allowlist, development=True)
    assert keystone_urls.auth_url_allowed("https://any.example", "", development=True)
    assert not keystone_urls.auth_url_allowed("https://any.example", "", development=False)
