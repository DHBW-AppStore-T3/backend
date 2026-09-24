import pytest

from app.services import handoff_service


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
