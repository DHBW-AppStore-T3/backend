"""Symmetric encryption for at-rest credentials, Celery-envelope payloads and task results.

Two Fernet keys, both shared between the backend and the worker:

- `CREDENTIAL_ENCRYPTION_KEY`: the backend encrypts OpenStack credentials
  when storing them in Postgres and forwards the ciphertext (base64) through
  Celery, with the state token; the VM executor decrypts in-process.
- `RESULTS_ENCRYPTION_KEY`: workers seal a task's results with it
  (``tasks.outputs_enc``), the pod deployer having no other key. Without it
  the credential key seals them, as before; results sealed with either open.

Plaintext never leaves either container's memory.
"""
from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import settings


def _build_cipher(name: str, key: str) -> Fernet:
    if not key:
        raise RuntimeError(
            f"{name} is not set. Generate one with: "
            "python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'"
        )
    try:
        # Fernet validates the 32-byte url-safe-base64 key shape itself.
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as e:
        raise RuntimeError(f"{name} is malformed: {e}") from e


_cipher = _build_cipher("CREDENTIAL_ENCRYPTION_KEY", settings.CREDENTIAL_ENCRYPTION_KEY)

#: Task results (``app.task_contract``): sealed with the results key, opened
#: with it or, for results sealed before it existed, with the credential key.
results_cipher: Fernet | MultiFernet = (
    MultiFernet([_build_cipher("RESULTS_ENCRYPTION_KEY", settings.RESULTS_ENCRYPTION_KEY), _cipher])
    if settings.RESULTS_ENCRYPTION_KEY
    else _cipher
)


def encrypt(plaintext: str) -> bytes:
    """Encrypt a string. Returns Fernet ciphertext as bytes (store as BYTEA)."""
    return _cipher.encrypt(plaintext.encode("utf-8"))


def decrypt(token: bytes) -> str:
    """Decrypt Fernet ciphertext bytes. Raises InvalidToken on tampering / wrong key."""
    return _cipher.decrypt(token).decode("utf-8")


def encrypt_b64(plaintext: str) -> str:
    """Encrypt and base64-encode for JSON-safe transport (Celery args)."""
    return base64.b64encode(encrypt(plaintext)).decode("ascii")


def decrypt_b64(token_b64: str) -> str:
    """Inverse of `encrypt_b64`. Raises InvalidToken on bad input."""
    return decrypt(base64.b64decode(token_b64.encode("ascii")))


__all__ = ["results_cipher", "encrypt", "decrypt", "encrypt_b64", "decrypt_b64", "InvalidToken"]
