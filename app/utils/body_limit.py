"""Refuse request bodies over a size limit, before the framework parses them.

FastAPI reads and JSON-parses a whole body before it runs any dependency, so
before this limit existed a request with a multi-gigabyte body was parsed
(and held in memory) before the login or the rate limit was looked at. The
frontend's nginx caps ``/api`` at 16 MiB, but the self-service-ui's Caddy
reaches the API without a cap, and so does anything else inside the cluster.
The limit therefore lives here, where every path ends up.

The limit is counted on the bytes that arrive, not on ``Content-Length``
alone: a chunked request announces no length.

A request whose ``Content-Length`` is over the limit is answered with 413
without reading anything. One that turns out to be too long while it is read
raises a 413 ``HTTPException`` from ``receive``, which the framework answers.
"""

from __future__ import annotations

import json

from fastapi import HTTPException, status

TOO_LARGE = "request_body_too_large"


def _detail(limit: int) -> dict:
    return {"code": TOO_LARGE, "limit_bytes": limit, "message": "The request body is too large"}


class BodySizeLimit:
    """ASGI middleware: 413 ``request_body_too_large`` for a body over ``max_bytes``.

    Plain ASGI rather than ``BaseHTTPMiddleware``, so streamed responses (the
    live task stream) pass untouched.
    """

    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self.max_bytes <= 0:
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > self.max_bytes
            except ValueError:
                too_big = False  # not a number: the server rejects it before it gets here
            if too_big:
                await self._refuse(send)
                return

        received = 0

        async def counting_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=_detail(self.max_bytes),
                    )
            return message

        await self.app(scope, counting_receive, send)

    async def _refuse(self, send) -> None:
        body = json.dumps({"detail": _detail(self.max_bytes)}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    # The unread body is still on the connection: do not reuse it.
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
