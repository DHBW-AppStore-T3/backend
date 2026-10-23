"""OpenTofu's HTTP state backend for the VM path (ported from dhbw-appstore).

The worker points OpenTofu's ``http`` backend here for every VM job;
OpenTofu then reads, writes and locks the deployment's state itself.
Credentials are HTTP Basic: the task id as user name and the task's state
token as password (``services/tf_state_store``).

Not part of the public API: left out of the OpenAPI document, and the
ingress does not route ``/api/internal/`` (Traefik and Caddy answer 404).
The token check stands on its own either way.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Task
from app.services import tf_state_store

router = APIRouter(include_in_schema=False)

_basic = HTTPBasic(auto_error=False)
# A state is a few hundred KiB at most; refuse anything absurd before
# decrypting or storing it.
MAX_STATE_BYTES = 16 * 1024 * 1024


def _task(
    deployment_id: uuid.UUID,
    credentials: HTTPBasicCredentials | None = Depends(_basic),
    db: Session = Depends(get_db),
) -> Task:
    """The running task of ``deployment_id`` whose token matches; 401 otherwise."""
    task = None
    if credentials is not None:
        task = tf_state_store.authorize(db, deployment_id, credentials.username, credentials.password)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": 'Basic realm="tfstate"'},
        )
    return task


async def _body(request: Request) -> str:
    """The request body as text, refused (413) beyond ``MAX_STATE_BYTES`` without buffering more."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_STATE_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_STATE_BYTES:
            raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        chunks.append(chunk)
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="state must be UTF-8") from e


def _locked(e: tf_state_store.LockConflict) -> Response:
    """423 Locked carrying the current holder's lock info (OpenTofu shows it)."""
    return Response(status_code=status.HTTP_423_LOCKED, content=e.lock_info or "{}", media_type="application/json")


@router.get("/{deployment_id}")
def get_state(deployment_id: uuid.UUID, _: Task = Depends(_task), db: Session = Depends(get_db)) -> Response:
    """The deployment's state as JSON, or 204 when there is none yet."""
    state = tf_state_store.read(db, deployment_id)
    if state is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return Response(content=state, media_type="application/json")


@router.post("/{deployment_id}")
async def put_state(
    deployment_id: uuid.UUID,
    request: Request,
    _: Task = Depends(_task),
    db: Session = Depends(get_db),
) -> Response:
    """Store a new state; OpenTofu passes its lock id as ``ID``. 423 while another task holds the lock."""
    body = await _body(request)
    try:
        tf_state_store.write(db, deployment_id, body, request.query_params.get("ID"))
    except tf_state_store.LockConflict as e:
        return _locked(e)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="state must be JSON") from e
    return Response(status_code=status.HTTP_200_OK)


@router.delete("/{deployment_id}")
def delete_state(deployment_id: uuid.UUID, _: Task = Depends(_task), db: Session = Depends(get_db)) -> Response:
    """Delete the deployment's state (and its lock)."""
    tf_state_store.drop(db, deployment_id)
    db.commit()
    return Response(status_code=status.HTTP_200_OK)


@router.api_route("/{deployment_id}", methods=["LOCK"])
async def lock_state(
    deployment_id: uuid.UUID,
    request: Request,
    task: Task = Depends(_task),
    db: Session = Depends(get_db),
) -> Response:
    """Take the state lock for the calling task. 400 without ``ID``, 423 while held."""
    body = await _body(request)
    try:
        tf_state_store.lock(db, deployment_id, task, body)
    except tf_state_store.LockConflict as e:
        return _locked(e)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    return Response(status_code=status.HTTP_200_OK)


@router.api_route("/{deployment_id}", methods=["UNLOCK"])
async def unlock_state(
    deployment_id: uuid.UUID,
    request: Request,
    _: Task = Depends(_task),
    db: Session = Depends(get_db),
) -> Response:
    """Release the state lock; 409 when the body names a different live lock."""
    body = await _body(request)
    try:
        tf_state_store.unlock(db, deployment_id, body or None)
    except tf_state_store.LockConflict as e:
        return Response(status_code=status.HTTP_409_CONFLICT, content=e.lock_info or "{}", media_type="application/json")
    return Response(status_code=status.HTTP_200_OK)
