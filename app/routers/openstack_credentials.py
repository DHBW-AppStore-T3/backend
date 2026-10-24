"""The caller's OpenStack credentials, one per project (dhbw-appstore's model).

All endpoints scope to the caller (`current_user`) — there is no
`{user_id}` path parameter, and another person's credential id answers 404.
Responses never contain identifier or secret material.

A credential is stored only after Keystone accepted it; its project comes
from the token, so holding one proves access to that project. Saving a
credential for a project that already has one replaces it: that is how a
secret is rotated, and it is allowed at any time. Deleting is refused while
the caller owns live deployments in the project, which would otherwise be
left without anyone able to run them.

Credentials saved before projects were taken from Keystone are unverified
until re-checked (``/test``); see ``crud.openstack_credentials``.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.crud import deployments as crud_deployments
from app.crud import locks as crud_locks
from app.crud import openstack_credentials as crud_creds
from app.database import get_db
from app.models import User, UserOpenStackCredential
from app.schemas import (
    OpenStackCredentialFromYaml,
    OpenStackCredentialResponse,
    OpenStackCredentialUpsert,
)
from app.services import clouds_yaml_parser, openstack_client, openstack_validator
from app.utils.keycloak_auth import get_current_user_keycloak

router = APIRouter()


def _active_deployments(db: Session, row: UserOpenStackCredential) -> int:
    """Live deployments of the owner that need this credential.

    Those in its project; for an unverified credential, those whose project
    is still unknown (only its re-check can place them).
    """
    if row.project_verified:
        return crud_deployments.count_active_user_deployments(db, row.userId, row.project_id)
    return crud_deployments.count_active_user_deployments(db, row.userId, unplaced=True)


def _response(db: Session, row: UserOpenStackCredential) -> OpenStackCredentialResponse:
    """Serialize a credential row with the number of live deployments that need it."""
    response = OpenStackCredentialResponse.model_validate(row)
    response.active_deployments = _active_deployments(db, row)
    return response


def _rejected(e: openstack_validator.CredentialRejected) -> HTTPException:
    """422 ``openstack_credentials_invalid`` carrying Keystone's rejection reason as ``cause``."""
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"reason": "openstack_credentials_invalid", "cause": e.reason},
    )


def _save(db: Session, user: User, payload: OpenStackCredentialUpsert, response: Response):
    """Validate ``payload`` against Keystone, then insert or replace the caller's credential.

    The project id/name come from the Keystone token, not from the payload.
    Sets the response status to 201 (new project) or 200 (replaced) and drops
    any cached resource lists of the credential. Commits via
    ``crud_creds.save``. Raises the 422 from ``_rejected`` when Keystone refuses.
    """
    try:
        scope = openstack_validator.validate(payload)
    except openstack_validator.CredentialRejected as e:
        raise _rejected(e) from None
    # Serialized with deploy dispatch, which reads the credential in its TX.
    crud_locks.acquire_user_xact_lock(db, user.userId)
    row, created = crud_creds.save(db, user.userId, payload, scope.project_id, scope.project_name)
    openstack_client.invalidate(row.credentialId)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return _response(db, row)


@router.get("/me/openstack-credentials", response_model=list[OpenStackCredentialResponse])
def list_my_credentials(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """List the caller's OpenStack credentials, one per project, without secrets.

    Each entry reports the result of the last Keystone check and the number of
    the caller's live deployments that need it.
    """
    return [_response(db, row) for row in crud_creds.list_for_user(db, current_user.userId)]


@router.post(
    "/me/openstack-credentials",
    response_model=OpenStackCredentialResponse,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"description": "Replaced the credential for the same project"}},
)
def save_my_credential(
    payload: OpenStackCredentialUpsert,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Check against Keystone, then store (201) or replace the one for the same project (200).

    422 ``openstack_credentials_invalid`` with a ``cause`` when Keystone
    does not accept it (``auth_url_not_allowed`` for a Keystone URL outside
    the allowlist); nothing is stored then.
    """
    return _save(db, current_user, payload, response)


@router.post(
    "/me/openstack-credentials/from-yaml",
    response_model=OpenStackCredentialResponse,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"description": "Replaced the credential for the same project"}},
)
def save_my_credential_from_yaml(
    body: OpenStackCredentialFromYaml,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Same as the plain POST, from a pasted `clouds.yaml`.

    ``cloud_name`` picks the entry when the file holds several clouds. Parse
    errors answer 422; otherwise status codes as for the plain POST.
    """
    payload = clouds_yaml_parser.parse(body.clouds_yaml, body.cloud_name)
    return _save(db, current_user, payload, response)


@router.post(
    "/me/openstack-credentials/{credential_id}/test",
    response_model=OpenStackCredentialResponse,
)
def test_my_credential(
    credential_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Ask Keystone again and record the outcome (e.g. a revoked application credential).

    Always answers 200 with the updated credential; a failure shows up in its
    validation fields (``project_changed`` when the credential now scopes to a
    different project). An unverified credential becomes verified with the
    project Keystone names, and the caller's deployments whose project was
    unknown are placed in it. 404 for a credential that is not the caller's.
    """
    row = openstack_client.owned_credential(db, current_user, credential_id)
    plaintext = crud_creds.decrypted(row)
    payload = OpenStackCredentialUpsert(
        auth_type=row.auth_type,
        auth_url=row.auth_url,
        region_name=row.region_name,
        interface=row.interface,
        identity_api_version=row.identity_api_version,
        project_id=row.project_id,
        project_name=row.project_name,
        user_domain_name=row.user_domain_name,
        project_domain_name=row.project_domain_name,
        identifier=plaintext["identifier"],
        secret=plaintext["secret"],
    )
    project = None
    try:
        scope = openstack_validator.validate(payload)
    except openstack_validator.CredentialRejected as e:
        error: str | None = e.reason
    else:
        project = (scope.project_id, scope.project_name)
        error = None if not row.project_verified or scope.project_id == row.project_id else "project_changed"
    crud_locks.acquire_user_xact_lock(db, current_user.userId)
    openstack_client.invalidate(row.credentialId)
    return _response(db, crud_creds.stamp_validation(db, row, error, project))


@router.delete(
    "/me/openstack-credentials/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_my_credential(
    credential_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Delete one of the caller's credentials (204).

    404 when it is not the caller's; 409 ``openstack_credentials_locked`` with
    ``active_deployments`` while the caller still owns live deployments that
    need it.
    """
    crud_locks.acquire_user_xact_lock(db, current_user.userId)
    row = openstack_client.owned_credential(db, current_user, credential_id)
    active = _active_deployments(db, row)
    if active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": "openstack_credentials_locked", "active_deployments": active},
        )
    openstack_client.invalidate(row.credentialId)
    crud_creds.delete(db, row)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
