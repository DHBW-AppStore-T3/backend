"""Refuse state-changing requests a browser sends on behalf of another site (review S3).

Every host of the platform is under one zone and so on one *site*: the
AppStore, SSO, Moodle, the self-service-ui and every student workload
``<workload>-<id>.<zone>``. ``SameSite`` cookies do not separate them, and
the self-service-ui signs people in with a cookie (its oauth2-proxy adds the
bearer token to every request). A page a student app serves could make the
browser of a signed-in admin send ``POST /api/app-store/admin/apps/.../approve``
to the self-service-ui, and that request would carry the admin's session.

A write (any method but GET, HEAD, OPTIONS) is let through when

  - the browser says it comes from the page's own origin
    (``Sec-Fetch-Site: same-origin``, or ``none`` for a person's own action),
  - or its ``Origin`` is one of the UIs (``trusted_origins``),
  - or it has neither header: then it is no browser request, and only a
    browser adds a session cookie by itself.

A browser that sends no Fetch Metadata but an ``Origin`` of somewhere else is
refused when trusted origins are configured. Moodle's LTI 1.3 login and
launch are form POSTs from another host by design and are not checked.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Cross-site POSTs by design: the LTI platform's form posts.
EXEMPT_PREFIXES = ("/lti13/",)
SAME_ORIGIN = frozenset({"same-origin", "none"})


def normalise_origin(origin: str) -> str:
    """``https://Host/`` -> ``https://host``: origins compare case-insensitively, without a slash."""
    return origin.strip().rstrip("/").lower()


def is_allowed(
    method: str, path: str, sec_fetch_site: str | None, origin: str | None, trusted: frozenset[str]
) -> bool:
    """Whether a request with these headers may change state."""
    if method in SAFE_METHODS or path.startswith(EXEMPT_PREFIXES):
        return True
    if origin is not None and normalise_origin(origin) in trusted:
        return True
    if sec_fetch_site is not None:
        return sec_fetch_site.strip().lower() in SAME_ORIGIN
    return origin is None or not trusted


class CrossSiteWriteGuard:
    """ASGI middleware: 403 ``cross_site_request`` for a write :func:`is_allowed` refuses.

    Plain ASGI rather than ``BaseHTTPMiddleware``, so streamed responses (the
    live task stream) pass untouched.
    """

    def __init__(self, app, trusted_origins: Iterable[str] = ()) -> None:
        self.app = app
        self.trusted = frozenset(normalise_origin(o) for o in trusted_origins if o.strip())

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        site = headers.get(b"sec-fetch-site")
        origin = headers.get(b"origin")
        if is_allowed(
            scope["method"],
            scope.get("path", ""),
            site.decode("latin-1") if site is not None else None,
            origin.decode("latin-1") if origin is not None else None,
            self.trusted,
        ):
            await self.app(scope, receive, send)
            return
        body = json.dumps(
            {
                "detail": {
                    "code": "cross_site_request",
                    "message": "Changes are only accepted from the AppStore's own pages",
                }
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
