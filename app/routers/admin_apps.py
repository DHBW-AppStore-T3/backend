from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.crud import app_version_approvals as crud_approvals
from app.crud import apps as crud_apps
from app.database import get_db
from app.models import RUNTIME_K8S, AppVersionApprovalStatus, User
from app.routers.apps import (
    _serialize_app,
    ensure_no_marker_errors,
    load_variable_definitions,
    load_version_spec,
    resolve_version_commit,
)
from app.schemas import (
    AppDeployVersionUpdate,
    AppRecommendedUpdate,
    AppResponse,
    AppVersionApprovalDecision,
    AppVersionApprovalResponse,
    AppVersionApprovalWithApp,
)
from app.utils.permissions import require_admin

router = APIRouter()


# ----------------------------------------------------------------
# PENDING REVIEW QUEUE
# ----------------------------------------------------------------
@router.get(
    "/apps/versions/pending",
    response_model=list[AppVersionApprovalWithApp],
    tags=["Admin"],
)
def list_pending_versions(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Return all version submissions awaiting admin review, oldest first."""
    return crud_approvals.get_pending_approvals(db)


def approve_submitted_version(db: Session, app, version_tag: str, admin_id: UUID):
    """The checks and the state change of an approval, shared by the endpoint
    and the management command (``python -m app.cli``): the submitted commit
    must still be the tag's, its markers valid and its spec unchanged."""
    submitted = crud_approvals.get_approval(db, app.appId, version_tag)
    if submitted is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version approval entry not found")
    commit_sha = resolve_version_commit(app, version_tag)
    if submitted.commit_sha and commit_sha != submitted.commit_sha:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": "version_moved",
                "message": "The tag now points to a different commit than the one submitted; submit it again",
            },
        )
    if submitted.commit_sha:
        ensure_no_marker_errors(load_variable_definitions(app, version_tag, commit_sha), "approved")
        # A pod version is approved only with the spec and images that were
        # submitted (.github#4): a commit cannot change, but the platform
        # limits can, and then the scan above already failed with 422.
        loaded = load_version_spec(app, version_tag, commit_sha)
        spec_sha256 = loaded.sha256 if loaded is not None and loaded.runtime == RUNTIME_K8S else None
        if spec_sha256 != submitted.spec_sha256:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"reason": "spec_changed", "message": "The version's appstore.yaml is not the submitted one"},
            )

    return crud_approvals.approve(db, app.appId, version_tag, admin_id)


# ----------------------------------------------------------------
# APPROVE VERSION
# ----------------------------------------------------------------
@router.post(
    "/apps/{app_id}/versions/{version_tag}/approve",
    response_model=AppVersionApprovalResponse,
    tags=["Admin"],
)
def approve_version(
    app_id: UUID,
    version_tag: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Approve a submitted version at its submitted commit — makes exactly
    that commit deployable by all users (.github#7 G).

    - 404: the app or the version submission does not exist.
    - 409 ``version_moved``: the tag no longer points to the submitted
      commit; the admin would approve code that was not submitted.
    - 409: the version is already approved, or was submitted before
      versions were pinned to commits.
    - 422: invalid ``@openstack`` markers in the submitted commit's
      Terraform/Packer variable files (``marker_errors`` lists them).
    - 502: the repository cannot be read; nothing is approved unchecked.
    """
    return approve_submitted_version(db, _require_app(db, app_id), version_tag, current_user.userId)


# ----------------------------------------------------------------
# REJECT VERSION
# ----------------------------------------------------------------
@router.post(
    "/apps/{app_id}/versions/{version_tag}/reject",
    response_model=AppVersionApprovalResponse,
    tags=["Admin"],
)
def reject_version(
    app_id: UUID,
    version_tag: str,
    body: AppVersionApprovalDecision,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Reject a PENDING version with a mandatory reason."""
    _require_app(db, app_id)
    return crud_approvals.reject(
        db, app_id, version_tag, current_user.userId, body.rejection_reason
    )


# ----------------------------------------------------------------
# REVOKE APPROVED VERSION
# ----------------------------------------------------------------
@router.post(
    "/apps/{app_id}/versions/{version_tag}/revoke",
    response_model=AppVersionApprovalResponse,
    tags=["Admin"],
)
def revoke_version(
    app_id: UUID,
    version_tag: str,
    body: AppVersionApprovalDecision,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Revoke a previously APPROVED version with a mandatory reason (sets status to REJECTED)."""
    _require_app(db, app_id)
    return crud_approvals.revoke(db, app_id, version_tag, current_user.userId, body.rejection_reason)


# ----------------------------------------------------------------
# EMERGENCY DEACTIVATION
# ----------------------------------------------------------------
@router.put(
    "/apps/{app_id}",
    response_model=AppResponse,
    tags=["Admin"],
)
def deactivate_app(
    app_id: UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Emergency deactivation: set the app to private so it disappears from
    the store immediately. Does not delete the app or its deployments.

    Marks it ``hidden_by_admin``: the owner can no longer make it public
    again, only an admin can (``PUT /apps/{id}`` with ``is_private: false``,
    .github#7 R).
    """
    _require_app(db, app_id)
    updated = crud_apps.set_hidden_by_admin(db, app_id, True)
    return _serialize_app(updated)


# ----------------------------------------------------------------
# RECOMMENDATION
# ----------------------------------------------------------------
@router.put(
    "/apps/{app_id}/recommended",
    response_model=AppResponse,
    tags=["Admin"],
)
def set_app_recommended(
    app_id: UUID,
    body: AppRecommendedUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Mark an app as recommended or not. Admin only.

    The flag only highlights the app in the catalog; it does not change who
    may see or deploy it.
    """
    _require_app(db, app_id)
    updated = crud_apps.set_recommended(db, app_id, body.recommended)
    return _serialize_app(updated)


# ----------------------------------------------------------------
# DEPLOY VERSION
# ----------------------------------------------------------------
@router.put(
    "/apps/{app_id}/deploy-version",
    response_model=AppResponse,
    tags=["Admin"],
)
def set_deploy_version(
    app_id: UUID,
    body: AppDeployVersionUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Set the version teachers deploy, or clear it with ``version_tag: null``.

    Only a version that is approved for the commit its tag points to now
    can be set:

    - 404: the app does not exist, or the version has no approval entry.
    - 409 ``version_not_approved``: the entry exists but is pending,
      rejected or from before versions were pinned to commits.
    - 409 ``version_moved``: the tag points to another commit than the
      approved one; it has to be submitted and reviewed again.
    - 502: the repository cannot be read.

    The setting is cleared again when the version's approval ends (revoked,
    or replaced by a new submission). ``POST /deployments/`` is not bound to
    it and still checks the approval of the version it is given.
    """
    app = _require_app(db, app_id)
    if body.version_tag is not None:
        # Locked until the commit below: a revocation waits and then clears
        # the setting, so it cannot be set to a version revoked meanwhile.
        approval = crud_approvals.get_approval(db, app_id, body.version_tag, lock=True)
        if approval is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version approval entry not found")
        if approval.status != AppVersionApprovalStatus.APPROVED or approval.commit_sha is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "reason": "version_not_approved",
                    "message": "Only an approved version can be set as the deploy version",
                },
            )
        if resolve_version_commit(app, body.version_tag) != approval.commit_sha:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "reason": "version_moved",
                    "message": "The tag now points to a different commit than the approved one; submit it again",
                },
            )
    updated = crud_apps.set_deploy_version(db, app_id, body.version_tag)
    return _serialize_app(updated)


# ----------------------------------------------------------------
# HELPER
# ----------------------------------------------------------------
def _require_app(db: Session, app_id: UUID):
    app = crud_apps.get_app(db, app_id, include_deleted=True)
    if not app:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="App not found",
        )
    return app
