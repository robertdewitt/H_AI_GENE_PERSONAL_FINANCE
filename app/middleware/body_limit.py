"""Bound the request body before anything parses it.

The upload route's own cap (``copy_with_limit``) governs the copy from the
parsed upload into ``uploads/``. It runs too late to stop the threat it was
written for: FastAPI resolves ``File(...)`` by parsing the whole multipart
body first, so by the time the handler executes the entire upload has
already been spooled — and rolled to disk once it passes a few hundred KB.
A 60 MB post against a 25 MB cap was measured fully on disk before the cap
saw a byte.

This middleware sits below that, at the ASGI layer, and refuses the body
itself:

* a declared ``Content-Length`` over the limit is rejected before the client
  sends the body at all;
* for a chunked request, which declares no length, the bytes are counted as
  they stream and the request is abandoned the moment the limit is passed.

Only methods that carry a body are examined, so GET traffic is untouched.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


class BodyTooLarge(Exception):
    """Raised inside the receive channel once the limit is passed."""


class BodySizeLimitMiddleware:
    """ASGI middleware capping the size of any request body."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def _reject(self, send) -> None:
        limit_mb = self.max_bytes // (1024 * 1024)
        body = (
            f'{{"detail":"Request body exceeds the {limit_mb} MB limit."}}'
        ).encode()
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in _BODY_METHODS:
            return await self.app(scope, receive, send)

        # Declared length: refuse before the body is transferred.
        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                try:
                    declared = int(value)
                except (TypeError, ValueError):
                    break
                if declared > self.max_bytes:
                    log.warning(
                        "rejected %s %s: declared %d bytes over the %d limit",
                        scope["method"], scope.get("path", ""),
                        declared, self.max_bytes,
                    )
                    return await self._reject(send)
                break

        # No declared length (chunked): count while it streams.
        received = 0
        started = False

        async def counting_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise BodyTooLarge
            return message

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except BodyTooLarge:
            log.warning(
                "rejected %s %s: body passed the %d byte limit while streaming",
                scope["method"], scope.get("path", ""), self.max_bytes,
            )
            if not started:
                await self._reject(send)
            # A response already began, so the connection is the only signal
            # left — let it close rather than appending to a sent body.
