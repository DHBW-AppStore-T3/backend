"""Teams of a deployment, checked against that deployment (.github#7 F).

Every request is decided by the rights on the team's deployment, never by
role alone (security review H-1, M-2):

- read a team: the owner view of its deployment (owner, admin,
  course-teacher of a student owner), or membership in that team;
- list teams: one deployment at a time (``deployment_id``), filtered the
  same way;
- create, rename, delete a team or change its members: the operate right
  on the deployment (owner or admin), so nobody else decides who gets
  access to a deployment.

Members must exist and be in at most one team of the deployment.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.crud import deployments as crud_deployments
from app.crud import teams as crud_teams
from app.crud import users as crud_users
from app.database import get_db
from app.models import Team, User, UserToTeam
from app.schemas import TeamCreate, TeamResponse, TeamUpdate, TeamWithMembers
from app.utils.capabilities import (
    can_view_deployment_owner,
    ensure_operate_deployment,
    ensure_view_deployment,
)
from app.utils.keycloak_auth import get_current_user_keycloak

router = APIRouter()


def _deployment_or_404(db: Session, deployment_id: UUID):
    deployment = crud_deployments.get_deployment(db, deployment_id)
    if not deployment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")
    return deployment


def _team_and_deployment(db: Session, team_id: UUID):
    team = crud_teams.get_team(db, team_id)
    if not team:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")
    return team, _deployment_or_404(db, team.deploymentId)


def _is_member(db: Session, team_id: UUID, user: User) -> bool:
    return (
        db.query(UserToTeam.userToTeamId)
        .filter(UserToTeam.teamId == team_id, UserToTeam.userId == user.userId)
        .first()
    ) is not None


def _may_read_team(db: Session, user: User, team: Team, deployment) -> bool:
    return can_view_deployment_owner(user, deployment, db) or _is_member(db, team.teamId, user)


def _ensure_new_members(db: Session, deployment_id: UUID, user_ids: list[UUID], *, except_team: UUID | None = None):
    """422 unless every user exists and is in no other team of the deployment."""
    for user_id in user_ids:
        if crud_users.get_user(db, user_id) is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"reason": "unknown_team_member", "user_id": str(user_id)},
            )
        other = (
            db.query(UserToTeam.teamId)
            .join(Team, Team.teamId == UserToTeam.teamId)
            .filter(Team.deploymentId == deployment_id, UserToTeam.userId == user_id)
        )
        if except_team is not None:
            other = other.filter(Team.teamId != except_team)
        if other.first() is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"reason": "member_in_several_teams", "user_id": str(user_id)},
            )


# ----------------------------------------------------------------
# LIST TEAMS OF ONE DEPLOYMENT
# ----------------------------------------------------------------
@router.get("/", response_model=list[TeamResponse])
def list_teams(
    deployment_id: UUID,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak)
):
    """The teams of ``deployment_id`` the caller may read (all of them with
    the owner view, otherwise their own). 403 without access to the
    deployment."""
    deployment = _deployment_or_404(db, deployment_id)
    ensure_view_deployment(current_user, deployment, db)
    teams = crud_teams.get_teams(db, skip=skip, limit=limit, deployment_id=deployment_id)
    return [t for t in teams if _may_read_team(db, current_user, t, deployment)]


# ----------------------------------------------------------------
# GET TEAM BY ID
# ----------------------------------------------------------------
@router.get("/{team_id}", response_model=TeamWithMembers)
def get_team(
    team_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak)
):
    """A team with its members: owner view of the deployment or membership."""
    team, deployment = _team_and_deployment(db, team_id)
    if not _may_read_team(db, current_user, team, deployment):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "team_view_forbidden"})
    return team


# ----------------------------------------------------------------
# CREATE TEAM (operate right on the deployment)
# ----------------------------------------------------------------
@router.post("/", response_model=TeamResponse, status_code=status.HTTP_201_CREATED)
def create_team(
    team: TeamCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak)
):
    """Create a team on a deployment the caller may operate."""
    deployment = _deployment_or_404(db, team.deploymentId)
    ensure_operate_deployment(current_user, deployment, db)
    _ensure_new_members(db, deployment.deploymentId, team.userIds)
    return crud_teams.create_team(db, team)


# ----------------------------------------------------------------
# UPDATE TEAM (operate right on the deployment)
# ----------------------------------------------------------------
@router.put("/{team_id}", response_model=TeamResponse)
def update_team(
    team_id: UUID,
    team_update: TeamUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak)
):
    """Rename a team of a deployment the caller may operate."""
    _team, deployment = _team_and_deployment(db, team_id)
    ensure_operate_deployment(current_user, deployment, db)
    return crud_teams.update_team(db, team_id, team_update)


# ----------------------------------------------------------------
# DELETE TEAM (operate right on the deployment)
# ----------------------------------------------------------------
@router.delete("/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_team(
    team_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak)
):
    """Delete a team of a deployment the caller may operate."""
    _team, deployment = _team_and_deployment(db, team_id)
    ensure_operate_deployment(current_user, deployment, db)
    crud_teams.delete_team(db, team_id)
    return None


# ----------------------------------------------------------------
# ADD USER TO TEAM (operate right on the deployment)
# ----------------------------------------------------------------
@router.post("/{team_id}/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def add_user_to_team(
    team_id: UUID,
    user_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak)
):
    """Add an existing user, who is in no other team of the deployment."""
    team, deployment = _team_and_deployment(db, team_id)
    ensure_operate_deployment(current_user, deployment, db)
    _ensure_new_members(db, deployment.deploymentId, [user_id], except_team=team.teamId)
    if not crud_teams.add_user_to_team(db, team_id, user_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User already in team"
        )
    return None


# ----------------------------------------------------------------
# REMOVE USER FROM TEAM (operate right on the deployment)
# ----------------------------------------------------------------
@router.delete("/{team_id}/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_user_from_team(
    team_id: UUID,
    user_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak)
):
    """Remove a user from a team of a deployment the caller may operate."""
    _team, deployment = _team_and_deployment(db, team_id)
    ensure_operate_deployment(current_user, deployment, db)
    if not crud_teams.remove_user_from_team(db, team_id, user_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not in team"
        )
    return None
