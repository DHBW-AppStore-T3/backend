"""CRUD for users' OpenStack credentials, one per project (dhbw-appstore's model).

Two ways out of the table, because the dispatch path and the backend's own
OpenStack calls have different needs:

* ``dispatch_envelope`` — JSON-safe dict with **base64 ciphertext** for the
  worker's job. The backend never decrypts here.
* ``decrypted`` — plaintext dict for the backend's own OpenStack calls
  (resource picker, quota, live resources). Plaintext lives only in this
  process's memory for the duration of the request.

The plaintext dict MUST NOT be put into a job.

A credential's project counts only once Keystone named it
(``project_verified``). Credentials saved before that was checked carry a
typed project id; they count for nobody's project until re-checked, and the
re-check also tells which project their owner's old VM deployments live in.
"""
from __future__ import annotations

import base64
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import Deployment, UserOpenStackCredential
from app.schemas import OpenStackCredentialUpsert
from app.utils import crypto
from app.utils.time import utcnow


def list_for_user(db: Session, user_id: UUID) -> list[UserOpenStackCredential]:
    """The caller's credentials, ordered by project name."""
    return (
        db.query(UserOpenStackCredential)
        .filter(UserOpenStackCredential.userId == user_id)
        .order_by(UserOpenStackCredential.project_name, UserOpenStackCredential.project_id)
        .all()
    )


def get_owned(db: Session, user_id: UUID, credential_id: UUID) -> UserOpenStackCredential | None:
    """The credential ``credential_id`` if it belongs to ``user_id``, else None (never someone else's)."""
    return (
        db.query(UserOpenStackCredential)
        .filter(UserOpenStackCredential.credentialId == credential_id, UserOpenStackCredential.userId == user_id)
        .first()
    )


def get_for_project(db: Session, user_id: UUID, project_id: str | None) -> UserOpenStackCredential | None:
    """The user's verified credential for ``project_id`` (at most one), or None."""
    if not project_id:
        return None
    return (
        db.query(UserOpenStackCredential)
        .filter(
            UserOpenStackCredential.userId == user_id,
            UserOpenStackCredential.project_id == project_id,
            UserOpenStackCredential.project_verified.is_(True),
        )
        .first()
    )


def project_ids_of(db: Session, user_id: UUID) -> list[str]:
    """The projects ``user_id`` holds verified credentials for (they are a peer there)."""
    rows = (
        db.query(UserOpenStackCredential.project_id)
        .filter(
            UserOpenStackCredential.userId == user_id,
            UserOpenStackCredential.project_verified.is_(True),
            UserOpenStackCredential.project_id.isnot(None),
        )
        .all()
    )
    return [project_id for (project_id,) in rows]


def holds_project(db: Session, user_id: UUID, project_id: str | None) -> bool:
    """Whether ``user_id`` has a verified credential for ``project_id`` (project peer test)."""
    return get_for_project(db, user_id, project_id) is not None


def save(
    db: Session,
    user_id: UUID,
    payload: OpenStackCredentialUpsert,
    project_id: str,
    project_name: str | None,
) -> tuple[UserOpenStackCredential, bool]:
    """Store a credential Keystone accepted; replace the one for the same project.

    ``project_id``/``project_name`` are the token's (``openstack_validator``),
    never what was typed. Replacing is how a secret is rotated. Identifier
    and secret are Fernet-encrypted; commits. Returns ``(row, created)``.
    """
    row = get_for_project(db, user_id, project_id)
    created = row is None
    if row is None:
        row = UserOpenStackCredential(userId=user_id, project_id=project_id, project_verified=True)
        db.add(row)
    row.auth_type = payload.auth_type
    row.auth_url = payload.auth_url
    row.region_name = payload.region_name
    row.interface = payload.interface or "public"
    row.identity_api_version = payload.identity_api_version or "3"
    row.project_name = project_name or payload.project_name
    row.user_domain_name = payload.user_domain_name
    row.project_domain_name = payload.project_domain_name
    row.encrypted_identifier = crypto.encrypt(payload.identifier)
    row.encrypted_secret = crypto.encrypt(payload.secret)
    row.last_validated_at = utcnow()
    row.last_validation_error = None
    db.commit()
    db.refresh(row)
    return row, created


def stamp_validation(
    db: Session, row: UserOpenStackCredential, error: str | None, project: tuple[str, str | None] | None = None
) -> UserOpenStackCredential:
    """Record the outcome of a re-check (``/test``) without touching the secret; commits.

    A failed check keeps ``last_validated_at`` at the last success. For a
    successful check of a credential whose project was never verified,
    ``project`` is what Keystone named: it becomes the credential's
    project, and the owner's deployments without a project are placed in
    it (the credential could not be changed while they were live, so they
    were made with it). If the owner already has a verified credential for
    that project, this one stays unverified with ``project_has_credential``
    (the deployments are placed all the same).
    """
    if error is not None:
        row.last_validation_error = error
    else:
        row.last_validated_at = utcnow()
        row.last_validation_error = None
        if not row.project_verified and project is not None:
            project_id, project_name = project
            db.execute(
                update(Deployment)
                .where(
                    Deployment.userId == row.userId,
                    Deployment.os_project_id.is_(None),
                    Deployment.deleted_at.is_(None),
                )
                .values(os_project_id=project_id)
                .execution_options(synchronize_session=False)
            )
            other = get_for_project(db, row.userId, project_id)
            if other is not None and other.credentialId != row.credentialId:
                row.last_validation_error = "project_has_credential"
            else:
                row.project_id = project_id
                row.project_name = project_name or row.project_name
                row.project_verified = True
    db.commit()
    db.refresh(row)
    return row


def delete(db: Session, row: UserOpenStackCredential) -> None:
    """Delete the credential and commit; the router refuses while deployments use it."""
    db.delete(row)
    db.commit()


def _common_metadata(row: UserOpenStackCredential) -> dict:
    """The non-secret ``clouds.yaml``-style fields shared by both output shapes."""
    return {
        "auth_type": row.auth_type.value if hasattr(row.auth_type, "value") else row.auth_type,
        "auth_url": row.auth_url,
        "region_name": row.region_name,
        "interface": row.interface or "public",
        "identity_api_version": row.identity_api_version or "3",
        "project_id": row.project_id,
        "project_name": row.project_name,
        "user_domain_name": row.user_domain_name,
        "project_domain_name": row.project_domain_name,
    }


def dispatch_envelope(row: UserOpenStackCredential) -> dict:
    """The JSON-safe envelope shipped to the worker with a job.

    Ciphertext is base64-encoded straight from Postgres — no decryption
    hop in the backend. The worker decrypts in-process.
    """
    envelope = _common_metadata(row)
    envelope["encrypted_identifier_b64"] = base64.b64encode(row.encrypted_identifier).decode("ascii")
    envelope["encrypted_secret_b64"] = base64.b64encode(row.encrypted_secret).decode("ascii")
    return envelope


def decrypted(row: UserOpenStackCredential) -> dict:
    """Plaintext dict for backend-only OpenStack calls.

    NEVER put this into a job. Use ``dispatch_envelope`` for that path.
    """
    out = _common_metadata(row)
    out["identifier"] = crypto.decrypt(row.encrypted_identifier)
    out["secret"] = crypto.decrypt(row.encrypted_secret)
    return out
