"""MCP server exposing the i-net HelpDesk Ticket Web-API as tools."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Mapping, Sequence

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations

from . import __version__
from .action_tools import register_action_tools
from .actions import describe_available
from .attachments import Attachment, prepare_attachments
from .client import ClientPool, HelpdeskClient
from .config import RequestConfig, Settings, resolve_request_config
from .errors import ApiError, HelpdeskError
from .normalize import html_to_text, normalize_record, truncate

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
Tools for the i-net HelpDesk ticket system (Web-API).

Typical flow:
  1. `search_tickets` with a phrase such as `Resource:"First Level Support"` or a
     plain full-text search to find ticket ids.
  2. `get_ticket_conversation` reads one ticket with its history in a single call;
     `get_ticket`, `list_ticket_steps` and `get_ticket_step` are the finer grained
     variants.
  3. Change a ticket with the named action tools - `change_priority`,
     `change_category`, `send_email`, `close_ticket`, `authorize_ticket`,
     `bundle_tickets` and so on. Each one knows its action id and checks that
     the action is available for the ticket right now.
  4. `list_ticket_actions` plus `apply_ticket_action` cover any action without a
     tool of its own.
  5. `create_ticket` opens a new ticket.

Requests waiting for authorization (status 0) never show up in
`search_tickets`; `find_unauthorized_tickets` scans ticket ids for them.
`ticket_statistics` counts search hits per status, priority or other field.

Ticket ids are accepted as plain integers or in the encoded form used in the
subject line of HelpDesk emails. Every call acts as the authenticated user, so
permissions and visible fields depend on that user's roles.
"""

#: How many step or ticket detail requests run at the same time. Enough to keep
#: a conversation fast, low enough to stay a polite client of one HelpDesk.
DETAIL_CONCURRENCY = 5

#: Above this many search hits the details are not resolved: one request per
#: ticket would turn a single search into a hundred round trips.
MAX_RESOLVED_DETAILS = 50

#: Upper bound for tools that read one ticket per id (scan, statistics).
MAX_SCANNED_TICKETS = 500

#: Status of a request that still waits for a dispatcher to authorize it.
UNAUTHORIZED_STATUS = 0

#: A short, useful field set for search results.
DEFAULT_DETAIL_FIELDS = "subject,statusid,priorityid,ownerid,lastchanged"

READ_ONLY_TOOL = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)


def _make_client(config: RequestConfig, **kwargs: Any) -> HelpdeskClient:
    """Indirection for the client pool, so tests can replace the class here."""
    return HelpdeskClient(config, **kwargs)


def _tool_result(payload: Any) -> dict[str, Any]:
    """Normalise Web-API responses that are not JSON objects."""
    if isinstance(payload, dict):
        return payload
    return {"result": payload}


async def _gather_limited(tasks: Sequence[Any]) -> list[Any]:
    """Run coroutines with a bounded number of concurrent requests."""
    semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)

    async def guarded(task: Any) -> Any:
        async with semaphore:
            return await task

    return list(await asyncio.gather(*(guarded(task) for task in tasks)))


class HelpdeskTools:
    """Binds the tool implementations to one set of settings."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.policy = settings.policy()
        self.pool = ClientPool(
            timeout=settings.timeout,
            verify=settings.tls_verify(),
            retries=settings.retries,
            max_size=settings.pool_size,
            factory=_make_client,
        )

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
        """The pooled client for this request. It stays open for the next call."""
        config = self.request_config(ctx)
        yield await self.pool.acquire(config)

    async def aclose(self) -> None:
        await self.pool.aclose()

    def normalizing(self, raw: bool) -> bool:
        return self.settings.normalize and not raw

    def shape(self, payload: Any, *, raw: bool) -> Any:
        """Reshape one ticket or step record unless raw output was asked for."""
        return normalize_record(payload) if self.normalizing(raw) else payload

    def audit(self, event: str, **details: Any) -> None:
        """Record a writing call. Never contains a token or a password."""
        rendered = " ".join(f"{key}={value}" for key, value in details.items() if value)
        logger.info("%s %s", event, rendered)

    async def apply(
        self,
        ctx: Context | None,
        event: str,
        ticket_id: str,
        action_id: str,
        *,
        text: str = "",
        html_content: bool = False,
        ticket_fields: Mapping[str, str] | None = None,
        step_fields: Mapping[str, str] | None = None,
        action_arguments: Mapping[str, str] | None = None,
        attachments: Sequence[Attachment] | None = None,
        check_available: bool = False,
    ) -> dict[str, Any]:
        """Apply one ticket action; shared by every writing tool on a ticket.

        With ``check_available`` the ticket's current actions are read first, so
        an action that does not fit the ticket's state fails with the list of
        actions that do - the HelpDesk itself often answers such a call with a
        bare HTTP 500.
        """
        self.policy.check(action_id)

        payload: dict[str, Any] = {
            "actionId": action_id,
            "text": text,
            "htmlContent": html_content,
        }
        if ticket_fields:
            payload["ticketFields"] = dict(ticket_fields)
        if step_fields:
            payload["stepFields"] = dict(step_fields)
        arguments = self.policy.apply_automail(action_arguments)
        if arguments:
            payload["actionArguments"] = arguments

        uploads = prepare_attachments(
            attachments, allow_local_files=self.settings.allow_local_files
        )
        path = f"/api/ticket/{ticket_id}/apply"
        if self.settings.dry_run:
            self.audit(event, ticket=ticket_id, action=action_id, dry_run=True)
            return _dry_run(payload, path, uploads)

        async with self.client(ctx) as client:
            if check_available:
                available = await client.get_ticket_actions(ticket_id)
                if isinstance(available, Mapping) and action_id not in available:
                    raise HelpdeskError(
                        f"The ticket action {action_id} is not available for ticket "
                        f"{ticket_id} right now (state, permissions or configuration). "
                        f"Available actions: {describe_available(available)}."
                    )
            result = await client.apply_action(ticket_id, payload, attachments=uploads)
        self.audit(
            event,
            ticket=ticket_id,
            action=action_id,
            step=result,
            attachments=len(uploads),
        )
        return {"ticketId": ticket_id, "actionId": action_id, "stepId": result}


def build_server(settings: Settings) -> MCPServer:
    """Create the MCP server for the given settings.

    Write tools (`create_ticket`, `apply_ticket_action`) are only registered when
    the server does not run in read-only mode.
    """
    tools = HelpdeskTools(settings)

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[dict[str, Any]]:
        try:
            yield {}
        finally:
            await tools.aclose()

    server = MCPServer(
        name="i-net-helpdesk",
        title="i-net HelpDesk",
        version=__version__,
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
    )

    # -- diagnostics -------------------------------------------------------

    @server.tool(
        title="Server info",
        description=(
            "Report how this MCP server is configured and verify that the i-net "
            "HelpDesk can be reached with the current credentials. Use this first "
            "when a call fails with an authentication or connection error."
        ),
        annotations=READ_ONLY_TOOL,
    )
    async def server_info(ctx: Context) -> dict[str, Any]:
        info: dict[str, Any] = {
            "version": __version__,
            "transport": settings.transport,
            "read_only": settings.read_only,
            "dry_run": settings.dry_run,
            "normalize": settings.normalize,
            "retries": settings.retries,
            "allowed_actions": list(settings.allowed_actions),
            "denied_actions": list(settings.denied_actions),
            "default_automail": settings.default_automail,
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
        client = await tools.pool.acquire(config)
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
            "narrow the query further. Set `include_details` to get subject and "
            "status of each hit in the same call instead of one `get_ticket` per id."
        ),
        annotations=READ_ONLY_TOOL,
    )
    async def search_tickets(
        ctx: Context,
        query: str,
        limit: int = 100,
        start: int = 0,
        locale: str | None = None,
        include_details: bool = False,
        detail_fields: str = DEFAULT_DETAIL_FIELDS,
        raw: bool = False,
    ) -> dict[str, Any]:
        """Args:
        query: Search phrase. An empty string matches everything.
        limit: Maximum number of tickets to return (default 100).
        start: Offset for paging through more results (default 0).
        locale: Language of the search phrase, e.g. "de" or "en".
        include_details: Also read a few fields of every hit. Only up to 50 hits.
        detail_fields: Comma separated fields to read when `include_details` is set.
        raw: Return the answer of the Web-API unchanged, without display values,
            ISO timestamps or converted HTML.
        """
        async with tools.client(ctx) as client:
            payload = _tool_result(
                await client.search_tickets(
                    query,
                    limit=limit,
                    start=start,
                    locale=locale or settings.default_locale,
                )
            )
            if not include_details:
                return payload

            ticket_ids = payload.get("ticketList") or []
            if len(ticket_ids) > MAX_RESOLVED_DETAILS:
                payload["detailsOmitted"] = (
                    f"{len(ticket_ids)} hits exceed the limit of {MAX_RESOLVED_DETAILS} "
                    "resolved tickets. Narrow the query or lower 'limit'."
                )
                return payload

            async def read(ticket_id: Any) -> dict[str, Any]:
                try:
                    record = await client.get_ticket(str(ticket_id), fields=detail_fields)
                except HelpdeskError as exc:
                    return {"ticketId": ticket_id, "error": str(exc)}
                return tools.shape(_tool_result(record), raw=raw)

            payload["tickets"] = await _gather_limited(
                [read(ticket_id) for ticket_id in ticket_ids]
            )
            return payload

    @server.tool(
        title="Get ticket",
        description=(
            "Read one ticket with its fields and attributes. Pass `fields` to limit "
            "the response to the entries you need - the full record can be large and "
            "may contain personal data."
        ),
        annotations=READ_ONLY_TOOL,
    )
    async def get_ticket(
        ctx: Context, ticket_id: str, fields: str | None = None, raw: bool = False
    ) -> dict[str, Any]:
        """Args:
        ticket_id: Ticket id, either the plain number or the encoded form from email subjects.
        fields: Optional comma separated list of field and attribute names, e.g. "subject,statusid".
        raw: Return the answer of the Web-API unchanged, without display values or
            ISO timestamps.
        """
        async with tools.client(ctx) as client:
            record = await client.get_ticket(ticket_id, fields=fields)
        return tools.shape(_tool_result(record), raw=raw)

    @server.tool(
        title="Get ticket conversation",
        description=(
            "Read a ticket together with its editing steps and their texts in one "
            "call - the fastest way to understand what a ticket is about. Prefer "
            "this over `get_ticket` plus one `get_ticket_step` per step. Steps of "
            "bundled secondary tickets are included. `limit` keeps the newest steps."
        ),
        annotations=READ_ONLY_TOOL,
    )
    async def get_ticket_conversation(
        ctx: Context,
        ticket_id: str,
        limit: int = 20,
        since: int | None = None,
        include_text: bool = True,
        max_text_chars: int = 4000,
        fields: str | None = None,
        raw: bool = False,
    ) -> dict[str, Any]:
        """Args:
        ticket_id: Ticket id, either the plain number or the encoded form from email subjects.
        limit: How many of the newest steps to read in full (default 20, 0 for all).
        since: Optional timestamp in milliseconds; only newer steps are considered.
        include_text: Read the text of every returned step (default true).
        max_text_chars: Cut a single step text after this many characters (0 for no limit).
        fields: Optional comma separated field list for the ticket record itself.
        raw: Return the answers of the Web-API unchanged: no display values, no ISO
            timestamps, HTML step texts as HTML.
        """
        async with tools.client(ctx) as client:
            ticket = await client.get_ticket(ticket_id, fields=fields)
            steps = await client.get_ticket_steps(ticket_id, since=since)
            summaries: list[dict[str, Any]] = [
                dict(step) for step in steps if isinstance(step, Mapping)
            ]
            selected = summaries[-limit:] if limit and limit > 0 else summaries

            details: list[Any] = []
            if include_text and selected:

                async def read(summary: Mapping[str, Any]) -> Any:
                    # A merged step belongs to the secondary ticket it came from;
                    # asking the primary ticket for it answers 404.
                    owner = summary.get("secondaryTicketId") or ticket_id
                    try:
                        return await client.get_ticket_step(
                            str(owner), str(summary.get("id"))
                        )
                    except HelpdeskError as exc:
                        return {"error": str(exc)}

                details = await _gather_limited([read(step) for step in selected])

        entries: list[dict[str, Any]] = []
        for index, summary in enumerate(selected):
            entry = dict(summary)
            detail = details[index] if index < len(details) else None
            if isinstance(detail, Mapping):
                entry.update(detail)
            if tools.normalizing(raw):
                entry = normalize_record(entry)
                text = entry.get("text")
                if isinstance(text, str):
                    if entry.get("htmlContent"):
                        entry["text"] = html_to_text(text)
                        entry["htmlContent"] = False
                        entry["textWasHtml"] = True
                    entry["text"] = truncate(str(entry["text"]), max_text_chars)
            entries.append(entry)

        return {
            "ticketId": ticket_id,
            "ticket": tools.shape(_tool_result(ticket), raw=raw),
            "steps": entries,
            "stepCount": len(summaries),
            "returnedSteps": len(entries),
            "truncated": len(entries) < len(summaries),
        }

    @server.tool(
        title="List ticket actions",
        description=(
            "List the ticket actions the authenticated user may currently apply to a "
            "ticket, as a map of action id to display name. The ids are the input for "
            "`apply_ticket_action`."
        ),
        annotations=READ_ONLY_TOOL,
    )
    async def list_ticket_actions(ctx: Context, ticket_id: str) -> dict[str, Any]:
        """Args:
        ticket_id: Ticket id, either the plain number or the encoded form from email subjects.
        """
        async with tools.client(ctx) as client:
            actions = await client.get_ticket_actions(ticket_id)
        if not isinstance(actions, Mapping):
            return {"ticketId": ticket_id, "actions": actions}
        permitted, hidden = tools.policy.filter_actions(actions)
        result: dict[str, Any] = {"ticketId": ticket_id, "actions": permitted}
        if hidden:
            result["hiddenByPolicy"] = hidden
        return result

    @server.tool(
        title="List ticket steps",
        description=(
            "List the editing steps (history) of a ticket without their texts. For a "
            "primary ticket in a bundle the steps of its secondary tickets are merged "
            "in. `get_ticket_conversation` returns the same list including the texts."
        ),
        annotations=READ_ONLY_TOOL,
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
        annotations=READ_ONLY_TOOL,
    )
    async def get_ticket_step(
        ctx: Context,
        ticket_id: str,
        step_id: str,
        fields: str | None = None,
        raw: bool = False,
    ) -> dict[str, Any]:
        """Args:
        ticket_id: Ticket id, either the plain number or the encoded form from email subjects.
        step_id: Id of the editing step, as returned by `list_ticket_steps`.
        fields: Optional comma separated list of field and attribute names.
        raw: Return the answer of the Web-API unchanged, HTML text included.
        """
        async with tools.client(ctx) as client:
            record = await client.get_ticket_step(ticket_id, step_id, fields=fields)
        result = tools.shape(_tool_result(record), raw=raw)
        if tools.normalizing(raw) and isinstance(result.get("text"), str):
            if result.get("htmlContent"):
                result["text"] = html_to_text(result["text"])
                result["htmlContent"] = False
                result["textWasHtml"] = True
        return result

    @server.tool(
        title="Find unauthorized tickets",
        description=(
            "Find requests that still wait for authorization (status 0, 'Anfrage'). "
            "The ticket search never returns them, so this tool reads a range of "
            "ticket ids one by one and keeps those in status 0 - start just below the "
            "newest known ticket id and scan backwards. Needs the dispatcher role. "
            "Authorize a hit with `authorize_ticket`."
        ),
        annotations=READ_ONLY_TOOL,
    )
    async def find_unauthorized_tickets(
        ctx: Context,
        start_id: int,
        count: int = 100,
        descending: bool = True,
        raw: bool = False,
    ) -> dict[str, Any]:
        """Args:
        start_id: First ticket id to check, usually the newest known id.
        count: How many consecutive ids to check (at most 500).
        descending: Scan towards older tickets (default) or newer ones.
        raw: Return the ticket records unchanged.
        """
        if not 0 < count <= MAX_SCANNED_TICKETS:
            raise HelpdeskError(f"count must be between 1 and {MAX_SCANNED_TICKETS}.")
        step = -1 if descending else 1
        ticket_ids = [
            start_id + step * offset
            for offset in range(count)
            if start_id + step * offset > 0
        ]

        async with tools.client(ctx) as client:

            async def read(ticket_id: int) -> Any:
                try:
                    return await client.get_ticket(
                        str(ticket_id), fields="subject,statusid,ownerid,inquirydate"
                    )
                except ApiError as exc:
                    if exc.status_code == 404:
                        return None
                    raise

            records = await _gather_limited([read(ticket_id) for ticket_id in ticket_ids])

        hits = [
            tools.shape(record, raw=raw)
            for record in records
            if isinstance(record, Mapping) and _status_of(record) == UNAUTHORIZED_STATUS
        ]
        return {
            "scannedIds": [ticket_ids[0], ticket_ids[-1]] if ticket_ids else [],
            "checked": len(ticket_ids),
            "missing": sum(1 for record in records if record is None),
            "tickets": hits,
        }

    @server.tool(
        title="Ticket statistics",
        description=(
            "Count the tickets matching a search phrase by field, e.g. how many open "
            "tickets per status, priority or resource. Reads every hit, so it is "
            "limited to 500 tickets - narrow the phrase for larger sets."
        ),
        annotations=READ_ONLY_TOOL,
    )
    async def ticket_statistics(
        ctx: Context,
        query: str,
        group_by: str = "statusid,priorityid",
        limit: int = 200,
        locale: str | None = None,
    ) -> dict[str, Any]:
        """Args:
        query: Search phrase as for `search_tickets`; empty matches everything.
        group_by: Comma separated field keys to count by, e.g. "statusid,resourceid".
        limit: How many hits to read and count (at most 500).
        locale: Language of the search phrase.
        """
        if not 0 < limit <= MAX_SCANNED_TICKETS:
            raise HelpdeskError(f"limit must be between 1 and {MAX_SCANNED_TICKETS}.")
        keys = [key.strip() for key in group_by.split(",") if key.strip()]
        if not keys:
            raise HelpdeskError("group_by needs at least one field key.")

        async with tools.client(ctx) as client:
            found = _tool_result(
                await client.search_tickets(
                    query, limit=limit, start=0, locale=locale or settings.default_locale
                )
            )
            ticket_ids = found.get("ticketList") or []

            async def read(ticket_id: Any) -> Any:
                try:
                    return await client.get_ticket(str(ticket_id), fields=",".join(keys))
                except HelpdeskError:
                    return None

            records = await _gather_limited([read(ticket_id) for ticket_id in ticket_ids])

        counts: dict[str, dict[str, int]] = {key: {} for key in keys}
        for record in records:
            if not isinstance(record, Mapping):
                continue
            for key in keys:
                label = _display_value(record, key)
                counts[key][label] = counts[key].get(label, 0) + 1
        return {
            "query": query,
            "counted": sum(1 for record in records if isinstance(record, Mapping)),
            "unreadable": sum(1 for record in records if not isinstance(record, Mapping)),
            "hasMore": bool(found.get("hasMore")),
            "counts": {
                key: dict(sorted(values.items(), key=lambda item: -item[1]))
                for key, values in counts.items()
            },
        }

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
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
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
        arguments = tools.policy.apply_automail(action_arguments)
        if arguments:
            payload["actionArguments"] = arguments

        uploads = prepare_attachments(
            attachments, allow_local_files=settings.allow_local_files
        )
        if settings.dry_run:
            tools.audit("create_ticket", dry_run=True)
            return _dry_run(payload, "/api/ticket/create", uploads)

        async with tools.client(ctx) as client:
            result = await client.create_ticket(payload, attachments=uploads)
        tools.audit("create_ticket", ticket=result, attachments=len(uploads))
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
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=True,
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
        return await tools.apply(
            ctx,
            "apply_ticket_action",
            ticket_id,
            action_id,
            text=text,
            html_content=html_content,
            ticket_fields=ticket_fields,
            step_fields=step_fields,
            action_arguments=action_arguments,
            attachments=attachments,
        )

    register_action_tools(server, tools)

    return server


def _dry_run(
    payload: Mapping[str, Any], path: str, uploads: Sequence[tuple[dict[str, Any], bytes]]
) -> dict[str, Any]:
    """What the server would have sent, for `--dry-run`."""
    return {
        "dryRun": True,
        "method": "POST",
        "path": path,
        "payload": dict(payload),
        "attachments": [
            {"name": description.get("name"), "bytes": len(content)}
            for description, content in uploads
        ],
    }


def _ticket_value(record: Mapping[str, Any], key: str) -> tuple[Any, Any]:
    """A field or attribute of a raw ticket record, with its display value."""
    for section in ("fields", "attributes"):
        values = record.get(section)
        if isinstance(values, Mapping) and key in values:
            return values[key], values.get(f"{key}_display")
    return None, None


def _status_of(record: Mapping[str, Any]) -> int | None:
    value, _ = _ticket_value(record, "statusid")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _display_value(record: Mapping[str, Any], key: str) -> str:
    """How a value is counted: display name and id, so both stay visible."""
    value, display = _ticket_value(record, key)
    if value is None:
        return "<not set>"
    if display not in (None, "", value):
        return f"{display} ({value})"
    return str(value)
