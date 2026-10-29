"""Unit tests for the deployment-side scope/extension enforcement
helpers added alongside the marker-grammar extension.

Covers:
* ``attach_files_to_user_input`` extension-filter enforcement
* ``validate_scoped_user_input`` shape + slot-target checks

These are pure functions — no DB, no FastAPI app, no queue.
"""
import base64

import pytest
from fastapi import HTTPException

from app.schemas import DeploymentFileUpload, Team
from app.services.deployment_input import (
    attach_files_to_user_input,
    validate_scoped_user_input,
)


def _payload(name: str, content: bytes) -> DeploymentFileUpload:
    """Build the Pydantic model the wizard ships per slot."""
    return DeploymentFileUpload(
        name=name,
        content_b64=base64.b64encode(content).decode("ascii"),
        size=len(content),
        content_type="application/octet-stream",
    )


# ----------------------------------------------------------------
# File-extension filter (defense-in-depth on top of the wizard's
# ``accept`` attribute).
# ----------------------------------------------------------------

VAR_PDF = {
    "name": "task_pdf",
    "source": "terraform",
    "osType": "file",
    "osScope": "all",
    "varScope": "all",
    "fileExtensions": ["pdf"],
}


@pytest.mark.unit
def test_file_extension_filter_accepts_matching_suffix():
    out = attach_files_to_user_input(
        user_input_var={"terraform": {}},
        files={"task_pdf": {"all": _payload("aufgabe.pdf", b"%PDF-1.4...")}},
        variable_definitions=[VAR_PDF],
    )
    assert "task_pdf" in out["terraform"]
    assert out["terraform"]["task_pdf"]["all"]["name"] == "aufgabe.pdf"


@pytest.mark.unit
def test_file_extension_filter_rejects_mismatched_suffix():
    with pytest.raises(HTTPException) as exc:
        attach_files_to_user_input(
            user_input_var={"terraform": {}},
            files={"task_pdf": {"all": _payload("malware.exe", b"\x4d\x5a")}},
            variable_definitions=[VAR_PDF],
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["reason"] == "file_extension_rejected"
    assert exc.value.detail["allowed"] == ["pdf"]


@pytest.mark.unit
def test_file_extension_filter_is_case_insensitive():
    """``Aufgabe.PDF`` matches a ``pdf`` filter — uploaders shouldn't
    care about file-extension case."""
    out = attach_files_to_user_input(
        user_input_var={"terraform": {}},
        files={"task_pdf": {"all": _payload("Aufgabe.PDF", b"%PDF-1.4...")}},
        variable_definitions=[VAR_PDF],
    )
    assert "task_pdf" in out["terraform"]


@pytest.mark.unit
def test_file_extension_filter_skipped_without_definitions():
    """When no variable_definitions are passed, the helper falls back
    to its pre-change behavior — no filter applied. Keeps the helper
    usable from older code paths that don't yet load definitions."""
    out = attach_files_to_user_input(
        user_input_var={"terraform": {}},
        files={"some_var": {"all": _payload("anything.dat", b"...")}},
        variable_definitions=None,
    )
    assert "some_var" in out["terraform"]


# ----------------------------------------------------------------
# Scoped user-input validation (team / user slot key allowlist).
# ----------------------------------------------------------------

VAR_TEAM_FLAVOR = {
    "name": "team_flavor_ids",
    "source": "terraform",
    "osType": "flavor",
    "osMode": "id",
    "osMulti": False,
    "varScope": "team",
}

VAR_USER_HOST = {
    "name": "user_hostname_prefix",
    "source": "terraform",
    "varScope": "user",
}


def _team(name: str, user_ids: list[str]) -> Team:
    return Team(name=name, userIds=user_ids)


@pytest.mark.unit
def test_scoped_team_ok_with_matching_team_names():
    validate_scoped_user_input(
        user_input_var={
            "terraform": {"team_flavor_ids": {"Team-1": "m1.small", "Team-2": "m1.medium"}},
        },
        variable_definitions=[VAR_TEAM_FLAVOR],
        teams_payload=[_team("Team-1", ["u1"]), _team("Team-2", ["u2"])],
    )


@pytest.mark.unit
def test_scoped_team_rejects_unknown_team_name():
    with pytest.raises(HTTPException) as exc:
        validate_scoped_user_input(
            user_input_var={
                "terraform": {"team_flavor_ids": {"Team-X": "m1.small"}},
            },
            variable_definitions=[VAR_TEAM_FLAVOR],
            teams_payload=[_team("Team-1", ["u1"])],
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["reason"] == "unknown_scope_target"
    assert exc.value.detail["scope"] == "team"


@pytest.mark.unit
def test_scoped_team_rejects_non_map_value():
    with pytest.raises(HTTPException) as exc:
        validate_scoped_user_input(
            user_input_var={"terraform": {"team_flavor_ids": "m1.small"}},
            variable_definitions=[VAR_TEAM_FLAVOR],
            teams_payload=[_team("Team-1", ["u1"])],
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["reason"] == "scoped_var_not_map"


@pytest.mark.unit
def test_scoped_user_accepts_composite_keys_under_known_team():
    validate_scoped_user_input(
        user_input_var={
            "terraform": {
                "user_hostname_prefix": {
                    "Team-1-alice": "alice-vm",
                    "Team-1-bob": "bob-vm",
                },
            },
        },
        variable_definitions=[VAR_USER_HOST],
        teams_payload=[_team("Team-1", ["uid-alice", "uid-bob"])],
    )


@pytest.mark.unit
def test_scoped_user_rejects_key_with_unknown_team_prefix():
    with pytest.raises(HTTPException) as exc:
        validate_scoped_user_input(
            user_input_var={
                "terraform": {
                    "user_hostname_prefix": {"Other-alice": "alice-vm"},
                },
            },
            variable_definitions=[VAR_USER_HOST],
            teams_payload=[_team("Team-1", ["u1"])],
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["reason"] == "unknown_scope_target"


@pytest.mark.unit
def test_scoped_var_skipped_when_value_absent():
    """Eine scoped Variable, die der User leer gelassen hat, darf nicht
    fälschlich als Fehler erscheinen — der HCL-Default soll greifen."""
    validate_scoped_user_input(
        user_input_var={"terraform": {}},
        variable_definitions=[VAR_TEAM_FLAVOR],
        teams_payload=[_team("Team-1", ["u1"])],
    )


@pytest.mark.unit
def test_non_scoped_variable_is_ignored():
    """Variablen ohne ``varScope`` werden vom Scope-Validator ignoriert
    — sie laufen weiterhin durch die normalen Terraform-Type-Checks."""
    validate_scoped_user_input(
        user_input_var={"terraform": {"some_string": "foo"}},
        variable_definitions=[{"name": "some_string", "source": "terraform"}],
        teams_payload=[],
    )


# ----------------------------------------------------------------
# Required scoped variables: every team / person must have a value
# (otherwise Terraform fails deep in the worker log with an opaque error)
# ----------------------------------------------------------------

REQUIRED_TEAM_VAR = {**VAR_TEAM_FLAVOR, "required": True}
REQUIRED_USER_VAR = {**VAR_USER_HOST, "required": True}
TWO_TEAMS = [_team("Team-1", ["u1"]), _team("Team-2", ["u2"])]


def _missing_slots(variable, value, teams=TWO_TEAMS):
    with pytest.raises(HTTPException) as exc:
        validate_scoped_user_input(
            user_input_var={"terraform": {variable["name"]: value}},
            variable_definitions=[variable],
            teams_payload=teams,
        )
    assert exc.value.status_code == 422 and exc.value.detail["reason"] == "required_slot_empty"
    assert exc.value.detail["variable"] == variable["name"]
    return exc.value.detail["missing_slots"]


@pytest.mark.unit
def test_a_required_team_variable_with_every_team_filled_passes():
    validate_scoped_user_input(
        user_input_var={
            "terraform": {"team_flavor_ids": {"Team-1": "m1.small", "Team-2": "m1.medium"}}
        },
        variable_definitions=[REQUIRED_TEAM_VAR],
        teams_payload=TWO_TEAMS,
    )


@pytest.mark.unit
def test_a_required_team_variable_that_leaves_a_team_out_names_that_team():
    assert _missing_slots(REQUIRED_TEAM_VAR, {"Team-1": "m1.small"}) == ["Team-2"]


@pytest.mark.unit
@pytest.mark.parametrize("blank", ["", None, [], {}])
def test_a_blank_slot_counts_as_not_filled(blank):
    assert _missing_slots(REQUIRED_TEAM_VAR, {"Team-1": "m1.small", "Team-2": blank}) == ["Team-2"]


@pytest.mark.unit
def test_a_required_team_variable_that_was_not_sent_at_all_misses_every_team():
    # A hand-crafted request leaves a required variable out: it is treated as an empty map, not as "use the default".
    assert _missing_slots(REQUIRED_TEAM_VAR, None) == ["Team-1", "Team-2"]


@pytest.mark.unit
def test_an_optional_team_variable_that_was_not_sent_keeps_its_default():
    validate_scoped_user_input(
        user_input_var={"terraform": {"team_flavor_ids": None}},
        variable_definitions=[VAR_TEAM_FLAVOR],
        teams_payload=TWO_TEAMS,
    )


@pytest.mark.unit
def test_a_required_user_variable_with_one_person_left_blank_names_that_slot():
    value = {"Team-1-anna": "ws-anna", "Team-1-ben": ""}
    assert _missing_slots(REQUIRED_USER_VAR, value) == ["Team-1-ben"]


@pytest.mark.unit
def test_a_user_slot_needs_a_person_behind_the_team_name():
    with pytest.raises(HTTPException) as exc:
        validate_scoped_user_input(
            user_input_var={"terraform": {"user_hostname_prefix": {"Team-1": "ws"}}},
            variable_definitions=[VAR_USER_HOST],
            teams_payload=TWO_TEAMS,
        )
    assert exc.value.detail["reason"] == "unknown_scope_target"
    assert exc.value.detail["slot"] == "Team-1" and "TeamName-Username" in exc.value.detail["hint"]


@pytest.mark.unit
def test_a_required_file_variable_is_left_to_the_upload_checks():
    # The slots of a file variable are checked for identity here; their content and emptiness by
    # attach_files_to_user_input (file_var_empty, ...), which names the problem better.
    file_var = {**VAR_TEAM_FLAVOR, "name": "assignment", "osType": "file", "required": True}
    validate_scoped_user_input(
        user_input_var={"terraform": {"assignment": {"Team-1": {}, "Team-2": None}}},
        variable_definitions=[file_var],
        teams_payload=TWO_TEAMS,
    )


@pytest.mark.unit
def test_teams_may_arrive_as_plain_dicts_and_a_team_without_a_name_is_ignored():
    teams = [{"name": "Team-1", "userIds": ["u1"]}, {"userIds": ["u2"]}]
    ok = {"terraform": {"team_flavor_ids": {"Team-1": "m1.small"}}}
    validate_scoped_user_input(ok, [VAR_TEAM_FLAVOR], teams)

    with pytest.raises(HTTPException) as exc:
        validate_scoped_user_input(
            {"terraform": {"team_flavor_ids": {"": "x"}}}, [VAR_TEAM_FLAVOR], teams
        )
    assert exc.value.detail["reason"] == "unknown_scope_target" and exc.value.detail["allowed"] == [
        "Team-1"
    ]


# ----------------------------------------------------------------
# What a hostile or sloppy upload request can still try after pydantic
# ----------------------------------------------------------------


def _txt(content: bytes, declared_size: int | None = None) -> DeploymentFileUpload:
    return DeploymentFileUpload(
        name="notes.txt",
        content_b64=base64.b64encode(content).decode("ascii"),
        size=len(content) if declared_size is None else declared_size,
        content_type="text/plain",
    )


def _refused(files, user_input_var=None):
    with pytest.raises(HTTPException) as exc:
        attach_files_to_user_input(
            user_input_var or {"terraform": {}}, files, variable_definitions=None
        )
    return exc.value


@pytest.mark.unit
def test_a_file_variable_the_wizard_already_filled_is_a_collision():
    refused = _refused(
        {"notes": {"all": _txt(b"hi")}}, user_input_var={"terraform": {"notes": "typed in"}}
    )
    assert refused.status_code == 422
    assert refused.detail == {"reason": "file_var_collision", "variable": "notes"}


@pytest.mark.unit
def test_a_file_variable_without_any_file_is_refused():
    assert _refused({"notes": {}}).detail == {"reason": "file_var_empty", "variable": "notes"}


@pytest.mark.unit
def test_the_declared_size_must_match_the_real_one_give_or_take_one_byte():
    content = b"hello"
    out = attach_files_to_user_input(
        {"terraform": {}}, {"notes": {"all": _txt(content, declared_size=6)}}, None
    )
    assert "notes" in out["terraform"]  # off by one: the client counted before encoding

    refused = _refused({"notes": {"all": _txt(content, declared_size=500)}})
    assert refused.status_code == 422
    assert (refused.detail["reason"], refused.detail["declared"], refused.detail["actual"]) == (
        "file_size_mismatch",
        500,
        5,
    )


@pytest.mark.unit
def test_all_files_of_a_deployment_together_may_not_exceed_the_deployment_cap():
    two_mib = b"a" * (2 * 1024 * 1024)
    five = {"notes": {f"Team-{i}": _txt(two_mib) for i in range(5)}}
    out = attach_files_to_user_input({"terraform": {}}, five, None)  # exactly 10 MiB: allowed
    assert len(out["terraform"]["notes"]) == 5

    six = {"notes": {f"Team-{i}": _txt(two_mib) for i in range(6)}}
    refused = _refused(six)
    assert refused.status_code == 413 and refused.detail["reason"] == "deployment_files_too_large"
    assert refused.detail["limit_bytes"] == 10 * 1024 * 1024


@pytest.mark.unit
def test_a_refused_upload_is_logged_with_its_reason_but_never_with_the_clients_file_name(caplog):
    import logging

    from app.services.deployment_input import log_rejected_upload

    forged = "evil.txt\nupload_rejected user=admin reason=none"
    exc = HTTPException(
        422,
        detail={
            "reason": "file_extension_rejected",
            "code": None,
            "variable": "notes",
            "slot": "Team-1",
            "filename": forged,
        },
    )

    with caplog.at_level(logging.WARNING, logger="app.services.deployment_input"):
        log_rejected_upload("u-1", exc)

    assert len(caplog.records) == 1
    line = caplog.records[0].getMessage()
    assert (
        line
        == 'upload_rejected user=u-1 reason="file_extension_rejected" variable="notes" slot="Team-1"'
    )
    assert "evil" not in line and "\n" not in line


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc",
    [
        HTTPException(404, detail="not found"),
        HTTPException(422, detail={"reason": "something_else"}),
    ],
    ids=["text-detail", "other-reason"],
)
def test_errors_that_are_no_upload_rejection_are_not_logged_as_one(caplog, exc):
    import logging

    from app.services.deployment_input import log_rejected_upload

    with caplog.at_level(logging.WARNING, logger="app.services.deployment_input"):
        log_rejected_upload("u-1", exc)

    assert caplog.records == []


# ----------------------------------------------------------------
# What the API shows of a stored userInputVar
# ----------------------------------------------------------------


@pytest.mark.unit
def test_a_stored_input_that_is_not_a_mapping_is_shown_as_it_is():
    from app.services.deployment_input import strip_file_vars_from_user_input

    assert strip_file_vars_from_user_input(None) is None
    assert strip_file_vars_from_user_input("legacy") == "legacy"
    assert strip_file_vars_from_user_input({"terraform": "broken", "note": 1}) == {
        "terraform": "broken",
        "note": 1,
    }


@pytest.mark.unit
def test_a_stored_input_that_is_not_valid_json_is_shown_as_nothing_not_as_a_500():
    from app.services.deployment_input import parse_and_strip_user_input

    assert parse_and_strip_user_input(None) is None
    assert parse_and_strip_user_input("") is None
    assert parse_and_strip_user_input("{not json") is None
    assert parse_and_strip_user_input('{"terraform": {"flavor": "m1.small"}}') == {
        "terraform": {"flavor": "m1.small"}
    }
