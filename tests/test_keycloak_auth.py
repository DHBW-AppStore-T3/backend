from unittest.mock import patch

from fastapi.security import HTTPAuthorizationCredentials

from app.config import settings
from app.models import User, UserRole
from app.services import lti13_service
from app.utils.keycloak_auth import get_current_user_keycloak, sync_user_from_keycloak


def _credentials(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _kc(user_id="kc-1", email="teacher@example.com", roles=()):
    return {"id": user_id, "email": email, "username": email, "roles": list(roles)}


def test_get_current_user_accepts_lti_session_token(db):
    user = User(
        email="lti-user@example.com",
        username="lti-user@example.com",
        role=UserRole.STUDENT,
    )
    db.add(user)
    db.commit()

    with patch.object(settings, "LTI13_SESSION_SECRET", "test-secret"):
        token = lti13_service.issue_session_token(str(user.userId), "test-secret")
        result = get_current_user_keycloak(credentials=_credentials(token), db=db)

    assert result.email == "lti-user@example.com"


def test_get_current_user_falls_through_to_keycloak_on_invalid_lti_token(db):
    with (
        patch.object(settings, "LTI13_SESSION_SECRET", "test-secret"),
        patch(
            "app.utils.keycloak_auth.verify_keycloak_token_offline",
            return_value={
                "sub": "keycloak-sub-1",
                "email": "kc-user@example.com",
                "preferred_username": "kc-user",
                "realm_access": {"roles": []},
            },
        ),
    ):
        result = get_current_user_keycloak(credentials=_credentials("not-a-valid-lti-token"), db=db)

    assert result.email == "kc-user@example.com"


def test_a_keycloak_login_after_an_lti_first_user_links_their_account(db):
    """F3: the LTI launch created the account by e-mail; the first SSO login used to insert a second row (500)."""
    lti_user = User(
        email="Teacher@Example.com",
        username="teacher",
        role=UserRole.TEACHER,
        lti_role=UserRole.TEACHER,
        lti_issuer="https://moodle.example.com",
        lti_subject="7",
    )
    db.add(lti_user)
    db.commit()

    user = sync_user_from_keycloak(db, _kc())

    assert user.userId == lti_user.userId
    assert db.query(User).count() == 1
    assert (user.keycloak_id, user.email, user.role) == (
        "kc-1",
        "teacher@example.com",
        UserRole.TEACHER,
    )


def test_a_moodle_teacher_stays_a_teacher_after_a_keycloak_login(db):
    """Keycloak-federated users carry no realm role; the teacher role comes from Moodle."""
    db.add(
        User(
            keycloak_id="kc-1",
            email="teacher@example.com",
            username="t",
            role=UserRole.TEACHER,
            lti_role=UserRole.TEACHER,
        )
    )
    db.commit()

    assert sync_user_from_keycloak(db, _kc()).role == UserRole.TEACHER


def test_removing_admin_in_keycloak_demotes_at_the_next_request(db):
    """Review S8: roles used to only ever go up."""
    assert sync_user_from_keycloak(db, _kc(roles=["admin"])).role == UserRole.ADMIN

    user = sync_user_from_keycloak(db, _kc(roles=["teacher"]))
    assert user.role == UserRole.TEACHER
    user = sync_user_from_keycloak(db, _kc(roles=[]))
    assert user.role == UserRole.STUDENT


def test_an_admin_grant_survives_keycloak_logins(db):
    from app.utils import roles

    user = sync_user_from_keycloak(db, _kc())
    roles.grant(user, UserRole.ADMIN)
    db.commit()

    assert sync_user_from_keycloak(db, _kc()).role == UserRole.ADMIN


def test_an_address_another_account_holds_is_not_taken_over(db):
    db.add(User(email="other@example.com", username="other", role=UserRole.STUDENT))
    db.commit()
    sync_user_from_keycloak(db, _kc(email="me@example.com"))

    user = sync_user_from_keycloak(db, _kc(email="other@example.com"))

    assert user.email == "me@example.com"
    assert db.query(User).count() == 2


def test_a_user_search_result_does_not_demote_whoever_it_finds(db):
    """Search results (Keycloak admin API) carry no realm roles: no statement about the role."""
    sync_user_from_keycloak(db, _kc(roles=["admin"]))
    found = {
        "id": "kc-1",
        "email": "teacher@example.com",
        "username": "teacher@example.com",
        "firstName": "T",
    }

    user = sync_user_from_keycloak(db, found)

    assert user.role == UserRole.ADMIN and user.keycloak_role == UserRole.ADMIN
    assert user.firstName == "T"


def test_the_user_search_keeps_roles(db, client, monkeypatch):
    from app.routers import users as users_router

    sync_user_from_keycloak(db, _kc(user_id="kc-admin", email="admin@example.com", roles=["admin"]))
    monkeypatch.setattr(
        users_router,
        "search_keycloak_users",
        lambda _query, _limit: [
            {"id": "kc-admin", "username": "admin@example.com", "email": "admin@example.com"}
        ],
    )

    response = client.get("/users/search", params={"query": "adm"})

    assert response.status_code == 200, response.text
    assert response.json()[0]["role"] == "admin"
