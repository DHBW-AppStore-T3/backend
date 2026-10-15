from pydantic_settings import BaseSettings


class Settings(BaseSettings):
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

    # Keycloak — single source of truth for authentication
    KEYCLOAK_SERVER_URL: str = "http://keycloak:8080"
    KEYCLOAK_REALM: str = "dhbw"
    KEYCLOAK_CLIENT_ID: str = "appstore-backend"
    KEYCLOAK_CLIENT_SECRET: str = ""  # Set via environment variable

    # CORS
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    # Symmetric Fernet key shared with the worker. Used to encrypt OpenStack
    # credentials at rest and to seal the envelope shipped through Celery.
    # Generate: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
    CREDENTIAL_ENCRYPTION_KEY: str

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

    # Dev mode: accept X-Dev-User header in place of a Keycloak token.
    # Never enable in production.
    DEV_MODE: bool = False

    # Self-service-ui handoff (POST /handoff/mint). Own secret, independent
    # of LTI13_SESSION_SECRET — a compromised LTI secret must not also open
    # the self-service handoff path.
    HANDOFF_SESSION_SECRET: str = "change-me-in-production"

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
