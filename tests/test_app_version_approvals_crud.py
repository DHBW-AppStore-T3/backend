"""Integration-tests for ``app.crud.app_version_approvals``.

Deckt die Submission-, Review- und Lifecycle-Pfade der
``AppVersionApproval``-Tabelle ab. Die Credential-CRUD testet
``tests/test_openstack_credentials_crud.py``.

Alle Tests sind ``@pytest.mark.integration`` und ziehen die ``db``-Fixture
aus ``tests/conftest.py``. ORM-Objekte werden direkt konstruiert; die
HTTP-API wird bewusst nicht beruehrt, um die CRUD-Schicht isoliert zu
testen.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

import pytest
from fastapi import HTTPException

from app.crud import app_version_approvals as crud_approvals
from app.models import (
    App,
    AppVersionApproval,
    AppVersionApprovalStatus,
    User,
    UserRole,
)


# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------
def _sha(tag: str) -> str:
    """A commit SHA for ``tag`` (the tests never reach a Git host)."""
    return hashlib.sha1(tag.encode()).hexdigest()


def _make_user(db, *, role: UserRole = UserRole.STUDENT) -> User:
    user = User(
        userId=uuid.uuid4(),
        keycloak_id=f"kc-{uuid.uuid4()}",
        email=f"{uuid.uuid4()}@example.test",
        username=f"user-{uuid.uuid4().hex[:8]}",
        role=role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_app(db, owner: User, *, is_private: bool = False, name: str = "App") -> App:
    application = App(
        appId=uuid.uuid4(),
        name=name,
        git_link="https://example.invalid/repo.git",
        is_private=is_private,
        userId=owner.userId,
    )
    db.add(application)
    db.commit()
    db.refresh(application)
    return application


# ================================================================
# app.crud.app_version_approvals
# ================================================================
@pytest.mark.integration
def test_submit_version_creates_pending_row(db):
    """submit_version legt eine PENDING-Zeile an."""
    owner = _make_user(db)
    application = _make_app(db, owner)

    approval = crud_approvals.submit_version(
        db,
        application.appId,
        "v1.0.0",
        _sha("v1.0.0"),
        diff_url="https://example/diff",
        notes="initial submission",
    )

    assert approval.status == AppVersionApprovalStatus.PENDING
    assert approval.appId == application.appId
    assert approval.version_tag == "v1.0.0"
    assert approval.commit_sha == _sha("v1.0.0")
    assert approval.diff_url == "https://example/diff"
    assert approval.notes == "initial submission"
    assert approval.reviewed_at is None
    assert approval.reviewed_by is None
    assert approval.created_at is not None


@pytest.mark.integration
def test_submit_version_conflict_when_already_pending(db):
    """Erneutes submit auf eine PENDING-Zeile gibt 409."""
    owner = _make_user(db)
    application = _make_app(db, owner)
    crud_approvals.submit_version(db, application.appId, "v1.0.0", _sha("v1.0.0"))

    with pytest.raises(HTTPException) as exc:
        crud_approvals.submit_version(db, application.appId, "v1.0.0", _sha("v1.0.0"))
    assert exc.value.status_code == 409
    assert "pending" in exc.value.detail.lower()


@pytest.mark.integration
def test_submit_version_conflict_when_already_approved(db):
    """Erneutes submit auf APPROVED gibt 409."""
    owner = _make_user(db)
    admin = _make_user(db, role=UserRole.ADMIN)
    application = _make_app(db, owner)
    crud_approvals.submit_version(db, application.appId, "v1.0.0", _sha("v1.0.0"))
    crud_approvals.approve(db, application.appId, "v1.0.0", admin.userId)

    with pytest.raises(HTTPException) as exc:
        crud_approvals.submit_version(db, application.appId, "v1.0.0", _sha("v1.0.0"))
    assert exc.value.status_code == 409
    assert "approved" in exc.value.detail.lower()


@pytest.mark.integration
def test_submit_version_after_rejection_replaces_row(db):
    """REJECTED erlaubt resubmission: alte Zeile wird geloescht, neue PENDING."""
    owner = _make_user(db)
    admin = _make_user(db, role=UserRole.ADMIN)
    application = _make_app(db, owner)
    first = crud_approvals.submit_version(db, application.appId, "v1.0.0", _sha("v1.0.0"))
    crud_approvals.reject(
        db, application.appId, "v1.0.0", admin.userId, "needs work"
    )

    second = crud_approvals.submit_version(db, application.appId, "v1.0.0", _sha("v1.0.0"))

    assert second.status == AppVersionApprovalStatus.PENDING
    assert second.approvalId != first.approvalId
    # Es darf nur eine Zeile fuer (appId, version_tag) existieren.
    rows = (
        db.query(AppVersionApproval)
        .filter(
            AppVersionApproval.appId == application.appId,
            AppVersionApproval.version_tag == "v1.0.0",
        )
        .all()
    )
    assert len(rows) == 1
    assert rows[0].approvalId == second.approvalId


@pytest.mark.integration
def test_get_pending_approvals_only_public_apps(db):
    """get_pending_approvals listet nur PENDING-Zeilen oeffentlicher Apps."""
    owner = _make_user(db)
    public_app = _make_app(db, owner, is_private=False, name="pub")
    private_app = _make_app(db, owner, is_private=True, name="priv")

    pending_pub = crud_approvals.submit_version(db, public_app.appId, "v1", _sha("v1"))
    crud_approvals.submit_version(db, private_app.appId, "v1", _sha("v1"))

    result = crud_approvals.get_pending_approvals(db)

    ids = {a.approvalId for a in result}
    assert pending_pub.approvalId in ids
    # Der private-App-Eintrag darf NICHT enthalten sein.
    assert all(a.appId == public_app.appId for a in result)


@pytest.mark.integration
def test_get_pending_approvals_excludes_non_pending(db):
    """Approvals mit Status != PENDING tauchen in get_pending_approvals nicht auf."""
    owner = _make_user(db)
    admin = _make_user(db, role=UserRole.ADMIN)
    application = _make_app(db, owner, is_private=False)

    crud_approvals.submit_version(db, application.appId, "v1", _sha("v1"))
    crud_approvals.approve(db, application.appId, "v1", admin.userId)

    crud_approvals.submit_version(db, application.appId, "v2", _sha("v2"))
    crud_approvals.reject(db, application.appId, "v2", admin.userId, "no")

    pending_v3 = crud_approvals.submit_version(db, application.appId, "v3", _sha("v3"))

    result = crud_approvals.get_pending_approvals(db)
    assert [a.approvalId for a in result] == [pending_v3.approvalId]


@pytest.mark.integration
def test_get_approvals_for_app_newest_first(db):
    """get_approvals_for_app sortiert nach created_at DESC."""
    owner = _make_user(db)
    application = _make_app(db, owner)

    a1 = crud_approvals.submit_version(db, application.appId, "v1", _sha("v1"))
    # Direkt nach dem Commit Datum manuell setzen, damit die Ordnung
    # deterministisch ist (keine Sleeps).
    a1.created_at = datetime(2024, 1, 1, 12, 0, 0)
    db.commit()

    a2 = crud_approvals.submit_version(db, application.appId, "v2", _sha("v2"))
    a2.created_at = datetime(2024, 6, 1, 12, 0, 0)
    db.commit()

    a3 = crud_approvals.submit_version(db, application.appId, "v3", _sha("v3"))
    a3.created_at = datetime(2024, 12, 1, 12, 0, 0)
    db.commit()

    result = crud_approvals.get_approvals_for_app(db, application.appId)
    assert [a.version_tag for a in result] == ["v3", "v2", "v1"]


@pytest.mark.integration
def test_get_approvals_for_app_returns_empty_for_unknown(db):
    """Unbekannte appId liefert leere Liste."""
    result = crud_approvals.get_approvals_for_app(db, uuid.uuid4())
    assert result == []


@pytest.mark.integration
def test_has_approved_version_true_and_false(db):
    """has_approved_version gibt True nur fuer exakt diese APPROVED-Version."""
    owner = _make_user(db)
    admin = _make_user(db, role=UserRole.ADMIN)
    application = _make_app(db, owner)

    # Keine Approval-Zeile vorhanden -> False.
    assert (
        crud_approvals.has_approved_version(db, application.appId, "v1", _sha("v1")) is False
    )

    crud_approvals.submit_version(db, application.appId, "v1", _sha("v1"))
    # PENDING zaehlt nicht.
    assert (
        crud_approvals.has_approved_version(db, application.appId, "v1", _sha("v1")) is False
    )

    crud_approvals.approve(db, application.appId, "v1", admin.userId)
    assert (
        crud_approvals.has_approved_version(db, application.appId, "v1", _sha("v1")) is True
    )
    # Andere Version: False.
    assert (
        crud_approvals.has_approved_version(db, application.appId, "v2", _sha("v2")) is False
    )


@pytest.mark.integration
def test_has_any_approved_version_true_and_false(db):
    """has_any_approved_version: True sobald mindestens eine APPROVED existiert."""
    owner = _make_user(db)
    admin = _make_user(db, role=UserRole.ADMIN)
    application = _make_app(db, owner)

    assert crud_approvals.has_any_approved_version(db, application.appId) is False

    crud_approvals.submit_version(db, application.appId, "v1", _sha("v1"))
    assert crud_approvals.has_any_approved_version(db, application.appId) is False

    crud_approvals.approve(db, application.appId, "v1", admin.userId)
    assert crud_approvals.has_any_approved_version(db, application.appId) is True


@pytest.mark.integration
def test_approval_covers_only_its_commit(db):
    """A moved tag is not approved any more (.github#7 G)."""
    owner = _make_user(db)
    admin = _make_user(db, role=UserRole.ADMIN)
    application = _make_app(db, owner)
    crud_approvals.submit_version(db, application.appId, "v1", _sha("v1"))
    crud_approvals.approve(db, application.appId, "v1", admin.userId)

    assert crud_approvals.has_approved_version(db, application.appId, "v1", _sha("v1")) is True
    assert crud_approvals.has_approved_version(db, application.appId, "v1", _sha("other")) is False
    assert crud_approvals.has_approved_version(db, application.appId, "v1", "") is False
    assert crud_approvals.approved_commits(db, application.appId) == {"v1": _sha("v1")}


@pytest.mark.integration
def test_resubmitting_a_moved_tag_replaces_the_approval(db):
    """The tag points elsewhere now: submitting it again starts a new review."""
    owner = _make_user(db)
    admin = _make_user(db, role=UserRole.ADMIN)
    application = _make_app(db, owner)
    crud_approvals.submit_version(db, application.appId, "v1", _sha("v1"))
    crud_approvals.approve(db, application.appId, "v1", admin.userId)

    again = crud_approvals.submit_version(db, application.appId, "v1", _sha("moved"))

    assert again.status == AppVersionApprovalStatus.PENDING
    assert again.commit_sha == _sha("moved")
    assert crud_approvals.has_approved_version(db, application.appId, "v1", _sha("v1")) is False


@pytest.mark.integration
def test_unpinned_approvals_never_count(db):
    """Approvals from before the pinning have no commit and approve nothing."""
    owner = _make_user(db)
    admin = _make_user(db, role=UserRole.ADMIN)
    application = _make_app(db, owner)
    db.add(AppVersionApproval(
        appId=application.appId, version_tag="v1", status=AppVersionApprovalStatus.APPROVED,
    ))
    db.add(AppVersionApproval(
        appId=application.appId, version_tag="v2", status=AppVersionApprovalStatus.PENDING,
    ))
    db.commit()

    assert crud_approvals.has_any_approved_version(db, application.appId) is False
    assert crud_approvals.approved_commits(db, application.appId) == {}
    with pytest.raises(HTTPException) as exc:
        crud_approvals.approve(db, application.appId, "v2", admin.userId)
    assert exc.value.status_code == 409
    assert exc.value.detail["reason"] == "version_not_pinned"


@pytest.mark.integration
def test_pin_unpinned_records_the_current_commit(db):
    """The backfill pins entries whose tag still exists and leaves the rest."""
    owner = _make_user(db)
    application = _make_app(db, owner)
    db.add(AppVersionApproval(
        appId=application.appId, version_tag="v1", status=AppVersionApprovalStatus.APPROVED,
    ))
    db.add(AppVersionApproval(
        appId=application.appId, version_tag="gone", status=AppVersionApprovalStatus.APPROVED,
    ))
    db.commit()

    done = crud_approvals.pin_unpinned(db, application.appId, {"v1": _sha("v1")}.get)

    assert sorted(done, key=lambda d: d[0]) == [("gone", None), ("v1", _sha("v1"))]
    assert crud_approvals.approved_commits(db, application.appId) == {"v1": _sha("v1")}


@pytest.mark.integration
def test_withdraw_deletes_pending_row(db):
    """withdraw entfernt eine PENDING-Zeile vollstaendig."""
    owner = _make_user(db)
    application = _make_app(db, owner)
    crud_approvals.submit_version(db, application.appId, "v1", _sha("v1"))

    crud_approvals.withdraw(db, application.appId, "v1")

    remaining = (
        db.query(AppVersionApproval)
        .filter(
            AppVersionApproval.appId == application.appId,
            AppVersionApproval.version_tag == "v1",
        )
        .first()
    )
    assert remaining is None


@pytest.mark.integration
def test_withdraw_conflict_when_approved(db):
    """withdraw verweigert eine APPROVED-Zeile mit 409."""
    owner = _make_user(db)
    admin = _make_user(db, role=UserRole.ADMIN)
    application = _make_app(db, owner)
    crud_approvals.submit_version(db, application.appId, "v1", _sha("v1"))
    crud_approvals.approve(db, application.appId, "v1", admin.userId)

    with pytest.raises(HTTPException) as exc:
        crud_approvals.withdraw(db, application.appId, "v1")
    assert exc.value.status_code == 409


@pytest.mark.integration
def test_approve_sets_reviewer_and_timestamp(db):
    """approve flippt PENDING -> APPROVED und setzt reviewed_by/_at."""
    owner = _make_user(db)
    admin = _make_user(db, role=UserRole.ADMIN)
    application = _make_app(db, owner)
    crud_approvals.submit_version(db, application.appId, "v1", _sha("v1"))

    before = datetime.utcnow()
    approval = crud_approvals.approve(
        db, application.appId, "v1", admin.userId
    )

    assert approval.status == AppVersionApprovalStatus.APPROVED
    assert approval.reviewed_by == admin.userId
    assert approval.reviewed_at is not None
    assert approval.reviewed_at >= before
    assert approval.rejection_reason is None


@pytest.mark.integration
def test_approve_conflict_when_already_approved(db):
    """approve auf eine bereits APPROVED-Zeile gibt 409."""
    owner = _make_user(db)
    admin = _make_user(db, role=UserRole.ADMIN)
    application = _make_app(db, owner)
    crud_approvals.submit_version(db, application.appId, "v1", _sha("v1"))
    crud_approvals.approve(db, application.appId, "v1", admin.userId)

    with pytest.raises(HTTPException) as exc:
        crud_approvals.approve(db, application.appId, "v1", admin.userId)
    assert exc.value.status_code == 409


@pytest.mark.integration
def test_reject_requires_pending(db):
    """reject benoetigt PENDING; APPROVED gibt 409."""
    owner = _make_user(db)
    admin = _make_user(db, role=UserRole.ADMIN)
    application = _make_app(db, owner)
    crud_approvals.submit_version(db, application.appId, "v1", _sha("v1"))

    rejected = crud_approvals.reject(
        db, application.appId, "v1", admin.userId, "not good"
    )
    assert rejected.status == AppVersionApprovalStatus.REJECTED
    assert rejected.rejection_reason == "not good"
    assert rejected.reviewed_by == admin.userId
    assert rejected.reviewed_at is not None

    # Erneut submitten, danach approven, dann reject -> 409.
    crud_approvals.submit_version(db, application.appId, "v1", _sha("v1"))
    crud_approvals.approve(db, application.appId, "v1", admin.userId)
    with pytest.raises(HTTPException) as exc:
        crud_approvals.reject(
            db, application.appId, "v1", admin.userId, "too late"
        )
    assert exc.value.status_code == 409


@pytest.mark.integration
def test_revoke_requires_approved(db):
    """revoke benoetigt APPROVED; setzt Status auf REJECTED."""
    owner = _make_user(db)
    admin = _make_user(db, role=UserRole.ADMIN)
    application = _make_app(db, owner)
    crud_approvals.submit_version(db, application.appId, "v1", _sha("v1"))
    crud_approvals.approve(db, application.appId, "v1", admin.userId)

    before = datetime.utcnow()
    revoked = crud_approvals.revoke(
        db, application.appId, "v1", admin.userId, "policy violation"
    )

    assert revoked.status == AppVersionApprovalStatus.REJECTED
    assert revoked.rejection_reason == "policy violation"
    assert revoked.reviewed_by == admin.userId
    assert revoked.reviewed_at is not None
    assert revoked.reviewed_at >= before


@pytest.mark.integration
def test_revoke_conflict_when_pending(db):
    """revoke gegen eine PENDING-Zeile gibt 409."""
    owner = _make_user(db)
    admin = _make_user(db, role=UserRole.ADMIN)
    application = _make_app(db, owner)
    crud_approvals.submit_version(db, application.appId, "v1", _sha("v1"))

    with pytest.raises(HTTPException) as exc:
        crud_approvals.revoke(
            db, application.appId, "v1", admin.userId, "n/a"
        )
    assert exc.value.status_code == 409


@pytest.mark.integration
def test_terminal_ops_raise_404_for_unknown_version(db):
    """Unbekannte (appId, version_tag) -> 404 bei allen Terminal-Ops."""
    owner = _make_user(db)
    admin = _make_user(db, role=UserRole.ADMIN)
    application = _make_app(db, owner)

    with pytest.raises(HTTPException) as exc:
        crud_approvals.withdraw(db, application.appId, "ghost")
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        crud_approvals.approve(db, application.appId, "ghost", admin.userId)
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        crud_approvals.reject(
            db, application.appId, "ghost", admin.userId, "x"
        )
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        crud_approvals.revoke(
            db, application.appId, "ghost", admin.userId, "x"
        )
    assert exc.value.status_code == 404
