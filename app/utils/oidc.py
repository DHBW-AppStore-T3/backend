"""Keycloak access tokens, verified with PyJWT (.github#7 P, security review H-9, L-1, L-2).

Replaces the python-jose check, which took the realm key once per process
(a key rotation broke every sign-in until a restart), skipped the audience
and never looked at the issuer, so a token of any client of the realm was
accepted. Modelled on dhbw-appstore's ``oidc.py``:

- **Signature** with the realm's JWKS (``…/protocol/openid-connect/certs``,
  reached internally). Keys are cached for an hour and re-fetched when a
  token names an unknown ``kid``, so a rotation needs no restart.
  Asymmetric algorithms only.
- **Issuer** must be ``KEYCLOAK_ISSUER`` (the public URL the browser signs
  in at; the backend may reach Keycloak under another name).
- **Who the token is for:** ``azp`` must be one of ``OIDC_ALLOWED_CLIENTS``
  (the frontend client), and with ``OIDC_AUDIENCE`` set ``aud`` must contain
  it. ``typ`` must be ``Bearer``: ID and refresh tokens are not access
  tokens.
- **The identity provider is not a startup dependency.** Keys are fetched
  when the first token arrives. A token that cannot be judged because
  Keycloak does not answer gets **503**, not 401, so the browser does not
  loop into a login that cannot work.
"""

from __future__ import annotations

import logging
import threading

import jwt
from jwt import PyJWKClient, PyJWKClientConnectionError, PyJWKClientError

logger = logging.getLogger(__name__)

# Asymmetric only: an HS256 token would be checked against a shared secret,
# and "none" is never acceptable.
ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"]
_FETCH_TIMEOUT_SECONDS = 10


class InvalidToken(Exception):
    """Malformed, expired, for another client, or wrongly signed: 401."""


class IdentityProviderUnavailable(Exception):
    """The keys to judge the token could not be fetched: 503."""


class OidcVerifier:
    """Verifies access tokens of one issuer; keys fetched lazily and cached.

    One instance is shared by all requests (``keycloak_auth.get_verifier``).
    """

    def __init__(
        self,
        *,
        issuer: str,
        jwks_url: str,
        allowed_clients: frozenset[str],
        audience: str | None = None,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.jwks_url = jwks_url
        self.allowed_clients = allowed_clients
        self.audience = audience or None
        self._jwks: PyJWKClient | None = None
        self._lock = threading.Lock()

    def _client(self) -> PyJWKClient:
        with self._lock:
            if self._jwks is None:
                self._jwks = PyJWKClient(
                    self.jwks_url, cache_keys=True, lifespan=3600, timeout=_FETCH_TIMEOUT_SECONDS
                )
            return self._jwks

    def verify(self, token: str) -> dict:
        """Check signature, ``exp``, issuer, ``azp``, audience and type; return the claims.

        Raises InvalidToken (401) or IdentityProviderUnavailable (503).
        """
        try:
            key = self._client().get_signing_key_from_jwt(token)
        except PyJWKClientConnectionError as e:
            raise IdentityProviderUnavailable(f"fetching the realm keys failed: {e}") from e
        except (PyJWKClientError, jwt.DecodeError) as e:
            # Unknown kid even after a refresh, or not a JWT at all.
            raise InvalidToken(str(e)) from e
        options = {"require": ["exp", "iat", "iss", "sub"], "verify_aud": self.audience is not None}
        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=ALGORITHMS,
                issuer=self.issuer,
                audience=self.audience,
                options=options,
            )
        except jwt.PyJWTError as e:
            raise InvalidToken(str(e)) from e
        if claims.get("typ", "Bearer") != "Bearer":
            raise InvalidToken(f"not an access token (typ={claims.get('typ')!r})")
        if claims.get("azp") not in self.allowed_clients:
            raise InvalidToken(f"token issued to client {claims.get('azp')!r}")
        return claims
