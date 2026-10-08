"""Symmetric encryption for at-rest credentials, Celery-envelope payloads and task results.

The Fernet key (`CREDENTIAL_ENCRYPTION_KEY`) is shared between the backend
and the worker. The backend encrypts when storing in Postgres and forwards
the ciphertext (base64) through Celery; the worker decrypts in-process.
The worker seals a task's Terraform outputs and state with the same key
(``tasks.outputs_enc``). Plaintext never leaves either container's memory.
"""
from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


def _build_cipher() -> Fernet:
    key = settings.CREDENTIAL_ENCRYPTION_KEY
    if not key:
        raise RuntimeError(
            "CREDENTIAL_ENCRYPTION_KEY is not set. Generate one with: "
            "python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'"
        )
    try:
        # Fernet validates the 32-byte url-safe-base64 key shape itself.
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as e:
        raise RuntimeError(f"CREDENTIAL_ENCRYPTION_KEY is malformed: {e}") from e


_cipher = _build_cipher()

#: The shared Fernet instance, for sealing/opening task results
#: (``app.task_contract``).
cipher = _cipher


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


__all__ = ["cipher", "encrypt", "decrypt", "encrypt_b64", "decrypt_b64", "InvalidToken"]
