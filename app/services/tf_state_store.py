"""OpenTofu state of the VM deployments, kept in the AppStore database.

OpenTofu reaches it through the HTTP state backend (``routers/internal_tfstate``);
the API reads it for the resource views. The state is Fernet-encrypted at
rest because it holds every attribute of every resource, generated passwords
included. Ported from dhbw-appstore.

Access is per task: a queued VM task carries a random token, sealed in its
job message, and only the token's SHA-256 on its row. The token opens the
state of that task's deployment for as long as the task is RUNNING, so a job
can neither reach another deployment's state nor keep access after it ended.
This replaces the shared ``postgres-tfstate`` login that every job used to
get in its environment, which opened every deployment's state.

Locking mirrors OpenTofu's HTTP backend: a lock records the lock id, the
lock info JSON and the holding task; a lock whose task is no longer RUNNING
is stale and may be overridden, so a crashed worker cannot block a
deployment's state for good.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Task, TaskStatus, TerraformState
from app.utils.crypto import decrypt, encrypt


class LockConflict(Exception):
    """Someone else holds the state lock. Carries their lock info (JSON)."""

    def __init__(self, lock_info: str | None) -> None:
        super().__init__("state is locked")
        self.lock_info = lock_info


def token_hash(token: str) -> str:
    """What the task row keeps of its state token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token(task: Task) -> str:
    """A fresh state token for ``task``; stores its hash on the row (not committed)."""
    token = secrets.token_urlsafe(32)
    task.state_token_hash = token_hash(token)
    return token


def authorize(db: Session, deployment_id: uuid.UUID, task_id: str, token: str) -> Task | None:
    """The running task of ``deployment_id`` that ``token`` belongs to, or None.

    None for a malformed ``task_id``, a task of another deployment, a task
    that is not RUNNING, or a wrong token (constant-time comparison).
    """
    try:
        task_uuid = uuid.UUID(task_id)
    except ValueError:
        return None
    task = db.get(Task, task_uuid)
    if task is None or task.deploymentId != deployment_id or task.status != TaskStatus.RUNNING:
        return None
    if not task.state_token_hash or not hmac.compare_digest(task.state_token_hash, token_hash(token)):
        return None
    return task


def read(db: Session, deployment_id: uuid.UUID) -> str | None:
    """The decrypted state as JSON text, or None when there is none yet."""
    row = db.get(TerraformState, deployment_id)
    if row is None or row.state is None:
        return None
    return decrypt(row.state)


def _row_for_update(db: Session, deployment_id: uuid.UUID) -> TerraformState:
    """The deployment's state row, created if needed, locked ``FOR UPDATE``."""
    # Insert-if-missing first, so the row lock below always has a row to take
    # and two first writers cannot both create it.
    db.execute(insert(TerraformState).values(deploymentId=deployment_id).on_conflict_do_nothing())
    return db.scalars(
        select(TerraformState).where(TerraformState.deploymentId == deployment_id).with_for_update()
    ).one()


def _lock_is_stale(db: Session, row: TerraformState) -> bool:
    """Whether the row's lock may be overridden: no holder task, or it no longer runs."""
    if row.lock_task_id is None:
        return True
    holder = db.get(Task, row.lock_task_id)
    return holder is None or holder.status != TaskStatus.RUNNING


def write(db: Session, deployment_id: uuid.UUID, state_json: str, lock_id: str | None) -> None:
    """Encrypt and store a new state, then commit.

    With a live lock held, only a request carrying its ``lock_id`` may
    write; anyone else gets ``LockConflict``. The body must be JSON.
    """
    json.loads(state_json)  # refuse garbage before it replaces a state
    row = _row_for_update(db, deployment_id)
    if row.lock_id and row.lock_id != lock_id and not _lock_is_stale(db, row):
        info = row.lock_info
        db.rollback()
        raise LockConflict(info)
    row.state = encrypt(state_json)
    db.commit()


def lock(db: Session, deployment_id: uuid.UUID, task: Task, lock_info: str) -> None:
    """Take the state lock for ``task`` and commit.

    ``lock_info`` is OpenTofu's lock JSON; its ``ID`` becomes the lock id.
    Re-locking with the same id succeeds. Raises LockConflict when another
    live lock is held, ValueError if ``lock_info`` has no ``ID``.
    """
    try:
        lock_id = str(json.loads(lock_info)["ID"])
    except (ValueError, KeyError, TypeError) as e:
        raise ValueError("lock info without ID") from e
    row = _row_for_update(db, deployment_id)
    if row.lock_id and row.lock_id != lock_id and not _lock_is_stale(db, row):
        info = row.lock_info
        db.rollback()
        raise LockConflict(info)
    row.lock_id = lock_id
    row.lock_info = lock_info
    row.lock_task_id = task.taskId
    db.commit()


def unlock(db: Session, deployment_id: uuid.UUID, lock_info: str | None) -> None:
    """Release the lock and commit.

    An unlock naming a different live lock is a conflict; an unlock without
    a lock id (force-unlock) always succeeds.
    """
    row = _row_for_update(db, deployment_id)
    lock_id = None
    if lock_info:
        try:
            lock_id = str(json.loads(lock_info).get("ID"))
        except (ValueError, AttributeError):
            lock_id = None
    if row.lock_id and lock_id and row.lock_id != lock_id and not _lock_is_stale(db, row):
        info = row.lock_info
        db.rollback()
        raise LockConflict(info)
    row.lock_id = None
    row.lock_info = None
    row.lock_task_id = None
    db.commit()


def drop(db: Session, deployment_id: uuid.UUID) -> None:
    """Forget the state. Does not commit; the caller does."""
    db.execute(delete(TerraformState).where(TerraformState.deploymentId == deployment_id))


def import_state(db: Session, deployment_id: uuid.UUID, state_json: str) -> bool:
    """Store a state taken over from elsewhere unless one exists; commits. True if stored.

    For ``app.import_pg_tfstate``: deployments created before the switch keep
    their state in the old ``pg`` backend until it is moved here.
    """
    json.loads(state_json)
    row = _row_for_update(db, deployment_id)
    if row.state is not None:
        db.rollback()
        return False
    row.state = encrypt(state_json)
    db.commit()
    return True
