import time
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

from app.services import lti13_service

pytestmark = pytest.mark.unit


@pytest.fixture
def rsa_key():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key


def _jwk_dict(private_key, kid="test-kid"):
    public_numbers = private_key.public_key().public_numbers()

    def _b64(n: int) -> str:
        from jose.utils import base64url_encode

        length = (n.bit_length() + 7) // 8
        return base64url_encode(n.to_bytes(length, "big")).decode()

    return {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": _b64(public_numbers.n),
        "e": _b64(public_numbers.e),
    }


def _sign(private_key, claims, kid="test-kid"):
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": kid})


def _base_claims(
    nonce="expected-nonce", audience="client-123", issuer="https://moodle.example.com"
):
    now = int(time.time())
    return {
        "iss": issuer,
        "aud": audience,
        "nonce": nonce,
        "exp": now + 300,
        "iat": now,
        "email": "student@example.com",
        "name": "Ada Lovelace",
        "https://purl.imsglobal.org/spec/lti/claim/message_type": "LtiResourceLinkRequest",
        "https://purl.imsglobal.org/spec/lti/claim/roles": [],
        "https://purl.imsglobal.org/spec/lti/claim/context": {"title": "Course 101"},
    }


def test_validate_id_token_rejects_wrong_audience(monkeypatch, rsa_key):
    monkeypatch.setattr(
        lti13_service,
        "fetch_platform_jwks",
        lambda _jwks_url: [_jwk_dict(rsa_key)],
    )
    token = _sign(rsa_key, _base_claims(audience="someone-else"))

    with pytest.raises(ValueError):
        lti13_service.validate_id_token(
            id_token=token,
            client_id="client-123",
            expected_nonce="expected-nonce",
            platform_issuer="https://moodle.example.com",
            jwks_url="https://moodle.example.com/mod/lti/certs.php",
        )


def test_validate_id_token_rejects_expired_nonce(monkeypatch, rsa_key):
    monkeypatch.setattr(
        lti13_service,
        "fetch_platform_jwks",
        lambda _jwks_url: [_jwk_dict(rsa_key)],
    )
    token = _sign(rsa_key, _base_claims(nonce="a-different-nonce"))

    with pytest.raises(ValueError, match="[Nn]once"):
        lti13_service.validate_id_token(
            id_token=token,
            client_id="client-123",
            expected_nonce="expected-nonce",
            platform_issuer="https://moodle.example.com",
            jwks_url="https://moodle.example.com/mod/lti/certs.php",
        )


def test_issue_and_verify_session_token_roundtrip():
    token = lti13_service.issue_session_token("student@example.com", "test-secret")
    email = lti13_service.verify_session_token(token, "test-secret")
    assert email == "student@example.com"


def test_verify_session_token_rejects_wrong_secret():
    token = lti13_service.issue_session_token("student@example.com", "test-secret")
    with pytest.raises(ValueError):
        lti13_service.verify_session_token(token, "wrong-secret")


def test_verify_session_token_rejects_garbage():
    with pytest.raises(ValueError):
        lti13_service.verify_session_token("not-a-jwt-at-all", "test-secret")


def test_fetch_platform_jwks_fetches_over_http(monkeypatch):
    lti13_service._jwks_cache.clear()
    mock_response = MagicMock()
    mock_response.json.return_value = {"keys": [{"kid": "abc"}]}
    with patch.object(lti13_service.http_requests, "get", return_value=mock_response) as mock_get:
        keys = lti13_service.fetch_platform_jwks("https://moodle.example.com/certs.php")

    mock_get.assert_called_once_with("https://moodle.example.com/certs.php", timeout=10)
    mock_response.raise_for_status.assert_called_once()
    assert keys == [{"kid": "abc"}]


def test_fetch_platform_jwks_reuses_cache_within_ttl(monkeypatch):
    lti13_service._jwks_cache.clear()
    mock_response = MagicMock()
    mock_response.json.return_value = {"keys": [{"kid": "abc"}]}
    with patch.object(lti13_service.http_requests, "get", return_value=mock_response) as mock_get:
        lti13_service.fetch_platform_jwks("https://moodle.example.com/certs.php")
        lti13_service.fetch_platform_jwks("https://moodle.example.com/certs.php")

    mock_get.assert_called_once()


def test_validate_id_token_rejects_unparseable_header(monkeypatch, rsa_key):
    monkeypatch.setattr(
        lti13_service, "fetch_platform_jwks", lambda _jwks_url: [_jwk_dict(rsa_key)]
    )
    with pytest.raises(ValueError, match="header"):
        lti13_service.validate_id_token(
            id_token="not-a-jwt",
            client_id="client-123",
            expected_nonce="expected-nonce",
            platform_issuer="https://moodle.example.com",
            jwks_url="https://moodle.example.com/mod/lti/certs.php",
        )


def test_validate_id_token_retries_jwks_on_unknown_kid_then_fails(monkeypatch, rsa_key):
    calls = {"count": 0}

    def _fetch(_jwks_url):
        calls["count"] += 1
        return [_jwk_dict(rsa_key, kid="a-different-kid")]

    monkeypatch.setattr(lti13_service, "fetch_platform_jwks", _fetch)
    token = _sign(rsa_key, _base_claims(), kid="test-kid")

    with pytest.raises(ValueError, match="No matching JWK"):
        lti13_service.validate_id_token(
            id_token=token,
            client_id="client-123",
            expected_nonce="expected-nonce",
            platform_issuer="https://moodle.example.com",
            jwks_url="https://moodle.example.com/mod/lti/certs.php",
        )

    assert calls["count"] == 2  # first attempt + one retry after cache invalidation


def test_validate_id_token_rejects_wrong_message_type(monkeypatch, rsa_key):
    monkeypatch.setattr(
        lti13_service, "fetch_platform_jwks", lambda _jwks_url: [_jwk_dict(rsa_key)]
    )
    claims = _base_claims()
    claims["https://purl.imsglobal.org/spec/lti/claim/message_type"] = "LtiDeepLinkingRequest"
    token = _sign(rsa_key, claims)

    with pytest.raises(ValueError, match="message type"):
        lti13_service.validate_id_token(
            id_token=token,
            client_id="client-123",
            expected_nonce="expected-nonce",
            platform_issuer="https://moodle.example.com",
            jwks_url="https://moodle.example.com/mod/lti/certs.php",
        )


def test_validate_id_token_accepts_valid_token(monkeypatch, rsa_key):
    monkeypatch.setattr(
        lti13_service, "fetch_platform_jwks", lambda _jwks_url: [_jwk_dict(rsa_key)]
    )
    token = _sign(rsa_key, _base_claims())

    claims = lti13_service.validate_id_token(
        id_token=token,
        client_id="client-123",
        expected_nonce="expected-nonce",
        platform_issuer="https://moodle.example.com",
        jwks_url="https://moodle.example.com/mod/lti/certs.php",
    )

    assert claims["email"] == "student@example.com"


def test_extract_role_maps_instructor_variants():
    assert (
        lti13_service._extract_role(
            {
                "https://purl.imsglobal.org/spec/lti/claim/roles": [
                    "http://purl.imsglobal.org/vocab/lis/v2/membership#Instructor"
                ]
            }
        )
        == "instructor"
    )


def test_extract_role_defaults_to_student():
    assert lti13_service._extract_role({}) == "student"


def test_extract_user_info_falls_back_email_for_missing_name():
    info = lti13_service.extract_user_info(
        {
            "email": "no-name@example.com",
            "https://purl.imsglobal.org/spec/lti/claim/roles": [],
        }
    )
    assert info["name"] == "no-name@example.com"
    assert info["role"] == "student"
    assert info["course"] == ""
