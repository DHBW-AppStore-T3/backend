"""Result contract, phase 1 of the rename (.github#15): readers accept the legacy and the neutral names.

The same checks live in worker/tests/test_result_contract.py. Writers still use the legacy names
(``terraform_outputs`` / ``tf_state``, ``userInputVar.terraform``, the ``team_vms`` output), so a
reader of the previous release opens everything written now.
"""

import json

import pytest
from cryptography.fernet import Fernet

from app.services import access
from app.services.deployment_input import strip_file_vars_from_user_input
from app.task_contract import (
    open_results,
    result_outputs,
    result_state,
    seal_results,
    team_endpoints_output,
    user_input_variables,
)

pytestmark = pytest.mark.unit

OUT = {"ip": {"value": "::1"}}


def _seal_raw(cipher, document):
    return cipher.encrypt(json.dumps(document).encode())


def test_seal_writes_legacy_names_only():
    cipher = Fernet(Fernet.generate_key())
    raw = json.loads(cipher.decrypt(seal_results(cipher, terraform_outputs=OUT, tf_state="s")))
    assert raw == {"terraform_outputs": OUT, "tf_state": "s"}


@pytest.mark.parametrize(
    "document",
    [
        {"terraform_outputs": OUT, "tf_state": "s"},
        {"outputs": OUT, "state": "s"},
        {"outputs": OUT, "terraform_outputs": {"stale": 1}, "state": "s", "tf_state": "old"},  # new name wins
    ],
)
def test_open_results_reads_both_name_generations(document):
    cipher = Fernet(Fernet.generate_key())
    opened = open_results(cipher, _seal_raw(cipher, document))
    assert opened == {"terraform_outputs": OUT, "tf_state": "s", "outputs": OUT, "state": "s"}


def test_open_results_of_an_empty_document_has_all_keys():
    cipher = Fernet(Fernet.generate_key())
    assert open_results(cipher, _seal_raw(cipher, {})) == {
        "terraform_outputs": None, "tf_state": None, "outputs": None, "state": None,
    }


def test_an_explicit_null_under_the_new_name_is_not_replaced_by_the_legacy_value():
    assert result_outputs({"outputs": None, "terraform_outputs": OUT}) is None
    assert result_state({"tf_state": "s"}) == "s"


def test_user_input_variables_reads_either_block():
    assert user_input_variables({"terraform": {"a": 1}}) == {"a": 1}
    assert user_input_variables({"variables": {"b": 2}}) == {"b": 2}
    assert user_input_variables({"variables": {"b": 2}, "terraform": {"a": 1}}) == {"b": 2}
    assert user_input_variables({"variables": None, "terraform": {"a": 1}}) == {"a": 1}
    assert user_input_variables(None) == {} and user_input_variables({}) == {}


def test_team_output_is_read_under_either_name():
    teams = {"Team-1": {"url": "https://x"}}
    assert team_endpoints_output({"team_vms": {"value": teams}}) == teams
    assert team_endpoints_output({"team_endpoints": {"value": teams}}) == teams
    assert team_endpoints_output({"team_endpoints": {"value": None}, "team_vms": {"value": teams}}) == teams
    assert team_endpoints_output(None) == {}
    assert access.team_vms({"team_endpoints": {"value": teams}}) == teams


@pytest.mark.parametrize("key", ["terraform", "variables"])
def test_uploaded_bytes_are_stripped_from_either_block(key):
    stored = {key: {"f": {"all": {"name": "a.pdf", "size": 3, "content_b64": "QUJD", "mac": "m"}}, "x": "y"}}
    out = strip_file_vars_from_user_input(stored)
    assert "content_b64" not in json.dumps(out)
    assert out[key]["x"] == "y" and out[key]["f"]["all"]["name"] == "a.pdf"
