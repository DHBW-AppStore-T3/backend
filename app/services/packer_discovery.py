"""Discover Packer templates inside a cloned app repository.

Mirrors ``worker/app/services/packer_discovery.py`` deliberately: the
backend needs the layout to render the wizard, the worker needs it to
decide what to build, and the two live in separate repositories, so the
logic is duplicated rather than shared. Keeping both copies as a
stdlib-only module of the same shape makes a drift between them easy to
spot in review - previously the backend's copy was buried in a router
and the worker's was a standalone module, which made them hard to
compare at all.

Stdlib-only on purpose: no FastAPI, no ORM, no web-framework imports.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# ----------------------------------------------------------------
# PACKER TEMPLATE DISCOVERY
# ----------------------------------------------------------------
# Apps may ship Packer templates in one of two layouts:
#
#  1. Legacy single-template layout:
#         packer/template.pkr.hcl
#         packer/variables.pkr.hcl
#     → exactly ONE image, conventionally keyed ``default``. The
#       worker injects ``image_name`` (a single Terraform variable).
#
#  2. Multi-template layout:
#         packer/<key>/template.pkr.hcl
#         packer/<key>/variables.pkr.hcl   (optional)
#     → one image per ``<key>``. The worker injects one
#       ``image_name_<key>`` Terraform variable per template, each
#       marked ``@platform:internal`` in its description so the wizard
#       skips them.
#
# Discovery rules:
#   - No ``packer/`` directory → returns ``[]`` (no Packer phase).
#   - Legacy file present       → returns ``[PackerTemplate("default", ...)]``.
#   - Subdirectories with a
#     ``template.pkr.hcl``      → returns one entry per subdir, sorted.
#   - Both legacy AND subdirs   → ``PackerTemplateDiscoveryError`` (hard).
#   - Subdir without
#     ``template.pkr.hcl``      → ignored (e.g. ``_common/``, ``scripts/``).
#   - Subdir with a key that
#     doesn't match the pattern → ``PackerTemplateDiscoveryError``.
#
# Key pattern is intentionally narrow (``[a-z][a-z0-9_-]{0,30}``) so
# the key is safe to embed in Terraform variable names and image
# tags without quoting.
# ----------------------------------------------------------------

@dataclass
class PackerTemplate:
    """One Packer template discovered under ``<repo>/packer``.

    ``variables_path`` may point at a non-existing file — the caller
    must check ``os.path.isfile`` before reading it. We don't filter
    here because the file is optional and a missing one is not an
    error.
    """

    key: str
    template_path: str
    variables_path: str


_TEMPLATE_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")


class PackerTemplateDiscoveryError(ValueError):
    """Raised when the Packer directory has a layout the platform can't
    reconcile (ambiguous, contradictory, or with an unsafe key).

    Translated to HTTP 422 at the load_variable_definitions boundary
    so the app author sees the error immediately on the first
    ``GET /apps/{id}/variables`` instead of at first deploy.
    """


def discover_packer_templates(repo_path: str) -> list[PackerTemplate]:
    """Walk ``<repo_path>/packer`` and return the list of templates the
    worker will build for this app.

    See the section docstring above for the layout rules. Returns
    ``[]`` for apps without any Packer at all (Terraform-only).
    """
    packer_dir = os.path.join(repo_path, "packer")
    if not os.path.isdir(packer_dir):
        return []

    legacy_template = os.path.join(packer_dir, "template.pkr.hcl")
    has_legacy = os.path.isfile(legacy_template)

    multi_templates: list[PackerTemplate] = []
    bad_keys: list[str] = []
    for entry in sorted(os.listdir(packer_dir)):
        sub = os.path.join(packer_dir, entry)
        if not os.path.isdir(sub):
            continue
        tmpl = os.path.join(sub, "template.pkr.hcl")
        if not os.path.isfile(tmpl):
            # Subdirs without a template (``_common/``, ``scripts/``,
            # ``http/`` for boot-time HTTP servers, ...) are silently
            # ignored — they're tooling, not images to build.
            continue
        if not _TEMPLATE_KEY_RE.match(entry):
            bad_keys.append(entry)
            continue
        multi_templates.append(PackerTemplate(
            key=entry,
            template_path=tmpl,
            variables_path=os.path.join(sub, "variables.pkr.hcl"),
        ))

    if bad_keys:
        raise PackerTemplateDiscoveryError(
            f"Packer template subdirectories with invalid keys "
            f"(must match [a-z][a-z0-9_-]{{0,30}}): {bad_keys}"
        )

    if has_legacy and multi_templates:
        raise PackerTemplateDiscoveryError(
            "App repository has BOTH packer/template.pkr.hcl (legacy "
            "layout) AND packer/<key>/template.pkr.hcl subdirectories "
            f"({[t.key for t in multi_templates]}). Choose one layout "
            "— remove the legacy file or the subdirectories."
        )

    if has_legacy:
        return [PackerTemplate(
            key="default",
            template_path=legacy_template,
            variables_path=os.path.join(packer_dir, "variables.pkr.hcl"),
        )]

    return multi_templates
