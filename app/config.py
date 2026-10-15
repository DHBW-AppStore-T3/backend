import base64
import binascii
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings

# Placeholders and keys that are public (committed in CI files, examples or
# old docs): never acceptable outside development (.github#7 O).
_PLACEHOLDER_SECRETS = frozenset({"", "change-me-in-production", "changeme", "secret", "test-secret"})
_PUBLIC_FERNET_KEYS = frozenset({
    "Q1PNlFd4It9oQPtjCcXcmB7wGDkY4w8KwpIRNSF4u7U=",  # CI test key (backend/worker)
    "MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE=",  # OpenAPI export placeholder
})


def _fernet_key_problem(key: str) -> str | None:
    """Why ``key`` is not a usable Fernet key, or None."""
    try:
        raw = base64.urlsafe_b64decode(key.encode())
    except (binascii.Error, ValueError):
        return "is not url-safe base64"
    if len(raw) != 32:
        return "does not decode to 32 bytes"
    return None


class Settings(BaseSettings):
    # "development" allows plain-HTTP cookies, placeholder secrets and the
    # test keys; anything else is production and refuses to start with
    # them (.github#7 O).
    APP_ENV: Literal["development", "production"] = "production"

    # Database
    DATABASE_URL: str

    # Celery broker (.github#5): the ``pgq`` transport on our own Postgres.
    # Empty means "the application database", i.e. ``pgq+`` + DATABASE_URL.
    CELERY_BROKER_URL: str = ""

    # Password of the worker's database role ``appstore_worker``, set by
    # ``python -m app.worker_db_role`` after the migrations. Only the
    # deployment step that runs that command needs it.
    WORKER_DB_PASSWORD: str = ""

    # Git: app repositories are public and read anonymously (.github#7 B).
    # Apps may only be registered from these hosts (comma-separated, exact
    # names); the worker checks the same list before it clones.
    TEMP_REPO_BASE_PATH: str = "/tmp/worker_repos"
    APP_GIT_ALLOWED_HOSTS: str = "github.com"

    # Keycloak — single source of truth for authentication.
    # KEYCLOAK_SERVER_URL is how the backend reaches Keycloak (keys, user
    # search); KEYCLOAK_ISSUER is the ``iss`` of the tokens, i.e. the public
    # URL the browser signs in at (default: derived from the server URL,
    # which only holds where both are the same).
    KEYCLOAK_SERVER_URL: str = "http://keycloak:8080"
    KEYCLOAK_REALM: str = "dhbw"
    KEYCLOAK_ISSUER: str = ""
    KEYCLOAK_CLIENT_ID: str = "appstore-backend"
    KEYCLOAK_CLIENT_SECRET: str = ""  # Set via environment variable
    # Access tokens must have been issued to one of these clients (``azp``,
    # comma-separated) and, if set, name OIDC_AUDIENCE in ``aud``
    # (.github#7 P). The realm adds the audience with a mapper.
    OIDC_ALLOWED_CLIENTS: str = "appstore-frontend"
    OIDC_AUDIENCE: str = ""

    # CORS
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    # Symmetric Fernet key shared with the worker. Used to encrypt OpenStack
    # credentials at rest and to seal the envelope shipped through Celery.
    # Generate: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
    CREDENTIAL_ENCRYPTION_KEY: str

    # Keystone URLs OpenStack credentials may point to, comma-separated
    # (e.g. ``https://keystone.example.org:5000/v3``; a trailing ``/v3`` is
    # ignored). API and worker send no login anywhere else (.github#7 C).
    # Empty: in development any https URL, in production none.
    OPENSTACK_ALLOWED_AUTH_URLS: str = ""

    # Pod apps (.github#4) -------------------------------------------------
    # Images in an appstore.yaml must come from these registries (prefixes,
    # D10: our own registry only) and every container stay within these
    # limits; the worker checks the same settings again.
    APP_IMAGE_REGISTRY_ALLOWLIST: str = "ghcr.io/dhbw-appstore-t3/"
    APP_MAX_CPU: str = "2"
    APP_MAX_MEMORY: str = "4Gi"
    APP_MAX_STORAGE: str = "20Gi"
    # At most this many workloads (one per team, or per person) in one
    # deployment, and optionally per owner across their live pod
    # deployments (0: no budget).
    K8S_MAX_WORKLOADS_PER_DEPLOYMENT: int = 60
    K8S_MAX_WORKLOADS_PER_OWNER: int = 0
    # Read the live status of pod deployments from the cluster (the API's
    # service account may read the dep-* namespaces, nothing else).
    K8S_STATUS_ENABLED: bool = False
    # App hosts are <workload>-<deployment>.<K8S_APP_DOMAIN or apps.<K8S_ZONE>>.
    K8S_ZONE: str = ""
    K8S_APP_DOMAIN: str = ""
    # The installation this API belongs to, where several share one cluster
    # (the pod deployer labels its namespaces with it): the orphan check
    # looks only at this installation's namespaces.
    K8S_ENVIRONMENT: str = ""

    # Public URL of the frontend; the LTI launch redirects there. No
    # trailing slash.
    APP_BASE_URL: str = "http://localhost:5173"

    # LTI 1.3 — OIDC-based launch (IMS LTI Advantage).
    # LTI13_CLIENT_ID and LTI13_DEPLOYMENT_ID are assigned by Moodle after
    # the External Tool is registered; copy them from the Moodle admin UI.
    # Generate LTI13_SESSION_SECRET with:
    #   python -c 'import secrets; print(secrets.token_hex(32))'
    LTI13_PLATFORM_ISSUER: str = "http://localhost:8081"
    LTI13_CLIENT_ID: str = ""
    LTI13_DEPLOYMENT_ID: str = ""
    LTI13_PLATFORM_JWKS_URL: str = "http://localhost:8081/mod/lti/certs.php"
    LTI13_REDIRECT_URI: str = "http://localhost:8000/lti13/launch"
    LTI13_SESSION_SECRET: str = "change-me-in-production"

    @property
    def is_development(self) -> bool:
        return self.APP_ENV == "development"

    @model_validator(mode="after")
    def _refuse_unsafe_production_settings(self) -> "Settings":
        """Fail at start-up instead of running with secrets anybody knows.

        In production: a well-formed Fernet key that is not a published test
        key; an explicit https token issuer; https browser origins only, no
        wildcard; and once LTI is configured, a real session secret of at
        least 32 characters.
        """
        problem = _fernet_key_problem(self.CREDENTIAL_ENCRYPTION_KEY)
        if problem:
            raise ValueError(f"CREDENTIAL_ENCRYPTION_KEY {problem}")
        if self.is_development:
            return self
        problems = []
        if self.CREDENTIAL_ENCRYPTION_KEY in _PUBLIC_FERNET_KEYS:
            problems.append("CREDENTIAL_ENCRYPTION_KEY is a published test key")
        if not self.KEYCLOAK_ISSUER.startswith("https://"):
            problems.append("KEYCLOAK_ISSUER must be set to the public https URL of the realm")
        if not self.oidc_allowed_clients:
            problems.append("OIDC_ALLOWED_CLIENTS is empty")
        bad_origins = [o for o in self.CORS_ORIGINS if o == "*" or not o.startswith("https://")]
        if bad_origins:
            problems.append(f"CORS_ORIGINS must be https origins, got {bad_origins}")
        if self.LTI13_CLIENT_ID and (
            self.LTI13_SESSION_SECRET in _PLACEHOLDER_SECRETS or len(self.LTI13_SESSION_SECRET) < 32
        ):
            problems.append("LTI13_SESSION_SECRET must be a random secret of at least 32 characters")
        if problems:
            raise ValueError("unsafe production configuration: " + "; ".join(problems))
        return self

    @property
    def keycloak_issuer(self) -> str:
        """The ``iss`` access tokens must carry."""
        if self.KEYCLOAK_ISSUER:
            return self.KEYCLOAK_ISSUER.rstrip("/")
        return f"{self.KEYCLOAK_SERVER_URL.rstrip('/')}/realms/{self.KEYCLOAK_REALM}"

    @property
    def keycloak_jwks_url(self) -> str:
        """Where the backend fetches the realm's signing keys (internal URL)."""
        return f"{self.KEYCLOAK_SERVER_URL.rstrip('/')}/realms/{self.KEYCLOAK_REALM}/protocol/openid-connect/certs"

    @property
    def oidc_allowed_clients(self) -> frozenset[str]:
        return frozenset(c.strip() for c in self.OIDC_ALLOWED_CLIENTS.split(",") if c.strip())

    @property
    def lti13_enabled(self) -> bool:
        """LTI launches and their session tokens are on once a real secret is set."""
        return bool(self.LTI13_SESSION_SECRET) and self.LTI13_SESSION_SECRET != "change-me-in-production"

    @property
    def celery_broker_url(self) -> str:
        """Broker URL for Celery; defaults to the application database."""
        if self.CELERY_BROKER_URL:
            return self.CELERY_BROKER_URL
        scheme, _, rest = self.DATABASE_URL.partition("://")
        return f"pgq+{scheme.split('+', 1)[0]}://{rest}"

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"


settings = Settings()
