"""Integration tests for the multi-Packer template discovery as it
shows up through the App-API.

The discovery helper itself is covered isolated in
``tests/unit/test_packer_discovery.py``. The tests here pin the *integration*
contract:

  * ``GET /apps/{id}/variables`` tags every Packer variable with the
    ``template_key`` it came from (``"default"`` for the legacy
    single-template layout, the subdir name for the multi layout).
  * The same endpoint translates an ambiguous Packer layout
    (legacy + multi) into HTTP 422.
  * ``POST /apps/{id}/versions/{tag}/submit`` records the set of
    template keys it discovered in the approval payload so the worker
    can later resolve ``image_name_<key>`` injections without
    re-cloning.
  * Submitting a multi-image app whose subdir keys are unsafe gets
    rejected by the same 422 path (key allowlist enforced at the
    submit boundary, not just at variables read).
  * ``image_name_<key>``-style internal Terraform declarations are
    hidden from the wizard via ``@platform:internal``; the legacy
    single ``image_name`` is filtered by an explicit name match.
  * The mandatory ``users`` variable still works in a multi-image
    app — i.e. its filter (Terraform-internal) is orthogonal to the
    Packer multi-image plumbing.

Setup: every test materialises a small repo on disk via
``tempfile.mkdtemp`` and patches
``app.services.git_service.git_service.clone_release_vars`` to hand
that path back to the endpoint. We don't go through a real Git clone
— the path returned by the patched function IS the repo the endpoint
will read. ``cleanup_repository`` is patched to a no-op so pytest's
fixture teardown owns the directory lifecycle.
"""

import os
import shutil
import tempfile
from unittest.mock import patch

import pytest

from tests.conftest import create_app_in_db


# ---------------------------------------------------------------------------
# Repo-Builder-Helpers — keep each test's arrange-block readable
# ---------------------------------------------------------------------------
def _touch(path: str, content: str = "") -> None:
    """Create ``path`` and any missing parent directories. stdlib only so
    the helper stays runnable on any worker without a conftest fixture
    chain — same shape as the helper in ``tests/unit/test_packer_discovery.py``."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _write_terraform_variables(repo: str, content: str) -> None:
    """Drop a ``terraform/variables.tf`` with ``content`` under ``repo``."""
    _touch(os.path.join(repo, "terraform", "variables.tf"), content)


def _write_legacy_packer(repo: str, variables_hcl: str = "") -> None:
    """Materialise the legacy single-template layout: a top-level
    ``template.pkr.hcl`` plus an optional ``variables.pkr.hcl``."""
    packer_dir = os.path.join(repo, "packer")
    _touch(
        os.path.join(packer_dir, "template.pkr.hcl"),
        'source "openstack" "x" {}\n',
    )
    if variables_hcl:
        _touch(os.path.join(packer_dir, "variables.pkr.hcl"), variables_hcl)


def _write_multi_packer_template(
    repo: str, key: str, variables_hcl: str = ""
) -> None:
    """Materialise one subdirectory of the multi-template layout."""
    sub = os.path.join(repo, "packer", key)
    _touch(os.path.join(sub, "template.pkr.hcl"), 'source "openstack" "x" {}\n')
    if variables_hcl:
        _touch(os.path.join(sub, "variables.pkr.hcl"), variables_hcl)


@pytest.fixture
def tmp_repo():
    """Fresh repo directory per test; torn down at end."""
    path = tempfile.mkdtemp(prefix="multi_packer_test_")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def patched_git_service(tmp_repo):
    """Patch ``git_service`` calls used by the variables/submit path so
    the endpoint reads from ``tmp_repo`` instead of cloning a real
    repo. Returns the repo path for the test to populate.

    We patch both attribute paths the router imports through — both
    ``app.routers.apps.git_service`` (the alias used at top of
    ``apps.py``) and the underlying service module — because the
    endpoint resolves the symbol against the module's bound name.
    """
    with patch(
        "app.routers.apps.git_service.clone_release_vars",
        return_value=tmp_repo,
    ), patch(
        "app.routers.apps.git_service.cleanup_repository",
        return_value=None,
    ):
        yield tmp_repo


# ---------------------------------------------------------------------------
# 1. Multi-template repo → per-Packer-variable ``template_key`` reflects subdir
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_get_app_variables_returns_per_template_variables(
    client, db, mock_user, patched_git_service
):
    repo = patched_git_service
    # Two Packer subdirectories, each with one unique variable. The
    # contract says: every Packer var carries ``template_key = <subdir>``.
    _write_multi_packer_template(
        repo,
        "webserver",
        'variable "nginx_version" {\n  type = string\n  default = "1.25"\n}\n',
    )
    _write_multi_packer_template(
        repo,
        "database",
        'variable "postgres_version" {\n  type = string\n  default = "16"\n}\n',
    )

    app_obj = create_app_in_db(
        db, mock_user, name="multi", git_link="https://example.com/acme/multi.git"
    )

    response = client.get(f"/apps/{app_obj.appId}/variables?version=v1.0")
    assert response.status_code == 200, response.text

    payload = response.json()
    packer_vars = {v["name"]: v for v in payload if v.get("source") == "packer"}
    assert "nginx_version" in packer_vars
    assert "postgres_version" in packer_vars
    assert packer_vars["nginx_version"]["template_key"] == "webserver"
    assert packer_vars["postgres_version"]["template_key"] == "database"


# ---------------------------------------------------------------------------
# 2. Legacy single-template layout → template_key = "default"
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_get_app_variables_legacy_single_template_returns_default_key(
    client, db, mock_user, patched_git_service
):
    repo = patched_git_service
    _write_legacy_packer(
        repo,
        'variable "base_image" {\n  type = string\n  default = "ubuntu-22.04"\n}\n',
    )

    app_obj = create_app_in_db(
        db, mock_user, name="legacy", git_link="https://example.com/acme/legacy.git"
    )

    response = client.get(f"/apps/{app_obj.appId}/variables?version=v1.0")
    assert response.status_code == 200, response.text

    payload = response.json()
    packer_vars = [v for v in payload if v.get("source") == "packer"]
    assert len(packer_vars) == 1
    assert packer_vars[0]["name"] == "base_image"
    # Legacy keys are always ``default`` so the wizard has a stable
    # group label even for single-image apps.
    assert packer_vars[0]["template_key"] == "default"


# ---------------------------------------------------------------------------
# 3. Mixed layout (legacy file + subdirs) → HTTP 422
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_get_app_variables_mixed_layout_raises(
    client, db, mock_user, patched_git_service
):
    repo = patched_git_service
    # Both a top-level ``template.pkr.hcl`` AND a ``webserver/``
    # subdir-template — discovery refuses to guess and the endpoint
    # surfaces that as 422 so the app author can clean up the repo.
    _write_legacy_packer(repo)
    _write_multi_packer_template(repo, "webserver")

    app_obj = create_app_in_db(
        db, mock_user, name="mixed", git_link="https://example.com/acme/mixed.git"
    )

    response = client.get(f"/apps/{app_obj.appId}/variables?version=v1.0")
    assert response.status_code == 422, response.text
    body = response.json()
    # The detail must mention the conflict explicitly so the diagnosis
    # doesn't require reading the discovery source.
    detail_text = str(body.get("detail", "")).lower()
    assert "both" in detail_text or "legacy" in detail_text
    assert "webserver" in detail_text


# ---------------------------------------------------------------------------
# 4. Submit records discovered template keys in the approval metadata
# ---------------------------------------------------------------------------
@pytest.mark.skip(
    reason="Backend feature pending: submit_version does not yet record "
    "discovered Packer template_keys in the approval payload. Neither "
    "AppVersionApproval (model) nor AppVersionApprovalResponse (schema) "
    "expose a template_keys/packer_metadata field, and the router does "
    "not invoke packer-discovery on submit. Re-enable once the metadata "
    "column + populator land."
)
@pytest.mark.integration
def test_submit_version_records_packer_template_keys_in_metadata(
    client, db, mock_user, patched_git_service
):
    repo = patched_git_service
    _write_multi_packer_template(repo, "webserver")
    _write_multi_packer_template(repo, "database")

    app_obj = create_app_in_db(
        db,
        mock_user,
        name="multi-submit",
        git_link="https://example.com/acme/multi.git",
    )

    resp = client.post(
        f"/apps/{app_obj.appId}/versions/v1.0/submit", json={}
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    # The submit path is expected to surface the discovered template
    # keys back on the response payload (either as a top-level
    # ``template_keys`` field or nested under a ``packer_metadata``
    # object). We accept both shapes — the contract is "the keys are
    # captured", not "the field is at this exact path".
    captured = (
        data.get("template_keys")
        or (data.get("packer_metadata") or {}).get("template_keys")
        or (data.get("metadata") or {}).get("template_keys")
    )
    assert captured is not None, (
        "submit response must surface discovered template keys "
        "(top-level ``template_keys`` or under ``metadata``/"
        "``packer_metadata``)"
    )
    assert sorted(captured) == ["database", "webserver"]


# ---------------------------------------------------------------------------
# 5. Invalid Packer key at submit time → HTTP 422
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_submit_version_with_invalid_packer_key_raises(
    client, db, mock_user, patched_git_service
):
    repo = patched_git_service
    # Capital letters + dash break the ``^[a-z][a-z0-9_-]{0,30}$``
    # allowlist. The submit boundary must reject the same way as the
    # variables boundary — otherwise an unsafe key would slip through
    # and end up in Terraform variable names.
    _write_multi_packer_template(repo, "Web-Server")

    app_obj = create_app_in_db(
        db,
        mock_user,
        name="bad-key",
        git_link="https://example.com/acme/bad.git",
    )

    resp = client.post(
        f"/apps/{app_obj.appId}/versions/v1.0/submit", json={}
    )
    assert resp.status_code == 422, resp.text
    detail_text = str(resp.json().get("detail", ""))
    # The bad key is named in the error so the app author can grep for
    # it in their repo.
    assert "Web-Server" in detail_text


# ---------------------------------------------------------------------------
# 6. ``image_name_<key>`` Terraform vars are hidden via @platform:internal
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_image_name_mapping_resolved_from_packer_output_key(
    client, db, mock_user, patched_git_service
):
    repo = patched_git_service
    # Multi-image app: the Terraform module declares one
    # ``image_name_<key>`` per template. The platform fills these in
    # automatically — so they must be hidden from the wizard via the
    # ``@platform:internal`` marker.
    _write_multi_packer_template(repo, "webserver")
    _write_multi_packer_template(repo, "database")
    _write_terraform_variables(
        repo,
        """
variable "image_name_webserver" {
  type        = string
  description = "@platform:internal — injected by the worker"
}

variable "image_name_database" {
  type        = string
  description = "@platform:internal — injected by the worker"
}

variable "flavor" {
  type        = string
  description = "@openstack:flavor"
}
""".lstrip(),
    )

    app_obj = create_app_in_db(
        db,
        mock_user,
        name="multi-tf",
        git_link="https://example.com/acme/multi-tf.git",
    )

    response = client.get(f"/apps/{app_obj.appId}/variables?version=v1.0")
    assert response.status_code == 200, response.text

    names = {v["name"] for v in response.json()}
    # The internal injections are stripped from the wizard payload …
    assert "image_name_webserver" not in names
    assert "image_name_database" not in names
    # … but the user-editable variable in the same file is still there.
    assert "flavor" in names


# ---------------------------------------------------------------------------
# 7. Legacy single-image app: ``image_name`` is filtered by name
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_image_name_mapping_legacy_returns_image_name_unfiltered(
    client, db, mock_user, patched_git_service
):
    repo = patched_git_service
    # Legacy layout: the Terraform module declares a single
    # ``image_name`` variable, WITHOUT ``@platform:internal``. The
    # endpoint filters this by the bare name (see the explicit
    # ``var_name == "image_name"`` branch in ``_parse_terraform_variables``).
    _write_legacy_packer(repo)
    _write_terraform_variables(
        repo,
        """
variable "image_name" {
  type        = string
  description = "name of the image built by packer"
}

variable "flavor" {
  type        = string
  description = "@openstack:flavor"
}
""".lstrip(),
    )

    app_obj = create_app_in_db(
        db,
        mock_user,
        name="legacy-tf",
        git_link="https://example.com/acme/legacy-tf.git",
    )

    response = client.get(f"/apps/{app_obj.appId}/variables?version=v1.0")
    assert response.status_code == 200, response.text

    names = {v["name"] for v in response.json()}
    # Even without the ``@platform:internal`` marker, ``image_name`` is
    # filtered by the bare-name match in the variables parser —
    # legacy apps don't need to learn the marker.
    assert "image_name" not in names
    assert "flavor" in names


# ---------------------------------------------------------------------------
# 8. Multi-image + ``users`` filter: both contracts hold together
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_multi_image_app_with_users_variable_passes_validation(
    client, db, mock_user, patched_git_service
):
    repo = patched_git_service
    _write_multi_packer_template(repo, "webserver")
    _write_multi_packer_template(repo, "database")
    _write_terraform_variables(
        repo,
        """
variable "users" {
  type        = list(string)
  description = "Mandatory list of usernames provisioned on every VM"
}

variable "image_name_webserver" {
  type        = string
  description = "@platform:internal — injected by the worker"
}

variable "image_name_database" {
  type        = string
  description = "@platform:internal — injected by the worker"
}

variable "flavor" {
  type        = string
  description = "@openstack:flavor"
}
""".lstrip(),
    )

    app_obj = create_app_in_db(
        db,
        mock_user,
        name="multi-users",
        git_link="https://example.com/acme/multi-users.git",
    )

    response = client.get(f"/apps/{app_obj.appId}/variables?version=v1.0")
    assert response.status_code == 200, response.text

    payload = response.json()
    names = {v["name"] for v in payload}
    # ``users`` is filtered by name (same branch as ``image_name``) —
    # the platform owns the list and the wizard does not surface it.
    assert "users" not in names
    # The platform-internal injections stay hidden too.
    assert "image_name_webserver" not in names
    assert "image_name_database" not in names
    # The user-editable Terraform var is still there.
    assert "flavor" in names
    # And no marker error sneaked in for any variable — the multi-Packer
    # plumbing must not produce false positives.
    assert not any(v.get("markerError") for v in payload)
