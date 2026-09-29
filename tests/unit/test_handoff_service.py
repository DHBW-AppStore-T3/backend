import pytest

from app.services import handoff_service

pytestmark = pytest.mark.unit


def test_issue_and_verify_roundtrip():
    token = handoff_service.issue_handoff_token("student@example.com", "test-secret")
    email = handoff_service.verify_handoff_token(token, "test-secret")
    assert email == "student@example.com"


def test_verify_rejects_expired_token():
    token = handoff_service.issue_handoff_token(
        "student@example.com", "test-secret", ttl_seconds=-1
    )
    with pytest.raises(ValueError):
        handoff_service.verify_handoff_token(token, "test-secret")


def test_verify_rejects_wrong_secret():
    token = handoff_service.issue_handoff_token("student@example.com", "test-secret")
    with pytest.raises(ValueError):
        handoff_service.verify_handoff_token(token, "wrong-secret")


def test_verify_rejects_garbage_token():
    with pytest.raises(ValueError):
        handoff_service.verify_handoff_token("not-a-jwt-at-all", "test-secret")


def test_verify_rejects_token_with_wrong_type_claim():
    from jose import jwt

    now_claims_token = jwt.encode(
        {"sub": "student@example.com", "type": "lti_session", "iat": 0, "exp": 9999999999},
        "test-secret",
        algorithm="HS256",
    )
    with pytest.raises(ValueError, match="type"):
        handoff_service.verify_handoff_token(now_claims_token, "test-secret")


def test_issue_handoff_token_uses_default_ttl():
    token = handoff_service.issue_handoff_token("student@example.com", "test-secret")
    email = handoff_service.verify_handoff_token(token, "test-secret")
    assert email == "student@example.com"
