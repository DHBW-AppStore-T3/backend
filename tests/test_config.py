from app.config import Settings


def _settings(**overrides):
    base = {
        "DATABASE_URL": "postgresql://user:pass@localhost/db",
        "CREDENTIAL_ENCRYPTION_KEY": "test-key",
    }
    base.update(overrides)
    return Settings(**base)


def test_lti13_settings_have_defaults():
    settings = _settings()
    assert settings.LTI13_PLATFORM_ISSUER == "http://localhost:8081"
    assert settings.LTI13_CLIENT_ID == ""
    assert settings.LTI13_DEPLOYMENT_ID == ""
    assert settings.LTI13_PLATFORM_JWKS_URL == "http://localhost:8081/mod/lti/certs.php"
    assert settings.LTI13_REDIRECT_URI == "http://localhost:8000/lti13/launch"
    assert settings.LTI13_SESSION_SECRET == "change-me-in-production"
    assert settings.DEV_MODE is False


def test_handoff_secret_has_default():
    settings = _settings()
    assert settings.HANDOFF_SESSION_SECRET == "change-me-in-production"
