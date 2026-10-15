from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.crud import app_version_approvals as crud_approvals
from app.crud import apps as crud_apps
from app.database import get_db
from app.models import RUNTIME_K8S, User
from app.routers.apps import (
    _serialize_app,
    ensure_no_marker_errors,
    load_variable_definitions,
    load_version_spec,
    resolve_version_commit,
)
from app.schemas import (
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
    app = _require_app(db, app_id)
    submitted = crud_approvals.get_approval(db, app_id, version_tag)
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

    return crud_approvals.approve(db, app_id, version_tag, current_user.userId)


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
    """Emergency deactivation: set app to private so it disappears from
    the store immediately. Does not delete the app or its deployments."""
    from app.schemas import AppUpdate

    _require_app(db, app_id)
    updated = crud_apps.update_app(db, app_id, AppUpdate(is_private=True))
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
