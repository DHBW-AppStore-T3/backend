"""Access-token verification with PyJWT (.github#7 P, security review H-9, L-1, L-2)."""
from __future__ import annotations

import base64
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jwt import PyJWKClientConnectionError

from app.utils import keycloak_auth
from app.utils.oidc import IdentityProviderUnavailable, InvalidToken, OidcVerifier

pytestmark = pytest.mark.unit

ISSUER = "https://appstore.example/realms/dhbw"


def _b64(n: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()


class _Keys:
    """A realm with rotating keys; stands in for PyJWKClient."""

    def __init__(self):
        self.private = {}
        self.calls = 0
        self.down = False

    def add(self, kid: str):
        self.private[kid] = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwk(self, kid: str) -> dict:
        numbers = self.private[kid].public_key().public_numbers()
        return {"kty": "RSA", "kid": kid, "alg": "RS256", "use": "sig", "n": _b64(numbers.n), "e": _b64(numbers.e)}

    def get_signing_key_from_jwt(self, token: str):
        self.calls += 1
        if self.down:
            raise PyJWKClientConnectionError("connection refused")
        kid = jwt.get_unverified_header(token).get("kid")
        if kid not in self.private:
            raise jwt.PyJWKClientError(f"Unable to find a signing key that matches: {kid!r}")
        return jwt.PyJWK(self.jwk(kid))

    def sign(self, kid: str, **claims) -> str:
        now = int(time.time())
        body = {"iss": ISSUER, "sub": "user-1", "azp": "appstore-frontend", "typ": "Bearer",
                "iat": now, "exp": now + 300, "aud": "account", **claims}
        body = {k: v for k, v in body.items() if v is not None}
        pem = self.private[kid].private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        return jwt.encode(body, pem, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def keys(monkeypatch):
    realm = _Keys()
    realm.add("k1")
    return realm


def _verifier(keys, audience=None) -> OidcVerifier:
    verifier = OidcVerifier(issuer=ISSUER + "/", jwks_url="http://keycloak:8080/certs",
                            allowed_clients=frozenset({"appstore-frontend"}), audience=audience)
    verifier._jwks = keys  # noqa: SLF001 - the fake stands in for PyJWKClient
    return verifier


def test_a_good_token_passes(keys):
    claims = _verifier(keys).verify(keys.sign("k1"))
    assert claims["sub"] == "user-1"


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://evil.example/realms/dhbw"},
        {"iss": "http://keycloak:8080/realms/dhbw"},
        {"azp": "some-other-client"},
        {"azp": None},
        {"typ": "ID"},
        {"typ": "Refresh"},
        {"exp": int(time.time()) - 10},
        {"sub": None},
    ],
)
def test_tokens_for_someone_else_or_of_the_wrong_kind_fail(keys, claims):
    with pytest.raises(InvalidToken):
        _verifier(keys).verify(keys.sign("k1", **claims))


def test_audience_is_checked_when_configured(keys):
    verifier = _verifier(keys, audience="appstore-backend")
    with pytest.raises(InvalidToken):
        verifier.verify(keys.sign("k1"))  # aud: account
    assert verifier.verify(keys.sign("k1", aud=["account", "appstore-backend"]))["sub"] == "user-1"


def test_hs256_with_the_public_key_as_secret_fails(keys):
    """The algorithm-confusion attack python-jose was vulnerable to."""
    public_pem = keys.private["k1"].public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    now = int(time.time())
    body = {"iss": ISSUER, "sub": "x", "azp": "appstore-frontend", "iat": now, "exp": now + 300}
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "kid": "k1", "typ": "JWT"}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(json.dumps(body).encode()).rstrip(b"=")
    import hashlib
    import hmac

    signature = base64.urlsafe_b64encode(
        hmac.new(public_pem, header + b"." + payload, hashlib.sha256).digest()
    ).rstrip(b"=")
    forged = (header + b"." + payload + b"." + signature).decode()
    with pytest.raises(InvalidToken):
        _verifier(keys).verify(forged)


def test_a_rotated_key_is_used_without_restart(keys):
    verifier = _verifier(keys)
    assert verifier.verify(keys.sign("k1"))
    keys.add("k2")  # Keycloak rotated; the verifier asks the key source per token
    assert verifier.verify(keys.sign("k2"))["sub"] == "user-1"
    assert keys.calls == 2


def test_garbage_is_an_invalid_token(keys):
    with pytest.raises(InvalidToken):
        _verifier(keys).verify("not-a-jwt")


def test_an_unreachable_realm_is_503_not_401(keys, monkeypatch):
    keys.down = True
    with pytest.raises(IdentityProviderUnavailable):
        _verifier(keys).verify(keys.sign("k1"))

    monkeypatch.setattr(keycloak_auth, "get_verifier", lambda: _verifier(keys))
    with pytest.raises(HTTPException) as exc:
        keycloak_auth.verify_keycloak_token_offline(keys.sign("k1"))
    assert exc.value.status_code == 503


def test_an_invalid_token_is_401(keys, monkeypatch):
    monkeypatch.setattr(keycloak_auth, "get_verifier", lambda: _verifier(keys))
    with pytest.raises(HTTPException) as exc:
        keycloak_auth.verify_keycloak_token_offline(keys.sign("k1", azp="other"))
    assert exc.value.status_code == 401
