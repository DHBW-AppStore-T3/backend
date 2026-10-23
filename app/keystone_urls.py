"""Which Keystone URLs OpenStack credentials may point to (.github#7 C).

Shared by API and worker: both send a credential's login to its
``auth_url``, which is user input. Without an allowlist they would send it
wherever it points, internal addresses and the metadata service included,
and a fake Keystone could hand back a service catalogue of internal
endpoints. This file is identical in both repositories
(``tests/test_shared_files.py`` checks it).
"""

from __future__ import annotations

from urllib.parse import urlsplit


def normalised(url: str) -> str | None:
    """``https://host[:port]/path`` without trailing ``/`` or ``/v3``; None unless a plain https URL."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or not parts.hostname or parts.username or parts.password:
        return None
    if parts.query or parts.fragment:
        return None
    netloc = parts.hostname.lower() + (f":{port}" if port else "")
    path = parts.path.rstrip("/").removesuffix("/v3").rstrip("/")
    return f"https://{netloc}{path}"


def parse_allowlist(value: str) -> frozenset[str]:
    """A comma-separated allowlist setting, normalised; unusable entries are dropped."""
    return frozenset(n for n in (normalised(u) for u in value.split(",") if u.strip()) if n)


def auth_url_allowed(url: str | None, allowlist: str, *, development: bool) -> bool:
    """Whether a login may be sent to ``url``.

    Only plain https URLs on the allowlist. An empty allowlist allows any
    https URL in development and none in production (fail closed).
    """
    candidate = normalised(url or "")
    if candidate is None:
        return False
    allowed = parse_allowlist(allowlist)
    if not allowed:
        return development
    return candidate in allowed
