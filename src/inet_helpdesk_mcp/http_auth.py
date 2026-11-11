"""Client authentication for the HTTP transports.

The MCP endpoint itself does not know who calls it: with a HelpDesk token in
the server's configuration everyone who reaches the port acts as that
HelpDesk user. ``INET_CLIENT_KEY`` closes that gap with a shared secret which
every request has to carry - in the ``X-Inet-Mcp-Key`` header or, when the
server runs with ``--ignore-client-auth`` and the ``Authorization`` header is
therefore free, as ``Authorization: Bearer <key>``.
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any, Awaitable, Callable, MutableMapping

logger = logging.getLogger(__name__)

CLIENT_KEY_HEADER = "x-inet-mcp-key"

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class ClientKeyMiddleware:
    """ASGI middleware that answers 401 unless the request carries the key."""

    def __init__(self, app: ASGIApp, key: str, *, accept_authorization: bool = False) -> None:
        if not key:
            raise ValueError("The client key must not be empty.")
        self._app = app
        self._key = key.encode("utf-8")
        self._accept_authorization = accept_authorization

    def _presented(self, scope: Scope) -> list[bytes]:
        candidates: list[bytes] = []
        for name, value in scope.get("headers") or []:
            name = name.lower()
            if name == CLIENT_KEY_HEADER.encode("ascii"):
                candidates.append(value.strip())
            elif self._accept_authorization and name == b"authorization":
                scheme, _, credentials = value.strip().partition(b" ")
                if scheme.lower() == b"bearer":
                    candidates.append(credentials.strip())
        return candidates

    def _authorized(self, scope: Scope) -> bool:
        # Compare every candidate in constant time, without stopping early.
        matches = [hmac.compare_digest(candidate, self._key) for candidate in self._presented(scope)]
        return any(matches)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._authorized(scope):
            await self._app(scope, receive, send)
            return

        client = scope.get("client")
        logger.warning(
            "Rejected a request without a valid client key from %s.",
            client[0] if client else "an unknown address",
        )
        body = json.dumps(
            {"error": "unauthorized", "message": "A valid client key is required."}
        ).encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        if self._accept_authorization:
            headers.append((b"www-authenticate", b"Bearer"))
        await send({"type": "http.response.start", "status": 401, "headers": headers})
        await send({"type": "http.response.body", "body": body})
