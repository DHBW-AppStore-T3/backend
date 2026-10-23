"""Seal the plaintext results of old tasks and scrub credentials from old logs.

Run once on a database that ran tasks before .github#5 / .github#7 E::

    python -m app.seal_legacy_results            # write
    python -m app.seal_legacy_results --dry-run  # only count

1. ``tasks.outputs`` / ``tasks.tf_state`` (plaintext Terraform outputs and
   state, written before .github#5) are sealed into ``tasks.outputs_enc``
   with the Fernet key and the plaintext columns are cleared. A task that
   already has ``outputs_enc`` only loses its plaintext copy.
2. Log entries "Terraform deployment output", which the worker used to
   write with every output value (security review H-8), keep their
   timestamp, level and message but carry only the output names, in
   ``tasks.logs`` and in the stored live events.

Afterwards rotate the credentials of deployments whose logs were read by
people they did not belong to, and rotate container logs on the hosts:
both happen outside this command.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from sqlalchemy import or_, select, text

from app.database import SessionLocal
from app.models import Task, TaskEvent
from app.task_contract import seal_results
from app.utils.crypto import results_cipher

LEAKY_MESSAGE = "Terraform deployment output"
KEPT_FIELDS = ("timestamp", "level", "category", "message", "correlation_id", "deployment_id", "type")
BATCH = 200


def scrub_entry(entry: Any) -> tuple[Any, bool]:
    """``entry`` without output values if it is a leaky log entry; ``(entry, changed)``."""
    if not isinstance(entry, dict) or entry.get("message") != LEAKY_MESSAGE:
        return entry, False
    names = sorted(k for k in entry if k not in KEPT_FIELDS)
    scrubbed = {k: entry[k] for k in KEPT_FIELDS if k in entry}
    scrubbed["message"] = "Terraform deployment outputs"
    scrubbed["output_names"] = names
    return scrubbed, True


def scrub_logs(logs: str | None) -> tuple[str | None, bool]:
    """``tasks.logs`` with leaky entries scrubbed; non-JSON transcripts stay as they are."""
    if not logs:
        return logs, False
    try:
        entries = json.loads(logs)
    except (TypeError, ValueError):
        return logs, False
    if not isinstance(entries, list):
        return logs, False
    changed = False
    out = []
    for entry in entries:
        new, hit = scrub_entry(entry)
        changed |= hit
        out.append(new)
    return (json.dumps(out) if changed else logs), changed


def seal_task(task: Task) -> bool:
    """Seal ``task``'s plaintext results into ``outputs_enc`` and clear them. True if it had any."""
    if task.outputs is None and task.tf_state is None:
        return False
    if not task.outputs_enc:
        try:
            outputs = json.loads(task.outputs) if isinstance(task.outputs, str) else task.outputs
        except ValueError:
            outputs = None
        task.outputs_enc = seal_results(results_cipher, terraform_outputs=outputs, tf_state=task.tf_state)
    task.outputs = None
    task.tf_state = None
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="count only, write nothing")
    args = parser.parse_args(argv)

    sealed = scrubbed_tasks = scrubbed_events = 0
    with SessionLocal() as db:
        last = None
        while True:
            query = select(Task).order_by(Task.taskId).limit(BATCH)
            query = query.where(or_(Task.outputs.isnot(None), Task.tf_state.isnot(None), Task.logs.isnot(None)))
            if last is not None:
                query = query.where(Task.taskId > last)
            tasks = db.scalars(query).all()
            if not tasks:
                break
            for task in tasks:
                if seal_task(task):
                    sealed += 1
                logs, changed = scrub_logs(task.logs)
                if changed:
                    task.logs = logs
                    scrubbed_tasks += 1
            last = tasks[-1].taskId
            if args.dry_run:
                db.rollback()
            else:
                db.commit()

        events = db.scalars(
            select(TaskEvent).where(text("payload->>'message' = :m")).params(m=LEAKY_MESSAGE)
        ).all()
        for event in events:
            event.payload, _ = scrub_entry(dict(event.payload))
            scrubbed_events += 1
        if args.dry_run:
            db.rollback()
        else:
            db.commit()

    verb = "would be" if args.dry_run else "were"
    print(f"{sealed} task result(s) {verb} sealed, {scrubbed_tasks} task log(s) and "
          f"{scrubbed_events} live event(s) {verb} scrubbed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
