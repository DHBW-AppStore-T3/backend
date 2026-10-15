"""Settings: defaults, and the refusal to start with unsafe production values (.github#7 O)."""
import pytest
from pydantic import ValidationError

from app.config import Settings

GOOD_KEY = "kZ1V3mEJqvGQ7yVxN7kT9mH4cP0sR2aB6dF8gJ1lK3o="  # random, unpublished
CI_KEY = "Q1PNlFd4It9oQPtjCcXcmB7wGDkY4w8KwpIRNSF4u7U="


def _settings(**overrides):
    base = {
        "DATABASE_URL": "postgresql://user:pass@localhost/db",
        "CREDENTIAL_ENCRYPTION_KEY": CI_KEY,
        "APP_ENV": "development",
    }
    base.update(overrides)
    return Settings(**base)


def _production(**overrides):
    base = {
        "APP_ENV": "production",
        "CREDENTIAL_ENCRYPTION_KEY": GOOD_KEY,
        "KEYCLOAK_ISSUER": "https://appstore.example/realms/dhbw",
        "CORS_ORIGINS": ["https://appstore.example"],
    }
    base.update(overrides)
    return _settings(**base)


def test_lti13_settings_have_defaults():
    settings = _settings()
    assert settings.LTI13_PLATFORM_ISSUER == "http://localhost:8081"
    assert settings.LTI13_CLIENT_ID == ""
    assert settings.LTI13_DEPLOYMENT_ID == ""
    assert settings.LTI13_PLATFORM_JWKS_URL == "http://localhost:8081/mod/lti/certs.php"
    assert settings.LTI13_REDIRECT_URI == "http://localhost:8000/lti13/launch"
    assert settings.LTI13_SESSION_SECRET == "change-me-in-production"
    assert settings.lti13_enabled is False


def test_production_is_the_default_environment():
    assert Settings.model_fields["APP_ENV"].default == "production"


def test_a_safe_production_configuration_starts():
    settings = _production()
    assert settings.is_development is False
    assert settings.keycloak_issuer == "https://appstore.example/realms/dhbw"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"CREDENTIAL_ENCRYPTION_KEY": CI_KEY}, "published test key"),
        ({"KEYCLOAK_ISSUER": ""}, "KEYCLOAK_ISSUER"),
        ({"KEYCLOAK_ISSUER": "http://keycloak:8080/realms/dhbw"}, "KEYCLOAK_ISSUER"),
        ({"CORS_ORIGINS": ["*"]}, "CORS_ORIGINS"),
        ({"CORS_ORIGINS": ["http://appstore.example"]}, "CORS_ORIGINS"),
        ({"OIDC_ALLOWED_CLIENTS": " , "}, "OIDC_ALLOWED_CLIENTS"),
        ({"LTI13_CLIENT_ID": "moodle-client"}, "LTI13_SESSION_SECRET"),
        ({"LTI13_CLIENT_ID": "moodle-client", "LTI13_SESSION_SECRET": "short"}, "LTI13_SESSION_SECRET"),
    ],
)
def test_production_refuses_unsafe_values(overrides, message):
    with pytest.raises(ValidationError, match=message):
        _production(**overrides)


def test_lti_with_a_real_secret_is_fine():
    settings = _production(LTI13_CLIENT_ID="moodle-client", LTI13_SESSION_SECRET="x" * 48)
    assert settings.lti13_enabled is True


@pytest.mark.parametrize("key", ["test-key", "not base64 !!", "c2hvcnQ="])
def test_a_malformed_fernet_key_is_refused_everywhere(key):
    with pytest.raises(ValidationError, match="CREDENTIAL_ENCRYPTION_KEY"):
        _settings(CREDENTIAL_ENCRYPTION_KEY=key)


def test_development_accepts_the_test_key_and_http():
    settings = _settings(KEYCLOAK_SERVER_URL="http://localhost:8080")
    assert settings.keycloak_issuer == "http://localhost:8080/realms/dhbw"
    assert settings.keycloak_jwks_url == "http://localhost:8080/realms/dhbw/protocol/openid-connect/certs"
    assert settings.oidc_allowed_clients == frozenset({"appstore-frontend"})


def test_no_git_token_and_no_handoff_secret():
    fields = Settings.model_fields
    assert "GIT_ACCESS_TOKEN" not in fields
    assert "HANDOFF_SESSION_SECRET" not in fields
    assert "DEV_MODE" not in fields
    assert not any(name.startswith("SMTP_") for name in fields)
