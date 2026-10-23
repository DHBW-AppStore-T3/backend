"""Give the worker's database role its login: ``python -m app.worker_db_role``.

Migration ``5e1f0c2a9b7d`` creates ``appstore_worker`` without a login and
grants it only what the worker needs (the queue, appending task events, the
result columns of ``tasks``). The deployment runs this command after
``alembic upgrade head``; it sets ``LOGIN`` and the password from
``WORKER_DB_PASSWORD``. Re-running it rotates the password.

The password is sent as a SCRAM-SHA-256 verifier, never in plain text, so
it cannot end up in the server's statement log.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sys

import psycopg
from psycopg import sql

from app.config import settings
from app.pgq import libpq_url

ROLE = "appstore_worker"
SCRAM_ITERATIONS = 4096


def scram_sha256_verifier(password: str, *, salt: bytes | None = None, iterations: int = SCRAM_ITERATIONS) -> str:
    """The verifier Postgres stores for ``password`` (RFC 5802/7677, as ``pg_authid.rolpassword``)."""
    salt = salt if salt is not None else os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()

    def b64(value: bytes) -> str:
        return base64.b64encode(value).decode("ascii")

    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def set_worker_login(database_url: str, password: str) -> None:
    """``ALTER ROLE appstore_worker LOGIN PASSWORD <verifier>``."""
    with psycopg.connect(libpq_url(database_url), autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (ROLE,)).fetchone()
        if not exists:
            raise SystemExit(f"role {ROLE} does not exist - run 'alembic upgrade head' first")
        conn.execute(
            sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                sql.Identifier(ROLE), sql.Literal(scram_sha256_verifier(password))
            )
        )


def main() -> int:
    password = settings.WORKER_DB_PASSWORD
    if len(password) < 16:
        print("WORKER_DB_PASSWORD must be set (at least 16 characters)", file=sys.stderr)
        return 2
    set_worker_login(settings.DATABASE_URL, password)
    print(f"role {ROLE}: login enabled, password set")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
