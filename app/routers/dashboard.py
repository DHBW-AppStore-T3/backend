from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (
    App,
    AppVersionApproval,
    AppVersionApprovalStatus,
    Course,
    Deployment,
    Team,
    User,
    UserRole,
    UserToDeployment,
    UserToTeam,
)
from app.utils.keycloak_auth import get_current_user_keycloak

router = APIRouter()


class DashboardStatsResponse(BaseModel):
    deployments: int
    apps: int
    courses: int


@router.get("/stats", response_model=DashboardStatsResponse)
def get_dashboard_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """
    Aggregate counts for the dashboard KPI strip.

    Cheap DB-only aggregates.

    Deployments counter MUST mirror the visibility rules of
    ``GET /deployments`` so the KPI matches what the user actually sees
    on the Deployments page:

      * Teacher/Admin: deployments they own.
      * Student:       deployments they own OR are a team member of OR
                       have a direct ``UserToDeployment`` mapping for.

    Apps counter MUST mirror the role-branched visibility of
    ``GET /apps`` (see ``routers/apps.py``):

      * Admin: every non-deleted app — exactly what
        ``crud_apps.get_apps`` returns when called without
        ``user_id``. Admin keeps the plattform-wide view.
      * Teacher/Student: own apps (regardless of ``is_private`` /
        approval state) OR public apps (``is_private = False``)
        with at least one APPROVED version — mirrors
        ``crud_apps.get_visible_apps``. Teacher gets the student-style
        filter.

    Soft-deleted rows (``deleted_at IS NOT NULL``) are excluded on both
    counters — same as the list endpoints.
    """
    deployments_q = (
        db.query(func.count(Deployment.deploymentId))
        .filter(Deployment.deleted_at.is_(None))
    )

    if current_user.role in (UserRole.TEACHER, UserRole.ADMIN):
        deployments_q = deployments_q.filter(
            Deployment.userId == current_user.userId
        )
    else:
        # Owner OR team member OR direct mapping. Mirrors
        # ``crud_deployments.get_deployments(member_user_id=...)``.
        member_team_ids = db.query(UserToTeam.teamId).filter(
            UserToTeam.userId == current_user.userId
        )
        member_deployment_ids_via_teams = db.query(Team.deploymentId).filter(
            Team.teamId.in_(member_team_ids)
        )
        member_deployment_ids_direct = db.query(
            UserToDeployment.deploymentId
        ).filter(UserToDeployment.userId == current_user.userId)
        deployments_q = deployments_q.filter(
            or_(
                Deployment.userId == current_user.userId,
                Deployment.deploymentId.in_(member_deployment_ids_via_teams),
                Deployment.deploymentId.in_(member_deployment_ids_direct),
            )
        )

    deployments_total = deployments_q.scalar() or 0

    # Apps visible to the user — role-branched, same gate as the
    # ``/apps`` endpoint at ``routers/apps.py``. Admin sees everything
    # non-deleted (mirrors ``crud_apps.get_apps``); everyone else
    # sees own + public-approved (mirrors ``crud_apps.get_visible_apps``).
    if current_user.role == UserRole.ADMIN:
        apps_total = (
            db.query(func.count(App.appId))
            .filter(App.deleted_at.is_(None))
            .scalar()
        ) or 0
    else:
        approved_app_ids = (
            db.query(AppVersionApproval.appId)
            .filter(AppVersionApproval.status == AppVersionApprovalStatus.APPROVED)
            .distinct()
            .scalar_subquery()
        )
        apps_total = (
            db.query(func.count(App.appId))
            .filter(App.deleted_at.is_(None))
            .filter(
                or_(
                    App.userId == current_user.userId,
                    (App.is_private == False)  # noqa: E712
                    & App.appId.in_(approved_app_ids),
                )
            )
            .scalar()
        ) or 0

    courses_total = db.query(func.count(Course.courseId)).scalar() or 0

    return DashboardStatsResponse(
        deployments=deployments_total,
        apps=apps_total,
        courses=courses_total,
    )
