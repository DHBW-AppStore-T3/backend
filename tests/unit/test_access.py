"""Unit tests for ``app.services.access.find_account`` (.github#7 H).

DB-less: users are plain objects. The function must return exactly the
caller's own entry or nothing, never the first entry of the team.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services.access import find_account, template_account_key

pytestmark = pytest.mark.unit


def _user(email: str) -> SimpleNamespace:
    return SimpleNamespace(userId=uuid.uuid4(), email=email, username=email.split("@")[0])


def _outputs(accounts: dict) -> dict:
    return {"user_accounts": {"value": accounts, "sensitive": True}}


def test_template_key_replaces_dots_like_the_templates():
    assert template_account_key("Team-1", "anna.b@dhbw.de") == "Team-1-anna-b"
    assert template_account_key("Team 2", "bob@x") == "Team 2-bob"


def test_the_entrys_own_username_is_never_a_candidate():
    alice, bob = _user("alice@x"), _user("bob@x")
    outputs = _outputs({"Team-1-alice": {"username": "alice", "auth": "pw-A"}})
    # The old matcher returned Alice's entry for Bob here.
    assert find_account(outputs, "Team-1", bob, [alice, bob]) is None
    assert find_account(outputs, "Team-1", alice, [alice, bob]) == ("Team-1-alice", {"username": "alice", "auth": "pw-A"})


def test_other_team_prefix_does_not_match():
    alice = _user("alice@x")
    outputs = _outputs({"Team-2-alice": {"auth": "pw"}})
    assert find_account(outputs, "Team-1", alice, [alice]) is None


def test_owner_fields_win_over_keys():
    alice, bob = _user("alice@x"), _user("bob@x")
    outputs = _outputs({
        "Team-1-alice": {"auth": "pw-bob", "email": "BOB@x"},
        "Team-1-bob": {"auth": "pw-alice", "user_id": str(alice.userId)},
    })
    assert find_account(outputs, "Team-1", bob, [alice, bob])[1]["auth"] == "pw-bob"
    assert find_account(outputs, "Team-1", alice, [alice, bob])[1]["auth"] == "pw-alice"


def test_named_entries_of_another_team_are_skipped():
    alice = _user("alice@x")
    outputs = _outputs({"k": {"auth": "pw", "email": "alice@x", "team": "Team-2"}})
    assert find_account(outputs, "Team-1", alice, [alice]) is None


def test_ambiguity_returns_nothing():
    a1, a2 = _user("max@one.de"), _user("max@two.de")
    keyed = _outputs({"Team-1-max": {"auth": "pw"}})
    assert find_account(keyed, "Team-1", a1, [a1, a2]) is None
    named = _outputs({"x": {"auth": "1", "email": "max@one.de"}, "y": {"auth": "2", "email": "max@one.de"}})
    assert find_account(named, "Team-1", a1, [a1, a2]) is None


def test_garbage_outputs_are_tolerated():
    alice = _user("alice@x")
    assert find_account(None, "Team-1", alice, [alice]) is None
    assert find_account({"user_accounts": "x"}, "Team-1", alice, [alice]) is None
    assert find_account(_outputs({"Team-1-alice": "not-a-dict"}), "Team-1", alice, [alice]) is None
