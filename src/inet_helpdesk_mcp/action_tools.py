"""One tool per ticket action, built from :data:`actions.ACTION_SPECS`.

Every tool here is a thin, named front for ``POST /api/ticket/<id>/apply``:
it fixes the action id, maps typed parameters onto the ticket fields or
action arguments the action needs, and checks first that the action is
currently available for the ticket. Actions whose arguments the i-net
documentation does not describe get the generic parameters of
``apply_ticket_action`` instead of guessed field names.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Annotated, Any, Literal, Sequence

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from .actions import (
    AFFECTED_ASSETS_ARGUMENT,
    APPLY_ANSWER_ARGUMENT,
    EMAIL_FROM_FIELD,
    EMAIL_SUBJECT_FIELD,
    EMAIL_TO_FIELD,
    SPECS_BY_NAME,
    ActionSpec,
    resolve_action_ids,
    to_millis,
)
from .attachments import Attachment

if TYPE_CHECKING:
    from .server import HelpdeskTools

TicketId = Annotated[
    str,
    Field(description="Ticket id, the plain number or the encoded form from email subjects."),
]
Text = Annotated[
    str, Field(description="Text of the new editing step, e.g. why the change was made.")
]
HtmlContent = Annotated[bool, Field(description="True if `text` contains HTML.")]
TicketFields = Annotated[
    dict[str, str] | None,
    Field(description="Ticket fields to set, keyed by field key or localized display name."),
]
StepFields = Annotated[
    dict[str, str] | None, Field(description="Fields of the created editing step.")
]
ActionArguments = Annotated[
    dict[str, str] | None,
    Field(
        description=(
            "Ticket action arguments, e.g. {\"ticketextension.automail\": \"NEVER\"}. "
            "Values that need JSON must be stringified."
        )
    ),
]
Attachments = Annotated[
    Sequence[Attachment] | None, Field(description="Files to attach to the editing step.")
]

#: Added to tools whose arguments are not documented by i-net.
UNDOCUMENTED_ARGUMENTS = (
    " The Web-API documentation does not name the fields this action needs: pass "
    "them in `ticket_fields`, `step_fields` or `action_arguments` if the HelpDesk "
    "asks for more. Unknown ticket fields are rejected; unknown action arguments are "
    "dropped and the accepted ones are listed in the HelpDesk debug log."
)


def _annotations(spec: ActionSpec, *, outward: bool = False) -> ToolAnnotations:
    return ToolAnnotations(
        read_only_hint=False,
        destructive_hint=spec.destructive or outward,
        idempotent_hint=False,
        open_world_hint=True,
    )


def register_action_tools(server: MCPServer, tools: "HelpdeskTools") -> None:
    """Register the catalogue tools whose action id the policy permits."""
    action_ids = resolve_action_ids(tools.settings.action_ids)

    def describe(spec: ActionSpec, extra: str = "") -> str:
        return (
            f"{spec.summary} Applies the ticket action {action_ids[spec.name]} "
            f"('{spec.label}') and returns the id of the new editing step. Fails with "
            "the list of available actions if this one does not fit the ticket's "
            f"current state.{extra}"
        )

    def register(name: str, fn: Any, *, extra: str = "", outward: bool = False) -> None:
        spec = SPECS_BY_NAME[name]
        if not tools.policy.permits(action_ids[name]):
            return
        server.add_tool(
            fn,
            name=name,
            title=spec.title,
            description=describe(spec, extra),
            annotations=_annotations(spec, outward=outward),
        )

    async def apply(
        ctx: Context, name: str, ticket_id: str, **request: Any
    ) -> dict[str, Any]:
        return await tools.apply(
            ctx, name, ticket_id, action_ids[name], check_available=True, **request
        )

    async def change_fields(
        ctx: Context,
        name: str,
        ticket_id: str,
        fields: dict[str, str],
        text: str,
        action_arguments: dict[str, str] | None,
    ) -> dict[str, Any]:
        return await apply(
            ctx,
            name,
            ticket_id,
            text=text,
            ticket_fields=fields,
            action_arguments=action_arguments,
        )

    # -- ticket fields ---------------------------------------------------------

    async def change_ticket_fields(
        ctx: Context,
        ticket_id: TicketId,
        fields: Annotated[
            dict[str, str],
            Field(description='Fields to set, e.g. {"priorityid": "80", "categoryid": "12"}.'),
        ],
        text: Text = "",
        action_arguments: ActionArguments = None,
    ) -> dict[str, Any]:
        return await change_fields(
            ctx, "change_ticket_fields", ticket_id, fields, text, action_arguments
        )

    async def change_priority(
        ctx: Context,
        ticket_id: TicketId,
        priority_id: Annotated[
            str, Field(description='Priority id as configured in the HelpDesk, e.g. "80".')
        ],
        text: Text = "",
        action_arguments: ActionArguments = None,
    ) -> dict[str, Any]:
        return await change_fields(
            ctx, "change_priority", ticket_id, {"priorityid": priority_id}, text, action_arguments
        )

    async def change_category(
        ctx: Context,
        ticket_id: TicketId,
        category_id: Annotated[str, Field(description="Category id as configured in the HelpDesk.")],
        text: Text = "",
        action_arguments: ActionArguments = None,
    ) -> dict[str, Any]:
        return await change_fields(
            ctx, "change_category", ticket_id, {"categoryid": category_id}, text, action_arguments
        )

    async def change_classification(
        ctx: Context,
        ticket_id: TicketId,
        classification_id: Annotated[
            str, Field(description="Classification id as configured in the HelpDesk.")
        ],
        text: Text = "",
        action_arguments: ActionArguments = None,
    ) -> dict[str, Any]:
        return await change_fields(
            ctx,
            "change_classification",
            ticket_id,
            {"classificationid": classification_id},
            text,
            action_arguments,
        )

    async def change_subject(
        ctx: Context,
        ticket_id: TicketId,
        subject: Annotated[str, Field(description="The new subject.")],
        text: Text = "",
        action_arguments: ActionArguments = None,
    ) -> dict[str, Any]:
        return await change_fields(
            ctx, "change_subject", ticket_id, {"subject": subject}, text, action_arguments
        )

    async def change_ticket_owner(
        ctx: Context,
        ticket_id: TicketId,
        owner_guid: Annotated[str, Field(description="GUID of the user who becomes the owner.")],
        text: Text = "",
        action_arguments: ActionArguments = None,
    ) -> dict[str, Any]:
        return await change_fields(
            ctx, "change_ticket_owner", ticket_id, {"ownerid": owner_guid}, text, action_arguments
        )

    async def change_itil(
        ctx: Context,
        ticket_id: TicketId,
        itil_id: Annotated[str, Field(description="ITIL type id as configured in the HelpDesk.")],
        text: Text = "",
        action_arguments: ActionArguments = None,
    ) -> dict[str, Any]:
        return await change_fields(
            ctx, "change_itil", ticket_id, {"itilid": itil_id}, text, action_arguments
        )

    async def change_deadline(
        ctx: Context,
        ticket_id: TicketId,
        deadline: Annotated[
            int | str,
            Field(
                description=(
                    "New deadline as epoch milliseconds or ISO 8601, e.g. "
                    "2026-10-31T16:00:00+01:00 (no zone means UTC)."
                )
            ),
        ],
        text: Text = "",
        action_arguments: ActionArguments = None,
    ) -> dict[str, Any]:
        return await change_fields(
            ctx,
            "change_deadline",
            ticket_id,
            {"deadline": to_millis(deadline)},
            text,
            action_arguments,
        )

    async def change_resource(
        ctx: Context,
        ticket_id: TicketId,
        resource_guid: Annotated[str, Field(description="GUID of the target resource.")],
        text: Text = "",
        action_arguments: ActionArguments = None,
    ) -> dict[str, Any]:
        return await change_fields(
            ctx, "change_resource", ticket_id, {"resourceid": resource_guid}, text, action_arguments
        )

    register("change_ticket_fields", change_ticket_fields)
    register("change_priority", change_priority)
    register("change_category", change_category)
    register("change_classification", change_classification)
    register("change_subject", change_subject)
    register("change_ticket_owner", change_ticket_owner)
    register("change_itil", change_itil)
    unverified_key = (
        " The field key {key!r} is not documented by i-net; if the HelpDesk rejects it, "
        "read the ticket with get_ticket to find the right key and use change_ticket_fields."
    )
    register("change_deadline", change_deadline, extra=unverified_key.format(key="deadline"))
    register("change_resource", change_resource, extra=unverified_key.format(key="resourceid"))

    # -- mail ----------------------------------------------------------------

    async def send_email(
        ctx: Context,
        ticket_id: TicketId,
        to: Annotated[
            str,
            Field(
                description="Recipient address, e.g. the ticket owner's email address.",
                min_length=3,
            ),
        ],
        subject: Annotated[
            str, Field(description='Subject of the email, e.g. "Re: " plus the ticket subject.')
        ],
        text: Annotated[str, Field(description="Body of the email.")],
        send_mode: Annotated[
            Literal["SEND_ONLY", "SEND_AND_CLOSE", "SEND_AND_CLOSE_AFTER_X_DAYS"],
            Field(
                description=(
                    "SEND_ONLY keeps the ticket open, SEND_AND_CLOSE closes it, "
                    "SEND_AND_CLOSE_AFTER_X_DAYS closes it after the configured delay."
                )
            ),
        ] = "SEND_ONLY",
        mail_from: Annotated[
            str | None,
            Field(
                description=(
                    "Mailbox to send from. Leave empty to use the server's configured "
                    "default."
                )
            ),
        ] = None,
        html_content: HtmlContent = False,
        step_fields: StepFields = None,
        action_arguments: ActionArguments = None,
        attachments: Attachments = None,
    ) -> dict[str, Any]:
        fields = dict(step_fields or {})
        fields[EMAIL_TO_FIELD] = to.strip()
        fields[EMAIL_SUBJECT_FIELD] = subject
        sender = mail_from or tools.settings.mail_from
        if sender:
            fields[EMAIL_FROM_FIELD] = sender
        arguments = dict(action_arguments or {})
        arguments.setdefault(APPLY_ANSWER_ARGUMENT, send_mode)
        return await apply(
            ctx,
            "send_email",
            ticket_id,
            text=text,
            html_content=html_content,
            step_fields=fields,
            action_arguments=arguments,
            attachments=attachments,
        )

    register("send_email", send_email, outward=True)

    # -- typed odds and ends ---------------------------------------------------

    async def add_attachment(
        ctx: Context,
        ticket_id: TicketId,
        attachments: Annotated[
            Sequence[Attachment], Field(description="Files to attach.", min_length=1)
        ],
        text: Text = "",
    ) -> dict[str, Any]:
        return await apply(ctx, "add_attachment", ticket_id, text=text, attachments=attachments)

    async def change_affected_assets(
        ctx: Context,
        ticket_id: TicketId,
        asset_guids: Annotated[
            list[str], Field(description="GUIDs of all assets affected by the ticket.")
        ],
        text: Text = "",
    ) -> dict[str, Any]:
        return await apply(
            ctx,
            "change_affected_assets",
            ticket_id,
            text=text,
            action_arguments={AFFECTED_ASSETS_ARGUMENT: json.dumps(asset_guids)},
        )

    register("add_attachment", add_attachment)
    register("change_affected_assets", change_affected_assets)

    # -- everything else: the action id plus the generic parameters ------------

    def generic(name: str) -> Any:
        async def tool(
            ctx: Context,
            ticket_id: TicketId,
            text: Text = "",
            html_content: HtmlContent = False,
            ticket_fields: TicketFields = None,
            step_fields: StepFields = None,
            action_arguments: ActionArguments = None,
            attachments: Attachments = None,
        ) -> dict[str, Any]:
            return await apply(
                ctx,
                name,
                ticket_id,
                text=text,
                html_content=html_content,
                ticket_fields=ticket_fields,
                step_fields=step_fields,
                action_arguments=action_arguments,
                attachments=attachments,
            )

        tool.__name__ = name
        return tool

    # Status changes are complete without further arguments.
    for name in ("authorize_ticket", "close_ticket", "set_auto_close", "delete_ticket"):
        register(name, generic(name))
    for name in ("receive_email", "live_support_answer"):
        register(name, generic(name))

    for name in (
        "edit_step_text",
        "delete_attachment",
        "bundle_tickets",
        "change_target_time",
        "change_identifier",
        "change_sharing",
        "use_stopwatch",
        "apply_quick_ticket",
        "run_form",
        "start_process",
        "add_to_knowledge_base",
        "rate_ticket",
        "live_support_to_ticket",
        "close_live_support_to_ticket",
        "live_support_ai_answer",
        "request_supporter_answer",
    ):
        register(name, generic(name), extra=UNDOCUMENTED_ARGUMENTS)
