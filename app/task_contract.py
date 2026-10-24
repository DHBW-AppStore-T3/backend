"""What the API and the worker agree on about a running task (.github#5).

Events: the worker appends ``task_events`` rows while a job runs; the API
streams them as Server-Sent Events. The API writes a terminal event itself
when it cancels a task or fails one whose worker died. Event types keep the
names of the former Celery events, so the SSE event names and the frontend
stay as they were.

NOTIFY channels: ``task_events`` (trigger, one per event row),
``task_cancel`` (API -> worker, payload: task id) and ``task_released``
(worker -> API, payload: task id, once the worker let go of a task).

Results: the worker seals the OpenTofu/Terraform outputs and state with the
shared Fernet key into ``tasks.outputs_enc``; plaintext results never leave
the worker any more.

This file is identical in ``backend/app/task_contract.py`` and
``worker/app/task_contract.py``; ``tests/unit/test_shared_files.py`` checks
its hash in both repositories.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

EVENT_PROGRESS = "task-progress"
EVENT_LOG = "task-log"
EVENT_SUCCEEDED = "task-succeeded"
EVENT_FAILED = "task-failed"
EVENT_REVOKED = "task-revoked"
TERMINAL_EVENTS = frozenset({EVENT_SUCCEEDED, EVENT_FAILED, EVENT_REVOKED})

# ``failure_kind`` of a task-failed event; frontends treat a missing kind
# as a worker failure.
FAILURE_KIND_WORKER = "worker_failure"
FAILURE_KIND_WORKER_LOST = "worker_lost"
FAILURE_KIND_INFRASTRUCTURE = "celery_infrastructure"
FAILURE_KIND_TIME_LIMIT = "time_limit"

NOTIFY_TASK_EVENTS = "task_events"
NOTIFY_TASK_CANCEL = "task_cancel"
NOTIFY_TASK_RELEASED = "task_released"

# Log events per task; after that one marker event, then the live stream
# carries progress only. The full transcript lands in ``tasks.logs``.
MAX_LOG_EVENTS = 10_000
# Upper bound of one event's JSON; longer messages are cut.
MAX_EVENT_BYTES = 32 * 1024

TASK_STATUS_BY_TERMINAL_EVENT = {
    EVENT_SUCCEEDED: "success",
    EVENT_FAILED: "failed",
    EVENT_REVOKED: "cancelled",
}


class Cipher(Protocol):
    def encrypt(self, data: bytes) -> bytes: ...

    def decrypt(self, token: bytes) -> bytes: ...


def event_payload(event_type: str, *, deployment_id: str, task_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """The JSON of one event row: the fields plus type and ids, cut to ``MAX_EVENT_BYTES``."""
    payload = {**fields, "type": event_type, "deployment_id": str(deployment_id), "task_id": str(task_id)}
    encoded = json.dumps(payload, default=str)
    if len(encoded.encode("utf-8")) > MAX_EVENT_BYTES and isinstance(payload.get("message"), str):
        overflow = len(encoded.encode("utf-8")) - MAX_EVENT_BYTES
        keep = max(len(payload["message"]) - overflow - 64, 0)
        payload["message"] = payload["message"][:keep] + " …[truncated]"
        payload["truncated"] = True
    return payload


def terminal_payload(
    event_type: str,
    *,
    deployment_id: str,
    task_id: str,
    task_type: str | None,
    failure_kind: str | None = None,
) -> dict[str, Any]:
    """Payload of a terminal event (succeeded / failed / revoked), the shape the SSE clients know."""
    fields: dict[str, Any] = {"task_type": task_type, "status": TASK_STATUS_BY_TERMINAL_EVENT[event_type]}
    if event_type == EVENT_FAILED and failure_kind:
        fields["failure_kind"] = failure_kind
    return event_payload(event_type, deployment_id=deployment_id, task_id=task_id, fields=fields)


def seal_results(cipher: Cipher, *, terraform_outputs: Any, tf_state: str | None) -> bytes:
    """Fernet token over the task's outputs and state (``tasks.outputs_enc``)."""
    document = {"terraform_outputs": terraform_outputs, "tf_state": tf_state}
    return cipher.encrypt(json.dumps(document, default=str).encode("utf-8"))


def open_results(cipher: Cipher, token: bytes) -> dict[str, Any]:
    """Inverse of :func:`seal_results`: ``{"terraform_outputs": ..., "tf_state": ...}``."""
    return json.loads(cipher.decrypt(bytes(token)).decode("utf-8"))
