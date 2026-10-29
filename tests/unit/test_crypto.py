"""Unit-Tests fuer das symmetrische Verschluesselungs-Utility ``app.utils.crypto``."""
from __future__ import annotations

import base64
import importlib

import pytest
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings
from app.utils import crypto


@pytest.mark.unit
def test_encrypt_decrypt_roundtrip_returns_original_plaintext() -> None:
    """encrypt + decrypt liefert das urspruengliche UTF-8-Plaintext zurueck."""
    plaintext = "super-secret-password-123"
    token = crypto.encrypt(plaintext)
    assert isinstance(token, bytes)
    assert token != plaintext.encode("utf-8")
    assert crypto.decrypt(token) == plaintext


@pytest.mark.unit
def test_encrypt_b64_produces_ascii_string_and_roundtrips() -> None:
    """encrypt_b64 erzeugt einen ASCII/base64-sicheren String, der wieder dekodierbar ist."""
    plaintext = "queue-envelope-payload"
    token_b64 = crypto.encrypt_b64(plaintext)
    assert isinstance(token_b64, str)
    # Muss reine ASCII sein und sich als base64 dekodieren lassen.
    token_b64.encode("ascii")
    base64.b64decode(token_b64.encode("ascii"))
    assert crypto.decrypt_b64(token_b64) == plaintext


@pytest.mark.unit
def test_decrypt_raises_invalid_token_on_tampered_ciphertext() -> None:
    """Manipulierte Ciphertext-Bytes muessen InvalidToken ausloesen."""
    token = bytearray(crypto.encrypt("payload"))
    # Flip ein Byte in der Mitte des Tokens, um die HMAC zu brechen.
    token[len(token) // 2] ^= 0xFF
    with pytest.raises(InvalidToken):
        crypto.decrypt(bytes(token))


@pytest.mark.unit
def test_decrypt_b64_raises_on_garbage_input() -> None:
    """decrypt_b64 muss bei voellig invalider Eingabe einen Fehler werfen."""
    # base64-dekodierbarer, aber kein gueltiger Fernet-Token.
    garbage_b64 = base64.b64encode(b"this-is-not-a-fernet-token-at-all").decode("ascii")
    with pytest.raises((InvalidToken, ValueError)):
        crypto.decrypt_b64(garbage_b64)


@pytest.mark.unit
def test_non_ascii_payload_roundtrips_correctly() -> None:
    """Non-ASCII Plaintext (Umlaute, Emoji) ueberlebt einen vollen Roundtrip."""
    plaintext = "héllo wörld 🚀"
    assert crypto.decrypt(crypto.encrypt(plaintext)) == plaintext
    assert crypto.decrypt_b64(crypto.encrypt_b64(plaintext)) == plaintext


@pytest.mark.unit
def test_reload_with_malformed_key_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ein malformter Fernet-Key muss beim Import zu einem RuntimeError fuehren."""
    original_key = settings.CREDENTIAL_ENCRYPTION_KEY
    try:
        monkeypatch.setattr(settings, "CREDENTIAL_ENCRYPTION_KEY", "not-a-valid-key", raising=False)
        with pytest.raises(RuntimeError) as exc:
            importlib.reload(crypto)
        assert "malformed" in str(exc.value).lower()
    finally:
        monkeypatch.setattr(settings, "CREDENTIAL_ENCRYPTION_KEY", original_key, raising=False)
        importlib.reload(crypto)


@pytest.mark.unit
def test_reload_with_empty_key_raises_runtime_error_with_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ein leerer CREDENTIAL_ENCRYPTION_KEY muss eine hilfreiche RuntimeError-Message liefern."""
    original_key = settings.CREDENTIAL_ENCRYPTION_KEY
    try:
        monkeypatch.setattr(settings, "CREDENTIAL_ENCRYPTION_KEY", "", raising=False)
        with pytest.raises(RuntimeError) as exc:
            importlib.reload(crypto)
        message = str(exc.value)
        assert "CREDENTIAL_ENCRYPTION_KEY" in message
        assert "Fernet.generate_key" in message
    finally:
        monkeypatch.setattr(settings, "CREDENTIAL_ENCRYPTION_KEY", original_key, raising=False)
        importlib.reload(crypto)


@pytest.mark.unit
def test_reload_with_freshly_generated_key_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ein frisch generierter Fernet-Key laesst sich problemlos laden und ver-/entschluesseln."""
    original_key = settings.CREDENTIAL_ENCRYPTION_KEY
    fresh_key = Fernet.generate_key().decode()
    try:
        monkeypatch.setattr(settings, "CREDENTIAL_ENCRYPTION_KEY", fresh_key, raising=False)
        reloaded = importlib.reload(crypto)
        assert reloaded.decrypt(reloaded.encrypt("ok")) == "ok"
    finally:
        monkeypatch.setattr(settings, "CREDENTIAL_ENCRYPTION_KEY", original_key, raising=False)
        importlib.reload(crypto)


@pytest.fixture
def reload_with_results_key(monkeypatch: pytest.MonkeyPatch):
    """Baut die Ciphers des Moduls mit einem anderen RESULTS_ENCRYPTION_KEY neu; danach wieder wie vorher."""
    original = settings.RESULTS_ENCRYPTION_KEY

    def reload(key: str):
        monkeypatch.setattr(settings, "RESULTS_ENCRYPTION_KEY", key, raising=False)
        return importlib.reload(crypto)

    yield reload
    monkeypatch.setattr(settings, "RESULTS_ENCRYPTION_KEY", original, raising=False)
    importlib.reload(crypto)


@pytest.mark.unit
def test_results_are_sealed_with_their_own_key_and_old_ones_still_open(reload_with_results_key) -> None:
    """Ergebnisse werden mit RESULTS_ENCRYPTION_KEY versiegelt; mit dem Credential-Key versiegelte bleiben lesbar."""
    results_key = Fernet.generate_key()
    sealed_before = Fernet(settings.CREDENTIAL_ENCRYPTION_KEY.encode()).encrypt(b"old outputs")

    module = reload_with_results_key(results_key.decode())

    sealed = module.results_cipher.encrypt(b"outputs")
    assert Fernet(results_key).decrypt(sealed) == b"outputs"
    with pytest.raises(InvalidToken):
        Fernet(settings.CREDENTIAL_ENCRYPTION_KEY.encode()).decrypt(sealed)
    assert module.results_cipher.decrypt(sealed_before) == b"old outputs"


@pytest.mark.unit
def test_without_a_results_key_the_credential_key_seals_as_before(reload_with_results_key) -> None:
    """Ohne RESULTS_ENCRYPTION_KEY versiegelt der Credential-Key wie bisher."""
    module = reload_with_results_key("")

    sealed = module.results_cipher.encrypt(b"outputs")
    assert Fernet(settings.CREDENTIAL_ENCRYPTION_KEY.encode()).decrypt(sealed) == b"outputs"
