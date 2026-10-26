"""A seal on each uploaded file, so that the worker can tell what the API checked from what the database holds.

The API validates an upload (``deployment_input``) and stores its bytes in ``deployments.userInputVar``. Anyone
who can write that row, or a code path that stores a file without the validation, would get its bytes mounted
into a pod unchecked. The API therefore stores an HMAC next to every file; the worker recomputes it before it
mounts the file. A bare hash would not help: whoever can change the bytes can change the hash too. The key is
derived from ``RESULTS_ENCRYPTION_KEY`` (the credential key where that is not set): the one key the pod deployer
has, since it never gets the credential key, and the database has neither.

The worker has the same function (``app/upload_mac.py`` there); keep them in step.
"""

from __future__ import annotations

import hashlib
import hmac

from app.config import settings


def _key() -> bytes:
    secret = settings.RESULTS_ENCRYPTION_KEY or settings.CREDENTIAL_ENCRYPTION_KEY
    return hashlib.sha256(b"appstore-upload-mac-v1\0" + secret.encode()).digest()


def sign(variable: str, slot: str, name: str, data: bytes) -> str:
    """Hex HMAC-SHA256 over variable, slot, stored file name and the bytes."""
    head = "\0".join((variable, slot, name)).encode() + b"\0\0"
    return hmac.new(_key(), head + data, hashlib.sha256).hexdigest()
