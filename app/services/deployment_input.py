"""Validation and normalisation of a deployment's ``userInputVar``.

Everything between the wizard's JSON payload and the column that gets
persisted: folding the parallel ``files`` field into
``terraform[var_name]``, enforcing the upload size and extension caps,
checking that ``varScope = team|user`` variables arrive keyed by the
deployment's actual roster, and stripping base64 payloads back out
again on the read paths so list and detail responses stay small.

Extracted from ``app.routers.deployments``, which was 2018 lines. These
functions raise ``HTTPException`` rather than a domain error, which is
the one concession to their origin: the status codes and the structured
``detail`` payloads are part of the wizard's contract, and inventing a
translation layer purely to avoid importing HTTPException here would
add indirection without changing what the client sees.
"""

from __future__ import annotations

import base64
import binascii
import json

from fastapi import HTTPException, status

# Defense-in-depth limits for inline file uploads. The UX-side warning
# is mirrored on the wizard, but a hand-crafted POST could still try
# to push GBs of payload through ``userInputVar``. We refuse before
# the row hits the DB.
#
# Per-file cap matches the existing app-image cap so users don't have
# to learn a second number; deployment-wide cap is 5× that, leaving
# headroom for (e.g.) one big assignment plus several small starter
# files. Both are enforced post-base64-decode so a malicious base64
# blob of right-shape but wrong-size still fails fast.
_MAX_FILE_BYTES_PER_FILE = 2 * 1024 * 1024
_MAX_FILE_BYTES_PER_DEPLOYMENT = 10 * 1024 * 1024


def attach_files_to_user_input(
    user_input_var: dict | None,
    files: dict | None,
    variable_definitions: list[dict] | None = None,
) -> dict:
    """Validate and merge wizard-uploaded files into ``userInputVar``.

    The wizard ships files in a parallel ``files`` field instead of
    nesting them straight into ``userInputVar.terraform`` so the
    request payload's shape is obvious to a reader and so we can
    apply size / encoding validation in one place. Result is a fresh
    dict with the files folded into ``terraform[var_name]`` — the
    worker doesn't need to know they originally came from a separate
    field.

    Validation:
      * each top-level key in ``files`` becomes one terraform variable
      * each inner-map entry is one ``DeploymentFileUpload`` record
      * ``content_b64`` decodes cleanly (RFC 4648, padding optional)
      * decoded size matches the declared ``size`` (within rounding —
        client may have set it before encoding so we accept ±1)
      * per-file cap and total deployment cap
      * if ``variable_definitions`` are provided and a file variable
        declares ``fileExtensions``, each uploaded filename's suffix
        (lowercased, after the last dot) must be in the allowed list.
        Defense-in-depth: the wizard's ``accept`` attribute already
        filters in the picker, but a hand-crafted POST could bypass it.

    Raises ``HTTPException(413)`` for size violations and
    ``HTTPException(422)`` for malformed payload — Pydantic already
    rejected the obvious cases (missing fields, wrong types) before
    we get here, so we only catch what gets past it.
    """
    base = dict(user_input_var or {})
    base.setdefault("terraform", {})
    base.setdefault("packer", {})

    if not files:
        return base

    # Build an index var_name → allowed_extensions for the extension
    # check below. Variables without ``fileExtensions`` skip the
    # filter — keeps backward compatibility for any caller that doesn't
    # supply ``variable_definitions``.
    allowed_exts_by_var: dict[str, list[str]] = {}
    scoped_file_vars: set[str] = set()
    if variable_definitions:
        for vdef in variable_definitions:
            exts = vdef.get("fileExtensions")
            if exts:
                allowed_exts_by_var[vdef["name"]] = [e.lower() for e in exts]
            if vdef.get("varScope") in ("team", "user"):
                scoped_file_vars.add(vdef["name"])

    total_bytes = 0
    terraform_block = dict(base.get("terraform") or {})

    for var_name, slot_map in files.items():
        if var_name in terraform_block:
            # Wizard already routed something into this variable — a
            # collision means the frontend filled both the variables
            # picker AND the file uploader for the same name. That's an
            # unrecoverable contract violation; surface it clearly.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "reason": "file_var_collision",
                    "variable": var_name,
                },
            )
        if not isinstance(slot_map, dict) or not slot_map:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"reason": "file_var_empty", "variable": var_name},
            )

        encoded_slots: dict[str, dict] = {}
        for slot_key, upload in slot_map.items():
            # ``upload`` arrives here as a Pydantic model instance
            # already (FastAPI deserialised the request body into
            # ``DeploymentCreate``) — pull fields off attributes.
            content_b64 = upload.content_b64

            # Extension-filter check — only when the app author declared
            # an ``@openstack:file:<scope>:<exts>`` filter. We compare
            # the filename suffix (after the last dot, lowercased) to
            # the allowed list. Missing dot or unknown suffix → 422.
            allowed_exts = allowed_exts_by_var.get(var_name)
            if allowed_exts is not None:
                name = upload.name or ""
                dot = name.rfind(".")
                suffix = name[dot + 1 :].lower() if dot >= 0 else ""
                if suffix not in allowed_exts:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail={
                            "reason": "file_extension_rejected",
                            "variable": var_name,
                            "slot": slot_key,
                            "filename": upload.name,
                            "allowed": allowed_exts,
                        },
                    )
            try:
                # ``validate=True`` would reject any non-base64
                # whitespace; the wizard sends compact base64 so this
                # is fine.
                decoded = base64.b64decode(content_b64, validate=True)
            except (binascii.Error, ValueError) as e:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "reason": "file_b64_invalid",
                        "variable": var_name,
                        "slot": slot_key,
                        "error": str(e),
                    },
                )

            if abs(len(decoded) - upload.size) > 1:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "reason": "file_size_mismatch",
                        "variable": var_name,
                        "slot": slot_key,
                        "declared": upload.size,
                        "actual": len(decoded),
                    },
                )

            if len(decoded) > _MAX_FILE_BYTES_PER_FILE:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail={
                        "reason": "file_too_large",
                        "variable": var_name,
                        "slot": slot_key,
                        "limit_bytes": _MAX_FILE_BYTES_PER_FILE,
                        "actual_bytes": len(decoded),
                    },
                )
            total_bytes += len(decoded)
            if total_bytes > _MAX_FILE_BYTES_PER_DEPLOYMENT:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail={
                        "reason": "deployment_files_too_large",
                        "limit_bytes": _MAX_FILE_BYTES_PER_DEPLOYMENT,
                    },
                )

            encoded_slots[slot_key] = {
                "name": upload.name,
                "content_b64": content_b64,
                "size": upload.size,
                "content_type": upload.content_type or "application/octet-stream",
            }
        # scope=team|user: HCL type is map(map(object({...}))) —
        # outer key is the team/user slot, inner key is the upload slot.
        # scope=all: HCL type is map(object({...})) — flat map.
        if var_name in scoped_file_vars:
            terraform_block[var_name] = {
                slot_key: {"uploaded": file_obj}
                for slot_key, file_obj in encoded_slots.items()
            }
        else:
            terraform_block[var_name] = encoded_slots

    base["terraform"] = terraform_block
    return base


def validate_scoped_user_input(
    user_input_var: dict | None,
    variable_definitions: list[dict],
    teams_payload: list,
) -> None:
    """Enforce that variables marked with ``varScope = team|user``
    arrive as a map whose keys match the deployment's team / user roster.

    Reasoning: the wizard packs scoped variables as a Map
    (``{slot_key: value, ...}``) and ships them via ``userInputVar``.
    A hand-crafted POST could ship arbitrary keys; we want unknown
    Scope-Targets to fail fast and loud before they hit Terraform,
    where the error would be a confusing "module: invalid for_each
    key" deep in the worker log.

    File variables are NOT skipped here — they share the same scoped
    map shape (``{slot_key: file_obj}``) and a hand-crafted POST could
    just as easily smuggle an unknown team name into a file-scope
    variable. We validate slot identity against the same roster; the
    per-file size / base64 / extension validation stays in
    :func:`attach_files_to_user_input` because that's the layer that
    actually decodes the bytes.

    Raises ``HTTPException(422)`` with ``reason="unknown_scope_target"``,
    ``reason="scoped_var_not_map"``, or ``reason="required_slot_empty"``
    for shape/identity/completeness problems.
    """
    if not user_input_var:
        return

    # Compose the universe of valid slot keys per scope. ``team``
    # accepts any team name; ``user`` accepts ``TeamName-Username``
    # composites — mirror of ``userSlotKey`` in the wizard.
    team_names: set[str] = set()
    for team in teams_payload or []:
        team_name = getattr(team, "name", None) or (team.get("name") if isinstance(team, dict) else None)
        if not team_name:
            continue
        team_names.add(team_name)
        # ``team.userIds`` contains UUID strings here, not usernames —
        # the deployment endpoint resolves usernames just below us
        # when assembling ``teams_dict``. We accept any non-empty
        # composite key prefix-matching ``f"{team_name}-"`` for
        # user-scoped variables, because the wizard renders one slot
        # per member and labels it with the username (not the UUID).
        # A stricter check would require an extra DB round-trip; the
        # prefix-and-non-empty check is enough to catch typos and
        # cross-team key smuggling.

    # Longest-prefix-match helper for user-scope composite keys:
    # ``TeamName-Username``. A naive ``slot_key.find('-')`` would
    # truncate a team named ``Team-A`` to just ``Team``, so any team
    # name containing a dash would be misclassified as unknown. We
    # iterate the known team names from longest to shortest and pick
    # the first one that either equals ``slot_key`` (empty username,
    # rejected below) or prefixes it as ``f"{team}-"``.
    teams_by_length = sorted(team_names, key=len, reverse=True)

    def _user_slot_team_prefix(slot_key: str) -> str | None:
        for team in teams_by_length:
            if slot_key == team:
                # No trailing ``-Username`` — caller treats this as a
                # missing-user-segment and surfaces ``unknown_scope_target``.
                return team
            if slot_key.startswith(team + "-"):
                return team
        return None

    def _is_empty_slot_value(val) -> bool:
        """Treat None, empty string, empty list, and empty dict as
        "slot not filled". The wizard would otherwise let a required
        team/user-scoped var slip through with one team left blank,
        which Terraform would catch with a much less actionable
        ``Inappropriate value for attribute`` deep in the worker log.
        """
        if val is None:
            return True
        if isinstance(val, str) and val == "":
            return True
        return isinstance(val, (list, dict)) and len(val) == 0

    for source_key in ("terraform", "packer"):
        block = user_input_var.get(source_key)
        if not isinstance(block, dict):
            continue
        for vdef in variable_definitions:
            if vdef.get("source") != source_key:
                continue
            scope = vdef.get("varScope")
            if scope not in ("team", "user"):
                continue
            var_name = vdef["name"]
            value = block.get(var_name)
            is_file = vdef.get("osType") == "file"
            required = bool(vdef.get("required"))
            if value is None:
                # File-scope vars MUST be present — the wizard always
                # ships at least an empty map for them, so a None here
                # is a hand-crafted-POST shape. For non-file required
                # scoped vars, raise on the slot-completeness check
                # below by treating the absent value as an empty map.
                if required:
                    value = {}
                else:
                    continue  # variable left at HCL default — allowed
            if not isinstance(value, dict):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={
                        "reason": "scoped_var_not_map",
                        "variable": var_name,
                        "scope": scope,
                    },
                )
            for slot_key in value:
                if scope == "team":
                    if slot_key not in team_names:
                        raise HTTPException(
                            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={
                                "reason": "unknown_scope_target",
                                "variable": var_name,
                                "scope": scope,
                                "slot": slot_key,
                                "allowed": sorted(team_names),
                            },
                        )
                else:  # user scope
                    # Longest-prefix-match against known team names so
                    # a team named ``Team-A`` parses to prefix
                    # ``Team-A`` and rest ``Username`` instead of
                    # prefix ``Team`` (which wouldn't be a known team).
                    prefix = _user_slot_team_prefix(slot_key)
                    if prefix is None or slot_key == prefix:
                        raise HTTPException(
                            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={
                                "reason": "unknown_scope_target",
                                "variable": var_name,
                                "scope": scope,
                                "slot": slot_key,
                                "hint": "expected ``TeamName-Username``",
                            },
                        )

            # Required slot-completeness check: for required team /
            # user scoped variables every expected slot key must carry
            # a non-empty value. Without this an empty map (or one
            # team left blank) would silently pass here and only fail
            # downstream with an opaque Terraform error.
            #
            # File vars are skipped from the completeness sweep — the
            # per-file size/decode validation in
            # :func:`attach_files_to_user_input` raises a more specific
            # error (file_var_empty / file_b64_invalid) for them. We
            # only checked slot identity above; the bytes themselves
            # are validated at that layer.
            if required and not is_file:
                expected_slots: set[str] = set()
                if scope == "team":
                    expected_slots = set(team_names)
                # For ``user`` scope we don't have the per-team member
                # roster here (would need a DB round-trip we already
                # avoid above), so we only enforce that each slot the
                # caller did ship carries a non-empty value. The
                # wizard's frontend check is the primary guard; this
                # is defense-in-depth against hand-crafted POSTs that
                # ship one half-filled team. A POST that omits a team
                # entirely for a required user-scope var is caught by
                # the team-scope branch via team_names because the
                # wizard always emits at least one slot per team.

                missing: list[str] = []
                for slot in expected_slots:
                    if _is_empty_slot_value(value.get(slot)):
                        missing.append(slot)
                # Also flag empty values among slots the caller did
                # provide — covers user-scope and any partial-fill case.
                for slot, val in value.items():
                    if _is_empty_slot_value(val) and slot not in missing:
                        missing.append(slot)
                if missing:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail={
                            "reason": "required_slot_empty",
                            "variable": var_name,
                            "scope": scope,
                            "missing_slots": sorted(missing),
                        },
                    )


def strip_file_vars_from_user_input(user_input_var: dict | None) -> dict | None:
    """Strip per-file ``content_b64`` payloads from a userInputVar dict.

    Used by the deployment detail responses so the JSON the frontend
    receives only carries metadata (name/size/content_type) — the
    decoded bytes can be many MBs each and shipping them on every
    page render is wasteful. Owners who actually want the file fetch
    it via the dedicated download endpoint.

    Heuristic-based: a variable is a file slot when its value is a
    mapping whose entries each carry a ``content_b64`` field — the
    same shape ``attach_files_to_user_input`` writes. We match on
    that key because no other user-input kind uses it.
    """
    if not isinstance(user_input_var, dict):
        return user_input_var

    out = {k: v for k, v in user_input_var.items() if k != "terraform"}
    tf_block = user_input_var.get("terraform")
    if not isinstance(tf_block, dict):
        if "terraform" in user_input_var:
            out["terraform"] = tf_block
        return out

    stripped_tf: dict = {}
    for var_name, value in tf_block.items():
        if looks_like_file_var(value):
            stripped_tf[var_name] = _file_var_metadata_only(value)
        else:
            stripped_tf[var_name] = value
    out["terraform"] = stripped_tf
    return out


def parse_and_strip_user_input(raw: str | None) -> dict | None:
    """Parse a stored ``userInputVar`` JSON string and strip file bytes.

    Wraps the ``json.loads`` → :func:`strip_file_vars_from_user_input`
    chain (with a malformed-JSON guard) shared by the list, detail, and
    create responses so all three surface the same file-stripped shape.
    Returns ``None`` for an empty or unparseable value.
    """
    if not raw:
        return None
    try:
        return strip_file_vars_from_user_input(json.loads(raw))
    except json.JSONDecodeError:
        return None


def looks_like_file_var(value) -> bool:
    """True if ``value`` matches the file-upload shape produced by
    :func:`attach_files_to_user_input`: a non-empty mapping whose
    values are objects carrying ``content_b64`` plus the metadata
    triplet. Used at response-shaping time to identify file-typed
    variables without consulting the app's variable schema, AND at
    lifecycle-dispatch time (destroy/pause/resume/redeploy) to drop
    file vars from the worker's var-set so Terraform's schema
    validation doesn't trip on a payload it doesn't need.

    Shape examples it matches (and only these):

    * ``scope=all``   → ``{"all": {name, content_b64, size, content_type}}``
    * ``scope=team``  → ``{"Team-1": {...}, "Team-2": {...}}``
    * ``scope=user``  → ``{"Team-1-luca": {...}, ...}``

    Strict signature: each slot must carry ``content_b64``. Rows
    that survived an earlier response-side-strip-then-persisted
    accident (metadata triplet only, no bytes) are NOT auto-
    detected — clean them up by hand (delete the deployment row +
    its pg-backend tfstate schema). The strictness is intentional:
    a lenient detector would silently swallow legitimate non-file
    map variables that coincidentally share the metadata key names.
    """
    if not isinstance(value, dict) or not value:
        return False
    for slot in value.values():
        if not isinstance(slot, dict):
            return False
        if "content_b64" not in slot:
            return False
    return True


def _file_var_metadata_only(value: dict) -> dict:
    """Return a copy of a file-shape variable with the ``content_b64``
    payload stripped. Metadata fields (name, size, content_type)
    survive so the UI can list "what was uploaded" without shipping
    base64 megabytes on every detail-view render.
    """
    out: dict = {}
    for slot_key, slot in value.items():
        out[slot_key] = {k: v for k, v in slot.items() if k != "content_b64"}
    return out
