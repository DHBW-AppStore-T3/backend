"""Admin review of app versions, bound to the commit (.github#7 G).

A review covers one commit: ``commit_sha`` is what the tag pointed to at
submission. When the tag is moved later, the old review no longer applies:
the version is unapproved and can be submitted again. Approvals from before
the pinning have no commit and never count as approved.
"""

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models import RUNTIME_K8S, App, AppVersionApproval, AppVersionApprovalStatus
from app.utils.time import utcnow


def submit_version(
    db: Session,
    app_id: UUID,
    version_tag: str,
    commit_sha: str,
    diff_url: str | None = None,
    notes: str | None = None,
    runtime: str = RUNTIME_K8S,
    spec_sha256: str | None = None,
    image_digests: str | None = None,
) -> AppVersionApproval:
    """Submit ``version_tag`` at ``commit_sha`` for admin review.

    ``runtime``, ``spec_sha256`` and ``image_digests`` (JSON list) record
    what the review covers besides the commit (.github#4).

    Raises 409 if this commit is already pending or approved. A rejected
    version, or one whose review was for a commit the tag no longer points
    to, can be submitted again: the old entry is replaced.
    """
    existing = get_approval(db, app_id, version_tag)

    if existing and existing.commit_sha == commit_sha:
        if existing.status == AppVersionApprovalStatus.PENDING:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Version is already pending review",
            )
        if existing.status == AppVersionApprovalStatus.APPROVED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Version is already approved",
            )
    if existing:
        # Rejected, or reviewed for another commit: start over. An app never
        # keeps pointing at a version whose review is gone.
        _clear_deploy_version(db, app_id, version_tag)
        db.delete(existing)
        db.flush()

    approval = AppVersionApproval(
        appId=app_id,
        version_tag=version_tag,
        commit_sha=commit_sha,
        diff_url=diff_url,
        notes=notes,
        runtime=runtime,
        spec_sha256=spec_sha256,
        image_digests=image_digests,
        status=AppVersionApprovalStatus.PENDING,
        created_at=utcnow(),
    )
    db.add(approval)
    db.commit()
    db.refresh(approval)
    return approval


def get_pending_approvals(db: Session) -> list[AppVersionApproval]:
    """Return all PENDING version approvals for public apps, oldest first."""
    return (
        db.query(AppVersionApproval)
        .join(App, App.appId == AppVersionApproval.appId)
        .filter(
            AppVersionApproval.status == AppVersionApprovalStatus.PENDING,
            App.is_private.is_(False),
        )
        .order_by(AppVersionApproval.created_at.asc())
        .all()
    )


def get_approvals_for_app(db: Session, app_id: UUID) -> list[AppVersionApproval]:
    """Return all review entries of an app, whatever their status, newest first."""
    return (
        db.query(AppVersionApproval)
        .filter(AppVersionApproval.appId == app_id)
        .order_by(AppVersionApproval.created_at.desc())
        .all()
    )


def get_approval(db: Session, app_id: UUID, version_tag: str, lock: bool = False) -> AppVersionApproval | None:
    """The review entry of ``version_tag`` (any status), or None.

    ``lock`` keeps the row locked until the transaction ends, so a revocation
    cannot slip in between a check of the status and what is written on it.
    """
    query = db.query(AppVersionApproval).filter(
        AppVersionApproval.appId == app_id,
        AppVersionApproval.version_tag == version_tag,
    )
    if lock:
        query = query.with_for_update()
    return query.first()


def _clear_deploy_version(db: Session, app_id: UUID, version_tag: str) -> None:
    """Clear ``apps.deploy_version`` if it is ``version_tag`` (no commit; the caller commits)."""
    db.query(App).filter(App.appId == app_id, App.deploy_version == version_tag).update(
        {App.deploy_version: None}, synchronize_session=False
    )


def has_approved_version(db: Session, app_id: UUID, version_tag: str, commit_sha: str) -> bool:
    """Whether ``version_tag`` is approved for exactly ``commit_sha``."""
    if not commit_sha:
        return False
    return (
        db.query(AppVersionApproval.approvalId)
        .filter(
            AppVersionApproval.appId == app_id,
            AppVersionApproval.version_tag == version_tag,
            AppVersionApproval.commit_sha == commit_sha,
            AppVersionApproval.status == AppVersionApprovalStatus.APPROVED,
        )
        .first()
    ) is not None


def approved_commits(db: Session, app_id: UUID) -> dict[str, str]:
    """Approved versions of the app: tag -> the commit the approval covers."""
    rows = (
        db.query(AppVersionApproval.version_tag, AppVersionApproval.commit_sha)
        .filter(
            AppVersionApproval.appId == app_id,
            AppVersionApproval.status == AppVersionApprovalStatus.APPROVED,
            AppVersionApproval.commit_sha.isnot(None),
        )
        .all()
    )
    return {row.version_tag: row.commit_sha for row in rows}


def has_any_approved_version(db: Session, app_id: UUID) -> bool:
    """Whether the app has at least one approved, commit-pinned version."""
    return (
        db.query(AppVersionApproval.approvalId)
        .filter(
            AppVersionApproval.appId == app_id,
            AppVersionApproval.status == AppVersionApprovalStatus.APPROVED,
            AppVersionApproval.commit_sha.isnot(None),
        )
        .first()
    ) is not None


def withdraw(db: Session, app_id: UUID, version_tag: str) -> None:
    """Delete a PENDING approval entry (owner withdraws submission)."""
    approval = _get_approval_or_404(db, app_id, version_tag)

    if approval.status != AppVersionApprovalStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Only pending submissions can be withdrawn (current status: '{approval.status.value}')",
        )

    db.delete(approval)
    db.commit()


def _get_approval_or_404(
    db: Session, app_id: UUID, version_tag: str
) -> AppVersionApproval:
    approval = get_approval(db, app_id, version_tag)
    if not approval:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Version approval entry not found",
        )
    return approval


def approve(
    db: Session,
    app_id: UUID,
    version_tag: str,
    admin_id: UUID,
) -> AppVersionApproval:
    """Approve a PENDING or REJECTED version (at its stored ``commit_sha``).

    Raises 404 without an entry, 409 if it is already approved or was
    submitted before versions were pinned to commits (it has none).
    """
    approval = _get_approval_or_404(db, app_id, version_tag)

    if approval.status == AppVersionApprovalStatus.APPROVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Version is already approved",
        )
    if not approval.commit_sha:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": "version_not_pinned",
                "message": "The submission has no commit; submit the version again",
            },
        )

    approval.status = AppVersionApprovalStatus.APPROVED
    approval.reviewed_by = admin_id
    approval.reviewed_at = utcnow()
    approval.rejection_reason = None
    db.commit()
    db.refresh(approval)
    return approval


def reject(
    db: Session,
    app_id: UUID,
    version_tag: str,
    admin_id: UUID,
    rejection_reason: str,
) -> AppVersionApproval:
    """Reject a PENDING version with a mandatory reason."""
    approval = _get_approval_or_404(db, app_id, version_tag)

    if approval.status != AppVersionApprovalStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot reject a version with status '{approval.status.value}'",
        )

    approval.status = AppVersionApprovalStatus.REJECTED
    approval.reviewed_by = admin_id
    approval.reviewed_at = utcnow()
    approval.rejection_reason = rejection_reason
    db.commit()
    db.refresh(approval)
    return approval


def revoke(
    db: Session,
    app_id: UUID,
    version_tag: str,
    admin_id: UUID,
    rejection_reason: str,
) -> AppVersionApproval:
    """Revoke a previously APPROVED version (sets status back to REJECTED).

    Existing deployments of the version are not touched. If it is the
    app's deploy version, that setting is cleared.
    """
    approval = _get_approval_or_404(db, app_id, version_tag)

    if approval.status != AppVersionApprovalStatus.APPROVED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot revoke a version with status '{approval.status.value}'",
        )

    approval.status = AppVersionApprovalStatus.REJECTED
    approval.reviewed_by = admin_id
    approval.reviewed_at = utcnow()
    approval.rejection_reason = rejection_reason
    _clear_deploy_version(db, app_id, version_tag)
    db.commit()
    db.refresh(approval)
    return approval


def pin_unpinned(db: Session, app_id: UUID, resolve) -> list[tuple[str, str | None]]:
    """Record the current commit of every unpinned review entry of ``app_id``.

    ``resolve(tag) -> sha | None`` reads the repository. Entries whose tag
    is gone are left unpinned (and so never count). Returns
    ``[(tag, sha or None)]``. Used once by ``app.backfill_approval_commits``.
    """
    done: list[tuple[str, str | None]] = []
    for approval in get_approvals_for_app(db, app_id):
        if approval.commit_sha:
            continue
        sha = resolve(approval.version_tag)
        if sha:
            approval.commit_sha = sha
        done.append((approval.version_tag, sha))
    db.commit()
    return done
