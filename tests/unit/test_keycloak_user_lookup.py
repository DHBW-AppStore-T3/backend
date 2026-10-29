"""The Keycloak admin client, the token verifier and the user lookups built on them (no network)."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.config import settings
from app.utils import keycloak_auth

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_verifier_leaks_out_of_these_tests():
    keycloak_auth.reset_verifier()
    yield
    keycloak_auth.reset_verifier()


def _admin(**methods):
    admin = MagicMock()
    for name, behaviour in methods.items():
        if isinstance(behaviour, Exception) or callable(behaviour):
            getattr(admin, name).side_effect = behaviour
        else:
            getattr(admin, name).return_value = behaviour
    return admin


def test_the_admin_client_uses_the_service_account_and_verifies_certificates():
    with patch.object(keycloak_auth, "KeycloakAdmin") as client:
        keycloak_auth.get_keycloak_admin()

    kwargs = client.call_args.kwargs
    assert kwargs["verify"] is True
    assert (
        kwargs["server_url"] == settings.KEYCLOAK_SERVER_URL
        and kwargs["realm_name"] == settings.KEYCLOAK_REALM
    )
    assert kwargs["client_id"] == settings.KEYCLOAK_CLIENT_ID
    assert kwargs["client_secret_key"] == settings.KEYCLOAK_CLIENT_SECRET


def test_the_verifier_is_built_once_from_the_settings_and_again_after_a_reset(monkeypatch):
    monkeypatch.setattr(settings, "OIDC_AUDIENCE", "")
    with patch.object(keycloak_auth, "OidcVerifier") as verifier:
        first, second = keycloak_auth.get_verifier(), keycloak_auth.get_verifier()
        assert first is second and verifier.call_count == 1
        built = verifier.call_args.kwargs
        assert (
            built["issuer"] == settings.keycloak_issuer
            and built["jwks_url"] == settings.keycloak_jwks_url
        )
        assert built["allowed_clients"] == settings.oidc_allowed_clients
        assert built["audience"] is None  # no audience configured: not required

        monkeypatch.setattr(settings, "OIDC_AUDIENCE", "appstore")
        keycloak_auth.reset_verifier()
        keycloak_auth.get_verifier()
        assert verifier.call_count == 2 and verifier.call_args.kwargs["audience"] == "appstore"


def test_the_user_search_returns_the_simplified_shape_with_the_enabled_flag():
    admin = _admin(
        get_users=[
            {
                "id": "1",
                "username": "ada",
                "email": "ada@x.de",
                "firstName": "Ada",
                "lastName": "L",
            },
            {"id": "2", "username": "bob", "email": "bob@x.de", "enabled": False},
        ]
    )

    with patch.object(keycloak_auth, "get_keycloak_admin", return_value=admin):
        found = keycloak_auth.search_keycloak_users("a", max_results=5)

    admin.get_users.assert_called_once_with({"search": "a", "max": 5})
    assert found == [
        {
            "id": "1",
            "username": "ada",
            "email": "ada@x.de",
            "firstName": "Ada",
            "lastName": "L",
            "enabled": True,
        },
        {
            "id": "2",
            "username": "bob",
            "email": "bob@x.de",
            "firstName": "",
            "lastName": "",
            "enabled": False,
        },
    ]


@pytest.mark.parametrize("broken", ["the admin client", "the search"])
def test_a_failing_user_search_is_a_500_that_does_not_leak_the_cause(broken):
    cause = RuntimeError("keycloak.internal:8443 refused the connection")
    if broken == "the admin client":
        patched = patch.object(keycloak_auth, "get_keycloak_admin", side_effect=cause)
    else:
        patched = patch.object(
            keycloak_auth, "get_keycloak_admin", return_value=_admin(get_users=cause)
        )

    with patched, pytest.raises(HTTPException) as failed:
        keycloak_auth.search_keycloak_users("a")

    assert (
        failed.value.status_code == 500 and failed.value.detail == "Failed to search Keycloak users"
    )
    assert "keycloak.internal" not in str(failed.value.detail)


def test_looking_up_no_ids_does_not_call_keycloak():
    with patch.object(keycloak_auth, "get_keycloak_admin") as admin:
        assert keycloak_auth.get_keycloak_users_by_ids([]) == {}

    admin.assert_not_called()


def test_users_are_looked_up_by_id_and_keyed_by_it():
    admin = _admin(
        get_user={"id": "k1", "username": "ada", "email": "ada@x.de", "firstName": "Ada"}
    )

    with patch.object(keycloak_auth, "get_keycloak_admin", return_value=admin):
        found = keycloak_auth.get_keycloak_users_by_ids(["k1"])

    assert found == {
        "k1": {
            "id": "k1",
            "username": "ada",
            "email": "ada@x.de",
            "firstName": "Ada",
            "lastName": "",
        }
    }


def test_an_id_keycloak_does_not_answer_for_falls_back_to_a_search_and_unknown_ones_are_left_out():
    def get_user(kid):
        raise RuntimeError("404 not found")

    def get_users(query):
        # Only "k2" can be found by searching; "k3" is unknown and "k4"'s search fails as well.
        if query["search"] == "k4":
            raise RuntimeError("search failed")
        return (
            [{"id": "k2", "username": "bob", "email": "bob@x.de"}]
            if query["search"] == "k2"
            else []
        )

    admin = _admin(get_user=get_user, get_users=get_users)

    with patch.object(keycloak_auth, "get_keycloak_admin", return_value=admin):
        found = keycloak_auth.get_keycloak_users_by_ids(["k2", "k3", "k4"])

    assert list(found) == ["k2"] and found["k2"]["username"] == "bob"
    assert admin.get_users.call_args_list[0].args[0] == {"search": "k2", "max": 1}


def test_a_record_without_an_id_is_keyed_by_the_id_that_was_asked_for():
    admin = _admin(get_user={"username": "ada", "email": "ada@x.de"})

    with patch.object(keycloak_auth, "get_keycloak_admin", return_value=admin):
        found = keycloak_auth.get_keycloak_users_by_ids(["k1"])

    assert found["k1"]["id"] == "k1"


def test_an_unreachable_keycloak_is_a_500_for_the_lookup_by_ids():
    with (
        patch.object(
            keycloak_auth, "get_keycloak_admin", side_effect=RuntimeError("connection refused")
        ),
        pytest.raises(HTTPException) as failed,
    ):
        keycloak_auth.get_keycloak_users_by_ids(["k1"])

    assert (
        failed.value.status_code == 500 and failed.value.detail == "Failed to fetch Keycloak users"
    )
