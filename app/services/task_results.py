"""Read a task's Terraform outputs and state.

Since .github#5 the worker writes them sealed with the shared Fernet key
into ``tasks.outputs_enc`` (``task_contract.seal_results``); tasks that ran
before keep them as plaintext JSON in ``tasks.outputs`` / ``tasks.tf_state``.
Every reader goes through here so both kinds of rows look the same.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from cryptography.fernet import InvalidToken

from app.models import Task
from app.task_contract import open_results
from app.utils.crypto import results_cipher

logger = logging.getLogger(__name__)


def _sealed(task: Task) -> dict[str, Any] | None:
    token = getattr(task, "outputs_enc", None)
    if not token:
        return None
    try:
        return open_results(results_cipher, token)
    except (InvalidToken, ValueError) as e:
        # A key rotation without re-encryption, or a corrupt row: treat the
        # results as missing rather than failing the whole request.
        logger.error("Cannot open the results of task %s: %s", task.taskId, type(e).__name__)
        return None


def has_results(task: Task) -> bool:
    """Whether the task carries outputs or state in either form."""
    return bool(getattr(task, "outputs_enc", None) or task.outputs or task.tf_state)


def outputs(task: Task) -> dict[str, Any] | None:
    """Parsed Terraform outputs of the task, or None."""
    sealed = _sealed(task)
    if sealed is not None:
        value = sealed.get("terraform_outputs")
        return value if isinstance(value, dict) else None
    if not task.outputs:
        return None
    try:
        value = json.loads(task.outputs) if isinstance(task.outputs, str) else task.outputs
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def tf_state(task: Task) -> str | None:
    """The Terraform state JSON text of the task, or None."""
    sealed = _sealed(task)
    if sealed is not None:
        value = sealed.get("tf_state")
        if value is None or isinstance(value, str):
            return value
        return json.dumps(value)
    return task.tf_state


# Outputs that hold credentials whatever an app declares: the owner view
# never shows another person's account (.github#7 E).
ALWAYS_REDACTED_OUTPUTS = frozenset({"user_accounts"})


def redact_sensitive_outputs(outputs: dict[str, Any] | None) -> dict[str, Any] | None:
    """``outputs`` with the value of every sensitive output removed.

    For views that show a deployment's outputs to people other than the one
    each secret belongs to (the owner view). An output counts as sensitive
    if the app marks it so or it is ``user_accounts``; its entry keeps its
    other fields, gets ``value: None`` and ``redacted: True``. Members get
    their own entry through ``my-access`` instead.
    """
    if outputs is None:
        return None
    redacted: dict[str, Any] = {}
    for name, out in outputs.items():
        if isinstance(out, dict) and (out.get("sensitive") or name in ALWAYS_REDACTED_OUTPUTS):
            redacted[name] = {**{k: v for k, v in out.items() if k != "value"}, "value": None, "redacted": True}
        elif name in ALWAYS_REDACTED_OUTPUTS:
            redacted[name] = {"value": None, "redacted": True}
        else:
            redacted[name] = out
    return redacted
