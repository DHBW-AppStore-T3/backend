"""
Teams-API tests (``backend/app/routers/teams.py``).

Every request is decided by the rights on the team's deployment
(.github#7 F, security review H-1, M-2):

* reading a team needs the owner view of its deployment or membership in
  that team; the list is per deployment (``deployment_id`` required) and
  filtered the same way;
* creating, renaming, deleting a team and changing its members needs the
  operate right (owner or admin) — any other teacher gets 403;
* members must exist and be in at most one team of the deployment.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app as fastapi_app
from app.models import (
    App,
    Deployment,
    Team,
    User,
    UserRole,
    UserToTeam,
)
from app.utils.keycloak_auth import get_current_user_keycloak
from tests.conftest import TestingSessionLocal


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _make_deployment(db, owner: User) -> Deployment:
    """Persist ein App+Deployment-Paar für ``owner`` und gib das Deployment zurück.

    Wird gebraucht, weil ``Team.deploymentId`` NOT NULL ist.
    """
    app_row = App(
        appId=uuid.uuid4(),
        name=f"App-{uuid.uuid4().hex[:6]}",
        git_link="https://example.com/acme/repo.git",
        is_private=False,
        userId=owner.userId,
    )
    db.add(app_row)
    db.commit()
    db.refresh(app_row)

    deployment = Deployment(
        deploymentId=uuid.uuid4(),
        name=f"Dep-{uuid.uuid4().hex[:6]}",
        userId=owner.userId,
        appId=app_row.appId,
    )
    db.add(deployment)
    db.commit()
    db.refresh(deployment)
    return deployment


def _make_team(db, *, name: str, deployment: Deployment) -> Team:
    team = Team(
        teamId=uuid.uuid4(),
        name=name,
        deploymentId=deployment.deploymentId,
    )
    db.add(team)
    db.commit()
    db.refresh(team)
    return team


def _add_member(db, team: Team, user: User) -> UserToTeam:
    link = UserToTeam(
        userToTeamId=uuid.uuid4(),
        userId=user.userId,
        teamId=team.teamId,
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    return link


def _override(user: User) -> TestClient:
    """Setze Auth- und DB-Override für ``user`` und liefere TestClient."""

    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    fastapi_app.dependency_overrides[get_current_user_keycloak] = lambda: user
    fastapi_app.dependency_overrides[get_db] = override_get_db
    return TestClient(fastapi_app)


def _other_teacher(db) -> User:
    user = User(
        userId=uuid.uuid4(), keycloak_id=f"kc-{uuid.uuid4()}", email=f"{uuid.uuid4().hex[:6]}@dhbw.de",
        username="other", role=UserRole.TEACHER,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _call(user, method, url, **kwargs):
    try:
        with _override(user) as client:
            return getattr(client, method)(url, **kwargs)
    finally:
        fastapi_app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /teams/
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_list_teams_needs_a_deployment(db, mock_admin):
    assert _call(mock_admin, "get", "/teams/").status_code == 422


@pytest.mark.integration
def test_list_teams_owner_and_admin_see_all_teams_of_the_deployment(db, mock_admin, mock_user):
    dep = _make_deployment(db, mock_user)
    _make_team(db, name="A", deployment=dep)
    _make_team(db, name="B", deployment=dep)
    for user in (mock_user, mock_admin):
        response = _call(user, "get", f"/teams/?deployment_id={dep.deploymentId}")
        assert response.status_code == 200
        assert {t["name"] for t in response.json()} == {"A", "B"}


@pytest.mark.integration
def test_list_teams_member_sees_only_their_team(db, mock_user, mock_student):
    dep = _make_deployment(db, mock_user)
    member_team = _make_team(db, name="MemberTeam", deployment=dep)
    _make_team(db, name="OtherTeam", deployment=dep)
    _add_member(db, member_team, mock_student)

    response = _call(mock_student, "get", f"/teams/?deployment_id={dep.deploymentId}")

    assert response.status_code == 200
    assert [t["name"] for t in response.json()] == ["MemberTeam"]


@pytest.mark.integration
def test_list_teams_of_a_foreign_deployment_403(db, mock_user, mock_student):
    """Neither students nor unrelated teachers browse other deployments' rosters."""
    dep = _make_deployment(db, mock_user)
    _make_team(db, name="Secret", deployment=dep)
    for user in (mock_student, _other_teacher(db)):
        assert _call(user, "get", f"/teams/?deployment_id={dep.deploymentId}").status_code == 403


# ---------------------------------------------------------------------------
# GET /teams/{team_id}
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_get_team_member_ok(db, mock_user, mock_student):
    dep = _make_deployment(db, mock_user)
    team = _make_team(db, name="ReadableTeam", deployment=dep)
    _add_member(db, team, mock_student)

    response = _call(mock_student, "get", f"/teams/{team.teamId}")

    assert response.status_code == 200
    assert response.json()["name"] == "ReadableTeam"


@pytest.mark.integration
def test_get_team_owner_ok(db, mock_user):
    dep = _make_deployment(db, mock_user)
    team = _make_team(db, name="Owned", deployment=dep)
    assert _call(mock_user, "get", f"/teams/{team.teamId}").status_code == 200


@pytest.mark.integration
def test_get_team_non_member_403(db, mock_user, mock_student):
    dep = _make_deployment(db, mock_user)
    team = _make_team(db, name="NonMemberTeam", deployment=dep)
    other = _make_team(db, name="Other", deployment=dep)
    _add_member(db, other, mock_student)  # a member of another team of it

    assert _call(mock_student, "get", f"/teams/{team.teamId}").status_code == 403
    assert _call(_other_teacher(db), "get", f"/teams/{team.teamId}").status_code == 403
    assert _call(mock_student, "get", f"/teams/{uuid.uuid4()}").status_code == 404


# ---------------------------------------------------------------------------
# POST /teams/
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_create_team_owner_ok(db, mock_user, mock_student):
    dep = _make_deployment(db, mock_user)
    payload = {"name": "NewTeam", "deploymentId": str(dep.deploymentId), "userIds": [str(mock_student.userId)]}

    response = _call(mock_user, "post", "/teams/", json=payload)

    assert response.status_code == 201
    assert response.json()["deploymentId"] == str(dep.deploymentId)


@pytest.mark.integration
def test_create_team_on_someone_elses_deployment_403(db, mock_user, mock_student):
    dep = _make_deployment(db, mock_user)
    payload = {"name": "Forbidden", "deploymentId": str(dep.deploymentId), "userIds": []}
    for user in (mock_student, _other_teacher(db)):
        response = _call(user, "post", "/teams/", json=payload)
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "deployment_operate_forbidden"


@pytest.mark.integration
def test_create_team_member_rules(db, mock_user, mock_student):
    dep = _make_deployment(db, mock_user)
    taken = _make_team(db, name="Taken", deployment=dep)
    _add_member(db, taken, mock_student)

    unknown = _call(mock_user, "post", "/teams/", json={
        "name": "X", "deploymentId": str(dep.deploymentId), "userIds": [str(uuid.uuid4())],
    })
    twice = _call(mock_user, "post", "/teams/", json={
        "name": "Y", "deploymentId": str(dep.deploymentId), "userIds": [str(mock_student.userId)],
    })

    assert unknown.status_code == 422
    assert unknown.json()["detail"]["reason"] == "unknown_team_member"
    assert twice.status_code == 422
    assert twice.json()["detail"]["reason"] == "member_in_several_teams"


# ---------------------------------------------------------------------------
# PUT / DELETE /teams/{team_id}
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_rename_and_delete_by_owner_and_admin(db, mock_admin, mock_user):
    dep = _make_deployment(db, mock_user)
    team = _make_team(db, name="Before", deployment=dep)

    # Admins operate every deployment, like its owner.
    assert _call(mock_admin, "put", f"/teams/{team.teamId}", json={"name": "Admin"}).status_code == 200
    assert _call(mock_user, "put", f"/teams/{team.teamId}", json={"name": "Owner"}).status_code == 200
    db.expire_all()
    assert db.get(Team, team.teamId).name == "Owner"
    assert _call(mock_user, "delete", f"/teams/{team.teamId}").status_code == 204


@pytest.mark.integration
def test_rename_and_delete_by_others_403(db, mock_user, mock_student):
    dep = _make_deployment(db, mock_user)
    team = _make_team(db, name="Mine", deployment=dep)
    _add_member(db, team, mock_student)  # even a member may not rename it
    for user in (mock_student, _other_teacher(db)):
        assert _call(user, "put", f"/teams/{team.teamId}", json={"name": "Hijack"}).status_code == 403
        assert _call(user, "delete", f"/teams/{team.teamId}").status_code == 403
    db.expire_all()
    assert db.get(Team, team.teamId).name == "Mine"


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_add_and_remove_member_owner_ok(db, mock_user, mock_student):
    dep = _make_deployment(db, mock_user)
    team = _make_team(db, name="T", deployment=dep)

    assert _call(mock_user, "post", f"/teams/{team.teamId}/users/{mock_student.userId}").status_code == 204
    assert _call(mock_user, "post", f"/teams/{team.teamId}/users/{mock_student.userId}").status_code == 400
    assert _call(mock_user, "delete", f"/teams/{team.teamId}/users/{mock_student.userId}").status_code == 204
    assert _call(mock_user, "delete", f"/teams/{team.teamId}/users/{mock_student.userId}").status_code == 404


@pytest.mark.integration
def test_add_member_refuses_a_second_team_and_strangers(db, mock_user, mock_student):
    dep = _make_deployment(db, mock_user)
    first = _make_team(db, name="First", deployment=dep)
    second = _make_team(db, name="Second", deployment=dep)
    _add_member(db, first, mock_student)

    twice = _call(mock_user, "post", f"/teams/{second.teamId}/users/{mock_student.userId}")
    stranger = _call(_other_teacher(db), "post", f"/teams/{second.teamId}/users/{mock_student.userId}")
    unknown = _call(mock_user, "post", f"/teams/{second.teamId}/users/{uuid.uuid4()}")

    assert twice.status_code == 422
    assert stranger.status_code == 403
    assert unknown.status_code == 422
