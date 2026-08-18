"""MCP server exposing the i-net HelpDesk Ticket Web-API as tools."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Sequence

from mcp.server.mcpserver import Context, MCPServer

from . import __version__
from .attachments import Attachment, prepare_attachments
from .client import HelpdeskClient
from .config import RequestConfig, Settings, resolve_request_config
from .errors import HelpdeskError

INSTRUCTIONS = """\
Tools for the i-net HelpDesk ticket system (Web-API).

Typical flow:
  1. `search_tickets` with a phrase such as `Resource:"First Level Support"` or a
     plain full-text search to find ticket ids.
  2. `get_ticket` for the fields and attributes of one ticket, `list_ticket_steps`
     plus `get_ticket_step` for its history.
  3. `list_ticket_actions` shows which actions the current user may apply, then
     `apply_ticket_action` performs one of them (answer, close, escalate, ...).
  4. `create_ticket` opens a new ticket.

Ticket ids are accepted as plain integers or in the encoded form used in the
subject line of HelpDesk emails. Every call acts as the authenticated user, so
permissions and visible fields depend on that user's roles.
"""


def _tool_result(payload: Any) -> dict[str, Any]:
    """Normalise Web-API responses that are not JSON objects."""
    if isinstance(payload, dict):
        return payload
    return {"result": payload}


class HelpdeskTools:
    """Binds the tool implementations to one set of settings."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def request_config(self, ctx: Context | None) -> RequestConfig:
        headers = None
        if ctx is not None:
            try:
                headers = ctx.headers
            except Exception:  # pragma: no cover - stdio transport has no request
                headers = None
        return resolve_request_config(self.settings, headers)

    @asynccontextmanager
    async def client(self, ctx: Context | None) -> AsyncIterator[HelpdeskClient]:
        config = self.request_config(ctx)
        async with HelpdeskClient(
            config, timeout=self.settings.timeout, verify=self.settings.tls_verify()
        ) as client:
            yield client


def build_server(settings: Settings) -> MCPServer:
    """Create the MCP server for the given settings.

    Write tools (`create_ticket`, `apply_ticket_action`) are only registered when
    the server does not run in read-only mode.
    """
    tools = HelpdeskTools(settings)
    server = MCPServer(
        name="i-net-helpdesk",
        title="i-net HelpDesk",
        version=__version__,
        instructions=INSTRUCTIONS,
    )

    # -- diagnostics -------------------------------------------------------

    @server.tool(
        title="Server info",
        description=(
            "Report how this MCP server is configured and verify that the i-net "
            "HelpDesk can be reached with the current credentials. Use this first "
            "when a call fails with an authentication or connection error."
        ),
    )
    async def server_info(ctx: Context) -> dict[str, Any]:
        info: dict[str, Any] = {
            "version": __version__,
            "transport": settings.transport,
            "read_only": settings.read_only,
            "allow_local_file_attachments": settings.allow_local_files,
            "allow_base_url_header": settings.allow_url_header,
            "ignore_client_authorization": settings.ignore_client_auth,
            "tls": settings.describe_tls(),
        }
        try:
            config = tools.request_config(ctx)
        except HelpdeskError as exc:
            info["connection"] = "not configured"
            info["problem"] = str(exc)
            return info

        info["base_url"] = config.base_url
        info["authentication"] = config.source
        async with HelpdeskClient(
            config, timeout=settings.timeout, verify=settings.tls_verify()
        ) as client:
            try:
                await client.search_tickets("", limit=1, locale=settings.default_locale)
            except HelpdeskError as exc:
                info["connection"] = "failed"
                info["problem"] = str(exc)
            else:
                info["connection"] = "ok"
        return info

    # -- read tools --------------------------------------------------------

    @server.tool(
        title="Search tickets",
        description=(
            "Search tickets with the same phrase syntax as the Tickets application, "
            'for example `printer` or `Resource:"First Level Support"`. Returns the '
            "matching ticket ids plus suggestions for fields and values that can "
            "narrow the query further."
        ),
    )
    async def search_tickets(
        ctx: Context,
        query: str,
        limit: int = 100,
        start: int = 0,
        locale: str | None = None,
    ) -> dict[str, Any]:
        """Args:
        query: Search phrase. An empty string matches everything.
        limit: Maximum number of tickets to return (default 100).
        start: Offset for paging through more results (default 0).
        locale: Language of the search phrase, e.g. "de" or "en".
        """
        async with tools.client(ctx) as client:
            return _tool_result(
                await client.search_tickets(
                    query,
                    limit=limit,
                    start=start,
                    locale=locale or settings.default_locale,
                )
            )

    @server.tool(
        title="Get ticket",
        description=(
            "Read one ticket with its fields and attributes. Pass `fields` to limit "
            "the response to the entries you need - the full record can be large and "
            "may contain personal data."
        ),
    )
    async def get_ticket(
        ctx: Context, ticket_id: str, fields: str | None = None
    ) -> dict[str, Any]:
        """Args:
        ticket_id: Ticket id, either the plain number or the encoded form from email subjects.
        fields: Optional comma separated list of field and attribute names, e.g. "subject,statusid".
        """
        async with tools.client(ctx) as client:
            return _tool_result(await client.get_ticket(ticket_id, fields=fields))

    @server.tool(
        title="List ticket actions",
        description=(
            "List the ticket actions the authenticated user may currently apply to a "
            "ticket, as a map of action id to display name. The ids are the input for "
            "`apply_ticket_action`."
        ),
    )
    async def list_ticket_actions(ctx: Context, ticket_id: str) -> dict[str, Any]:
        """Args:
        ticket_id: Ticket id, either the plain number or the encoded form from email subjects.
        """
        async with tools.client(ctx) as client:
            actions = await client.get_ticket_actions(ticket_id)
        return {"ticketId": ticket_id, "actions": actions}

    @server.tool(
        title="List ticket steps",
        description=(
            "List the editing steps (history) of a ticket. For a primary ticket in a "
            "bundle the steps of its secondary tickets are merged in. Use "
            "`get_ticket_step` for the text of a single step."
        ),
    )
    async def list_ticket_steps(
        ctx: Context, ticket_id: str, since: int | None = None
    ) -> dict[str, Any]:
        """Args:
        ticket_id: Ticket id, either the plain number or the encoded form from email subjects.
        since: Optional timestamp in milliseconds; only newer steps are returned.
        """
        async with tools.client(ctx) as client:
            steps = await client.get_ticket_steps(ticket_id, since=since)
        return {"ticketId": ticket_id, "steps": steps}

    @server.tool(
        title="Get ticket step",
        description=(
            "Read one editing step of a ticket, including its text and the applied "
            "ticket action. Pass `fields` to reduce the returned fields and attributes."
        ),
    )
    async def get_ticket_step(
        ctx: Context, ticket_id: str, step_id: str, fields: str | None = None
    ) -> dict[str, Any]:
        """Args:
        ticket_id: Ticket id, either the plain number or the encoded form from email subjects.
        step_id: Id of the editing step, as returned by `list_ticket_steps`.
        fields: Optional comma separated list of field and attribute names.
        """
        async with tools.client(ctx) as client:
            return _tool_result(
                await client.get_ticket_step(ticket_id, step_id, fields=fields)
            )

    if settings.read_only:
        return server

    # -- write tools -------------------------------------------------------

    @server.tool(
        title="Create ticket",
        description=(
            "Create a new ticket and return its id. `text` is the request text of the "
            "ticket. `ticket_fields` and `action_arguments` are optional and only "
            "needed for advanced cases - unknown ticket fields are rejected, unknown "
            "action arguments are ignored by the server."
        ),
    )
    async def create_ticket(
        ctx: Context,
        text: str,
        html_content: bool = False,
        owner_guid: str | None = None,
        ticket_fields: dict[str, str] | None = None,
        action_arguments: dict[str, str] | None = None,
        attachments: Sequence[Attachment] | None = None,
    ) -> dict[str, Any]:
        """Args:
        text: Request text of the new ticket.
        html_content: True if `text` contains HTML.
        owner_guid: GUID of the ticket owner; defaults to the authenticated user.
            Only supporters may set a different owner.
        ticket_fields: Optional ticket fields, keyed by field key or localized display name.
        action_arguments: Optional ticket action arguments, e.g.
            {"ticketextension.dispatchNow": "ALWAYS"}. Values that need JSON must be stringified.
        attachments: Optional files to attach.
        """
        payload: dict[str, Any] = {"text": text, "htmlContent": html_content}
        if owner_guid:
            payload["ownerGUID"] = owner_guid
        if ticket_fields:
            payload["ticketFields"] = ticket_fields
        if action_arguments:
            payload["actionArguments"] = action_arguments

        uploads = prepare_attachments(
            attachments, allow_local_files=settings.allow_local_files
        )
        async with tools.client(ctx) as client:
            result = await client.create_ticket(payload, attachments=uploads)
        return {"ticketId": result}

    @server.tool(
        title="Apply ticket action",
        description=(
            "Apply a ticket action to an existing ticket - answering, closing, "
            "escalating and so on - and return the id of the new editing step. Call "
            "`list_ticket_actions` first: only the ids listed there are valid, and "
            "they differ per ticket, user and state. This changes the ticket, so "
            "confirm the action with the user when the intent is ambiguous."
        ),
    )
    async def apply_ticket_action(
        ctx: Context,
        ticket_id: str,
        action_id: str,
        text: str = "",
        html_content: bool = False,
        ticket_fields: dict[str, str] | None = None,
        step_fields: dict[str, str] | None = None,
        action_arguments: dict[str, str] | None = None,
        attachments: Sequence[Attachment] | None = None,
    ) -> dict[str, Any]:
        """Args:
        ticket_id: Ticket id, either the plain number or the encoded form from email subjects.
        action_id: Id of the ticket action, from `list_ticket_actions`.
        text: Text of the new editing step.
        html_content: True if `text` contains HTML.
        ticket_fields: Optional ticket fields to set, keyed by field key or display name.
        step_fields: Optional fields of the created editing step.
        action_arguments: Optional ticket action arguments; some actions require them,
            for example {"processingtimeextension.appointment": "1733875200000"} for a
            resubmission. Values that need JSON must be stringified.
        attachments: Optional files to attach.
        """
        payload: dict[str, Any] = {
            "actionId": action_id,
            "text": text,
            "htmlContent": html_content,
        }
        if ticket_fields:
            payload["ticketFields"] = ticket_fields
        if step_fields:
            payload["stepFields"] = step_fields
        if action_arguments:
            payload["actionArguments"] = action_arguments

        uploads = prepare_attachments(
            attachments, allow_local_files=settings.allow_local_files
        )
        async with tools.client(ctx) as client:
            result = await client.apply_action(ticket_id, payload, attachments=uploads)
        return {"ticketId": ticket_id, "stepId": result}

    return server
