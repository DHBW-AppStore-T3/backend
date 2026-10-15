"""Access data of one member, matched exactly (.github#7 H, security review H-3).

``GET /deployments/{id}/my-access`` is the only way a member gets the
credentials of a deployment (the access mails are gone, .github#7 I). It
returns the member's own account entry from the outputs of the latest
successful deploy, and nothing else.

The old matcher compared every account entry against a candidate set that
included the entry's own ``username``, so the first account of a team
matched everybody in it: Bob and every other member received Alice's
password. Matching is exact now, never against the entry's own fields:

1. An entry that names its owner is matched on that alone: ``user_id``
   (pod apps write it) or ``email`` (the Windows app writes it), compared
   case-insensitively; an entry carrying ``team`` must name the caller's
   team.
2. Templates that name no owner key their accounts ``<team>-<local part
   with "." replaced by "-">`` (Online-IDE, template-app, pgAdmin, ...).
   The caller's key is derived the same way and compared for equality.
   If two members of the team derive the same key, nobody gets it.

No match, or more than one, means no credentials: the response is empty,
never someone else's account.

Output shape (what the app templates emit, ``terraform output -json``)::

    team_vms.value:      {"<team>": {"url" | "code_server_url": ..., ...}}
    user_accounts.value: {"<key>": {"type": "password" | "ssh_key" | "oauth"
                                    | "none", "username", "auth", "ip",
                                    "port", optional "authtype" ("rdp" |
                                    "ssh"), optional "email", "user_id",
                                    "team", "url"}}

``type`` and ``authtype`` stay the contract with the apps.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.crud import deployments as crud_deployments
from app.models import User


def _output_value(outputs: dict[str, Any] | None, key: str) -> Any:
    """``outputs[key].value``, tolerating missing keys."""
    if not outputs:
        return None
    bag = outputs.get(key)
    if isinstance(bag, dict):
        return bag.get("value")
    return None


def team_vms(outputs: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """The ``team_vms`` output, or ``{}``."""
    val = _output_value(outputs, "team_vms")
    return val if isinstance(val, dict) else {}


def user_accounts(outputs: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """The ``user_accounts`` output, or ``{}``."""
    val = _output_value(outputs, "user_accounts")
    return val if isinstance(val, dict) else {}


def template_account_key(team_name: str, email: str) -> str:
    """The account key templates without owner fields derive for a member.

    ``"${team}-${replace(split("@", member.email)[0], ".", "-")}"`` in HCL.
    """
    local_part = (email or "").split("@", 1)[0]
    return f"{team_name}-{local_part.replace('.', '-')}"


def _names_owner(entry: dict[str, Any]) -> bool:
    return bool(entry.get("user_id") or entry.get("email"))


def _is_owner(entry: dict[str, Any], team_name: str, user: User) -> bool:
    team = entry.get("team")
    if team is not None and str(team) != team_name:
        return False
    if entry.get("user_id"):
        return str(entry["user_id"]) == str(user.userId)
    return str(entry.get("email", "")).casefold() == (user.email or "").casefold()


def find_account(
    outputs: dict[str, Any] | None,
    team_name: str,
    user: User,
    team_members: list[User],
) -> tuple[str, dict[str, Any]] | None:
    """The one account entry of ``user`` in ``team_name``, as ``(key, entry)``, or None.

    ``team_members`` are all members of the team (``user`` among them);
    they decide whether a derived key is unambiguous.
    """
    accounts = {k: v for k, v in user_accounts(outputs).items() if isinstance(v, dict)}
    named = {k: v for k, v in accounts.items() if _names_owner(v)}
    if named:
        matches = [(k, v) for k, v in named.items() if _is_owner(v, team_name, user)]
        return matches[0] if len(matches) == 1 else None

    key = template_account_key(team_name, user.email)
    clashes = [m for m in team_members if template_account_key(team_name, m.email) == key]
    if len(clashes) != 1 or str(clashes[0].userId) != str(user.userId):
        return None
    entry = accounts.get(key)
    return (key, entry) if entry is not None else None


def get_user_access(db: Session, deployment_id: UUID, user_id: UUID) -> dict[str, Any] | None:
    """The caller's own slice of the outputs, for ``GET /deployments/{id}/my-access``.

    ``{"user_accounts": {"<key>": {...}}, "team_vms": {"<team>": {...}}}``,
    mirroring the raw output shape so the frontend renders it unchanged.
    None when the deployment is missing, the user is in none of its teams,
    there is no successful deploy yet, or no entry belongs to them exactly.
    """
    deployment = crud_deployments.get_deployment_with_details(db, deployment_id)
    if not deployment:
        return None

    for team in deployment.teams or []:
        members = crud_deployments.get_team_members(db, team.teamId)
        member = next((m for m in members if m.userId == user_id), None)
        if member is None:
            continue
        outputs = crud_deployments.get_latest_successful_deploy_outputs(db, deployment_id)
        if outputs is None:
            return None
        match = find_account(outputs, team.name, member, members)
        if match is None:
            return None
        key, entry = match
        result: dict[str, Any] = {"user_accounts": {key: entry}, "team_vms": {}}
        vm = team_vms(outputs).get(team.name)
        if isinstance(vm, dict):
            result["team_vms"] = {team.name: vm}
        return result
    return None
