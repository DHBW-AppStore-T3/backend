
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str

    # Celery (optional - only needed for API runtime, not for migrations)
    CELERY_BROKER_URL: str = "amqp://admin:admin@rabbitmq:5672/"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/0"

    # Git
    TEMP_REPO_BASE_PATH: str = "/tmp/worker_repos"
    GIT_ACCESS_TOKEN: str = ""  # Token for HTTPS git authentication

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

    # SMTP (Gmail). Required for the post-deploy notification mails.
    # Use a Google "App password" (the regular password won't work with
    # 2FA enabled).
    #
    # SMTP_ENABLED is the explicit kill-switch — set it to False to turn
    # mail delivery into a no-op even when credentials are populated. It
    # lives separately from the credentials so operators can keep the
    # app-password in .env while disabling mail in dev/CI, and so the
    # resend-access endpoint can distinguish "we chose not to send"
    # (HTTP 503) from "SMTP refused" (HTTP 502).
    SMTP_ENABLED: bool = False
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = ""
    SMTP_FROM_NAME: str = "Click-n-Deploy"

    # Public URL the deployment detail page is reachable under, used in
    # the owner-summary mail to deep-link back into the UI. No trailing
    # slash. Falls back to the first CORS origin in dev.
    APP_BASE_URL: str = "http://localhost:5173"

    # LTI 1.1 — shared credentials between Moodle and this backend.
    # Pick any string for the key; use a long random value for the secret.
    LTI_CONSUMER_KEY: str = "appstore-lti-key"
    LTI_CONSUMER_SECRET: str = "appstore-lti-secret"

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

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"


settings = Settings()
