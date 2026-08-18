"""Thin async wrapper around the i-net HelpDesk Ticket Web-API.

Documentation: https://docs.inetsoftware.de/helpdesk/help/webapi.ticket/p/ticket-web-api
"""

from __future__ import annotations

import asyncio
import json as jsonlib
import logging
import os
import ssl
from collections import OrderedDict
from typing import Any, Callable, Mapping, Sequence

import httpx

from .config import RequestConfig
from .errors import ApiError, TransportError

logger = logging.getLogger(__name__)

USER_AGENT = "inet-helpdesk-mcp"

#: Status codes that mean "the HelpDesk is busy right now", not "your request was
#: wrong" - only these are retried, and only for GET.
RETRY_STATUS_CODES = frozenset({502, 503, 504})

#: Base of the exponential backoff between two attempts, in seconds.
RETRY_BACKOFF_SECONDS = 0.5

#: One attachment as it is handed to the Web-API: the JSON description plus the
#: raw bytes that are uploaded as ``attachment<N>``.
AttachmentUpload = tuple[dict[str, Any], bytes]


def build_ssl_verify(verify: str | bool) -> ssl.SSLContext | bool:
    """Turn a CA bundle path into the SSL context httpx expects.

    ``verify=<path>`` is deprecated in httpx, so the context is built here: the
    file (or directory) named by ``verify`` becomes the only trusted CA store.
    Booleans are passed through unchanged.
    """
    if not isinstance(verify, str):
        return verify
    if os.path.isdir(verify):
        return ssl.create_default_context(capath=verify)
    return ssl.create_default_context(cafile=verify)


class HelpdeskClient:
    """Talks to one i-net HelpDesk instance on behalf of one user."""

    def __init__(
        self,
        config: RequestConfig,
        *,
        timeout: float = 30.0,
        verify: str | bool = True,
        retries: int = 0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._retries = max(0, retries)
        self._client = client or httpx.AsyncClient(
            base_url=config.base_url,
            timeout=timeout,
            verify=build_ssl_verify(verify),
            follow_redirects=True,
            auth=(
                (config.username, config.password)
                if config.username and config.password
                else None
            ),
            headers={"User-Agent": USER_AGENT, **config.auth_headers()},
        )

    async def __aenter__(self) -> "HelpdeskClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- low level ---------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any | None = None,
        files: Any | None = None,
    ) -> Any:
        # Only GET is repeated: a second POST would create a second ticket or a
        # second editing step, which is worse than the error it tries to fix.
        attempts = self._retries + 1 if method.upper() == "GET" else 1
        for attempt in range(attempts):
            last = attempt + 1 == attempts
            try:
                response = await self._client.request(
                    method,
                    path,
                    params={k: v for k, v in (params or {}).items() if v is not None},
                    json=json,
                    files=files,
                )
            except httpx.HTTPError as exc:
                if last:
                    raise TransportError(
                        f"Could not reach the i-net HelpDesk at {self._config.base_url}: {exc}"
                    ) from exc
                await self._backoff(attempt, method, path, str(exc))
                continue

            if response.status_code in RETRY_STATUS_CODES and not last:
                await self._backoff(attempt, method, path, f"HTTP {response.status_code}")
                continue
            break

        if response.status_code >= 400:
            raise ApiError(response.status_code, method, path, response.text)

        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    async def _backoff(self, attempt: int, method: str, path: str, reason: str) -> None:
        delay = RETRY_BACKOFF_SECONDS * (2**attempt)
        logger.debug(
            "Retrying %s %s in %.1fs after %s (attempt %d of %d).",
            method,
            path,
            delay,
            reason,
            attempt + 1,
            self._retries + 1,
        )
        await asyncio.sleep(delay)

    # -- ticket endpoints --------------------------------------------------

    async def search_tickets(
        self,
        query: str,
        *,
        limit: int | None = None,
        start: int | None = None,
        locale: str | None = None,
    ) -> Any:
        """POST /api/ticket/search"""
        payload: dict[str, Any] = {"query": query}
        if limit is not None:
            payload["limit"] = limit
        if start is not None:
            payload["start"] = start
        if locale:
            payload["locale"] = locale
        return await self._request("POST", "/api/ticket/search", json=payload)

    async def get_ticket(self, ticket_id: str, *, fields: str | None = None) -> Any:
        """GET /api/ticket/<ticket-id>"""
        return await self._request(
            "GET", f"/api/ticket/{ticket_id}", params={"fields": fields}
        )

    async def get_ticket_actions(self, ticket_id: str) -> Any:
        """GET /api/ticket/<ticket-id>/actions"""
        return await self._request("GET", f"/api/ticket/{ticket_id}/actions")

    async def get_ticket_steps(self, ticket_id: str, *, since: int | None = None) -> Any:
        """GET /api/ticket/<ticket-id>/steps"""
        return await self._request(
            "GET", f"/api/ticket/{ticket_id}/steps", params={"since": since}
        )

    async def get_ticket_step(
        self, ticket_id: str, step_id: str, *, fields: str | None = None
    ) -> Any:
        """GET /api/ticket/<ticket-id>/steps/<step-id>"""
        return await self._request(
            "GET",
            f"/api/ticket/{ticket_id}/steps/{step_id}",
            params={"fields": fields},
        )

    async def create_ticket(
        self,
        payload: Mapping[str, Any],
        *,
        attachments: Sequence[AttachmentUpload] = (),
    ) -> Any:
        """POST /api/ticket/create"""
        return await self._post_with_attachments("/api/ticket/create", payload, attachments)

    async def apply_action(
        self,
        ticket_id: str,
        payload: Mapping[str, Any],
        *,
        attachments: Sequence[AttachmentUpload] = (),
    ) -> Any:
        """POST /api/ticket/<ticket-id>/apply"""
        return await self._post_with_attachments(
            f"/api/ticket/{ticket_id}/apply", payload, attachments
        )

    async def _post_with_attachments(
        self,
        path: str,
        payload: Mapping[str, Any],
        attachments: Sequence[AttachmentUpload],
    ) -> Any:
        if not attachments:
            return await self._request("POST", path, json=dict(payload))

        body = dict(payload)
        body["attachments"] = [description for description, _ in attachments]

        # The Web-API expects multipart/form-data with the JSON itself sent as a
        # file part named "json" and the files as attachment0, attachment1, ...
        files: list[tuple[str, tuple[str, bytes, str]]] = [
            ("json", ("json.txt", jsonlib.dumps(body).encode("utf-8"), "application/json"))
        ]
        for index, (description, content) in enumerate(attachments):
            name = str(description.get("name") or f"attachment{index}")
            files.append(
                (f"attachment{index}", (name, content, "application/octet-stream"))
            )
        return await self._request("POST", path, files=files)


#: How a pool builds one client. Kept injectable so the server can hand in its
#: own module level ``HelpdeskClient`` name, which the tests replace.
ClientFactory = Callable[..., HelpdeskClient]


class ClientPool:
    """Keeps one :class:`HelpdeskClient` per connection configuration alive.

    Building a client per tool call throws away the connection pool and, with
    basic authentication, opens a new session on the HelpDesk for every single
    request - the i-net documentation explicitly asks callers to reuse sessions
    instead. The pool is keyed by :class:`RequestConfig`, so different users of
    an HTTP deployment still never share a client.
    """

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        verify: str | bool = True,
        retries: int = 0,
        max_size: int = 8,
        factory: ClientFactory | None = None,
    ) -> None:
        self._timeout = timeout
        self._verify = verify
        self._retries = retries
        self._max_size = max(1, max_size)
        self._factory: ClientFactory = factory or HelpdeskClient
        self._clients: "OrderedDict[RequestConfig, HelpdeskClient]" = OrderedDict()
        self._lock = asyncio.Lock()

    async def acquire(self, config: RequestConfig) -> HelpdeskClient:
        """The client for *config*, created on first use."""
        async with self._lock:
            client = self._clients.get(config)
            if client is not None:
                self._clients.move_to_end(config)
                return client

            client = self._factory(
                config,
                timeout=self._timeout,
                verify=self._verify,
                retries=self._retries,
            )
            self._clients[config] = client
            evicted = []
            while len(self._clients) > self._max_size:
                _, oldest = self._clients.popitem(last=False)
                evicted.append(oldest)

        for oldest in evicted:
            await oldest.aclose()
        return client

    async def aclose(self) -> None:
        """Close every pooled client; called when the server shuts down."""
        async with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            await client.aclose()
