import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

from app.services import lti13_service


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
