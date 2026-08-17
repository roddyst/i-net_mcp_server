"""Thin async wrapper around the i-net HelpDesk Ticket Web-API.

Documentation: https://docs.inetsoftware.de/helpdesk/help/webapi.ticket/p/ticket-web-api
"""

from __future__ import annotations

import json as jsonlib
from typing import Any, Mapping, Sequence

import httpx

from .config import RequestConfig
from .errors import ApiError, TransportError

USER_AGENT = "inet-helpdesk-mcp"

#: One attachment as it is handed to the Web-API: the JSON description plus the
#: raw bytes that are uploaded as ``attachment<N>``.
AttachmentUpload = tuple[dict[str, Any], bytes]


class HelpdeskClient:
    """Talks to one i-net HelpDesk instance on behalf of one user."""

    def __init__(
        self,
        config: RequestConfig,
        *,
        timeout: float = 30.0,
        verify: bool = True,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient(
            base_url=config.base_url,
            timeout=timeout,
            verify=verify,
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
        try:
            response = await self._client.request(
                method,
                path,
                params={k: v for k, v in (params or {}).items() if v is not None},
                json=json,
                files=files,
            )
        except httpx.HTTPError as exc:
            raise TransportError(
                f"Could not reach the i-net HelpDesk at {self._config.base_url}: {exc}"
            ) from exc

        if response.status_code >= 400:
            raise ApiError(response.status_code, method, path, response.text)

        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

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
