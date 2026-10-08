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
from app.utils.crypto import cipher

logger = logging.getLogger(__name__)


def _sealed(task: Task) -> dict[str, Any] | None:
    token = getattr(task, "outputs_enc", None)
    if not token:
        return None
    try:
        return open_results(cipher, token)
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


def outputs_json(task: Task) -> str | None:
    """Outputs as the JSON text the task API returns (``TaskResponse.outputs``)."""
    sealed = _sealed(task)
    if sealed is not None:
        value = sealed.get("terraform_outputs")
        return json.dumps(value) if value else None
    return task.outputs


def tf_state(task: Task) -> str | None:
    """The Terraform state JSON text of the task, or None."""
    sealed = _sealed(task)
    if sealed is not None:
        value = sealed.get("tf_state")
        if value is None or isinstance(value, str):
            return value
        return json.dumps(value)
    return task.tf_state
