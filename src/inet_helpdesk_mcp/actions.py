"""Catalogue of the ticket actions that get a tool of their own.

``apply_ticket_action`` can run any action, but an agent has to know the id
and the payload shape for it. The tools built from this catalogue carry both:
a descriptive name, the action id, and - where the i-net documentation or a
verified test against a live HelpDesk tells us - the ticket field or action
argument the action needs.

Two kinds of ids exist:

* Negative ids are the built-in actions of the HelpDesk and are the same on
  every installation.
* Positive ids belong to actions configured per installation (``2`` closing,
  ``5`` authorizing ...). The defaults below match the installation this server
  was built against; ``--action-ids=close_ticket=12`` moves one elsewhere.

Ticket fields are changed through ``-29`` ("Ticket-Feld ändern") even where a
dedicated action exists: that path is verified against a live HelpDesk, while
for example ``3`` ("Priorität ändern") answers HTTP 500 without telling which
argument it misses. A dedicated id can still be configured per tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from .errors import ConfigurationError, HelpdeskError

#: The action that changes arbitrary ticket fields.
CHANGE_FIELD_ACTION = "-29"

#: Values of ``ticketextension.applyanswer``, the argument "E-Mail versenden"
#: (-7) needs - without it the HelpDesk rejects the step.
APPLY_ANSWER_ARGUMENT = "ticketextension.applyanswer"
APPLY_ANSWER_VALUES = ("SEND_ONLY", "SEND_AND_CLOSE", "SEND_AND_CLOSE_AFTER_X_DAYS")

#: Action argument with the stringified list of asset GUIDs (Inventory plugin).
AFFECTED_ASSETS_ARGUMENT = "inventory_affectedassets"


@dataclass(frozen=True)
class ActionSpec:
    """One ticket action exposed as its own tool."""

    #: Tool name, also the key for ``--action-ids`` overrides.
    name: str
    #: Default ticket action id.
    action_id: str
    #: Display name in the German HelpDesk user interface.
    label: str
    #: Tool title shown to the agent.
    title: str
    #: What the action does, for the tool description.
    summary: str
    #: The action removes or irreversibly changes data.
    destructive: bool = False


# Order follows the action list of the HelpDesk, which keeps the tool list
# predictable for agents and for the tests.
ACTION_SPECS: tuple[ActionSpec, ...] = (
    # -- ticket fields, all through -29 --------------------------------------
    ActionSpec(
        "change_ticket_fields", CHANGE_FIELD_ACTION, "Ticket-Feld ändern", "Change ticket fields",
        "Set one or more ticket fields by field key (e.g. priorityid, categoryid, "
        "classificationid, subject, ownerid). The generic form of every change_* tool.",
    ),
    ActionSpec(
        "change_priority", CHANGE_FIELD_ACTION, "Priorität ändern", "Change priority",
        "Set the priority of a ticket (field priorityid).",
    ),
    ActionSpec(
        "change_category", CHANGE_FIELD_ACTION, "Kategorie ändern", "Change category",
        "Set the category of a ticket (field categoryid).",
    ),
    ActionSpec(
        "change_classification", CHANGE_FIELD_ACTION, "Klassifizierung ändern",
        "Change classification", "Set the classification of a ticket (field classificationid).",
    ),
    ActionSpec(
        "change_subject", CHANGE_FIELD_ACTION, "Betreff ändern", "Change subject",
        "Replace the subject of a ticket (field subject).",
    ),
    ActionSpec(
        "change_ticket_owner", CHANGE_FIELD_ACTION, "Benutzer ändern", "Change ticket owner",
        "Assign the ticket to a different end user, i.e. the person the request is "
        "for (field ownerid, a user GUID).",
    ),
    ActionSpec(
        "change_itil", CHANGE_FIELD_ACTION, "ITIL ändern", "Change ITIL type",
        "Set the ITIL type of a ticket (field itilid), e.g. incident or service request.",
    ),
    ActionSpec(
        "change_deadline", CHANGE_FIELD_ACTION, "Deadline ändern", "Change deadline",
        "Set the deadline of a ticket (field deadline, epoch milliseconds).",
    ),
    ActionSpec(
        "change_resource", CHANGE_FIELD_ACTION, "Ressource ändern", "Change resource",
        "Hand the ticket to another resource, i.e. supporter group (field resourceid, "
        "a resource GUID).",
    ),
    # -- status changes ---------------------------------------------------------
    ActionSpec(
        "authorize_ticket", "5", "Anfrage autorisieren", "Authorize request",
        "Authorize a request that is still waiting for a dispatcher (status 0 -> 100).",
    ),
    ActionSpec(
        "close_ticket", "2", "Beenden", "Close ticket",
        "Close the ticket (status 'Erledigt'). Closing a bundle's primary ticket also "
        "closes its secondary tickets.",
    ),
    ActionSpec(
        "set_auto_close", "-32", "Automatisch Beenden", "Close automatically",
        "Mark the ticket to be closed automatically by the HelpDesk.",
    ),
    ActionSpec(
        "delete_ticket", "7", "Anfrage/Auftrag löschen", "Delete ticket",
        "Delete the ticket (status 'Gelöscht'). Only on explicit request of the user.",
        destructive=True,
    ),
    # -- mail and conversation -------------------------------------------------
    ActionSpec(
        "send_email", "-7", "E-Mail versenden", "Send email",
        "Send `text` as an email answer to the ticket owner, optionally closing the "
        "ticket. This mails a person outside the HelpDesk - confirm with the user first.",
    ),
    ActionSpec(
        "receive_email", "-23", "E-Mail empfangen (dem Endanwender nicht anzeigen)",
        "Record received email",
        "Record a received email as a step that the end user does not see.",
    ),
    ActionSpec(
        "edit_step_text", "-22", "Text eines Bearbeitungsschrittes verändern",
        "Edit step text", "Change the text of an existing editing step.",
    ),
    ActionSpec(
        "add_attachment", "-21", "Anlage manuell hinzufügen", "Add attachment",
        "Attach one or more files to the ticket.",
    ),
    ActionSpec(
        "delete_attachment", "-6", "Anlage löschen", "Delete attachment",
        "Remove an attachment from the ticket.", destructive=True,
    ),
    # -- structure and planning ------------------------------------------------
    ActionSpec(
        "bundle_tickets", "-35", "Tickets verknüpfen", "Bundle tickets",
        "Bundle tickets under this ticket as the primary ticket. Not possible for "
        "closed, deleted or unassigned tickets.",
    ),
    ActionSpec(
        "change_target_time", "-30", "Sollzeit ändern", "Change target time",
        "Change the planned effort (target time) of the ticket.",
    ),
    ActionSpec(
        "change_identifier", "-27", "Kennung ändern", "Change identifier",
        "Change the identifier of the ticket.",
    ),
    ActionSpec(
        "change_sharing", "-38", "Freigaben ändern", "Change sharing",
        "Share the ticket with users or groups, or revoke a share.",
    ),
    ActionSpec(
        "change_affected_assets", "-39", "Betroffene Assets ändern", "Change affected assets",
        "Link inventory assets to the ticket (requires the Inventory plugin).",
    ),
    ActionSpec(
        "use_stopwatch", "-16", "Stoppuhr verwenden", "Use stopwatch",
        "Record working time on the ticket with the stopwatch.",
    ),
    ActionSpec(
        "apply_quick_ticket", "-33", "Quick-Ticket anwenden", "Apply quick ticket",
        "Apply a quick ticket template, which fills in fields and text.",
    ),
    ActionSpec(
        "run_form", "-41", "Formular ausführen", "Run form",
        "Run a configured form that changes ticket fields.",
    ),
    ActionSpec(
        "start_process", "-19", "Prozess starten", "Start process",
        "Start a configured (ITIL) process for the ticket.",
    ),
    ActionSpec(
        "add_to_knowledge_base", "-4", "Hinzufügen zu Wissensbasis", "Add to knowledge base",
        "Create a knowledge base article from this ticket.",
    ),
    ActionSpec(
        "rate_ticket", "-40", "Ticket bewerten", "Rate ticket",
        "Rate how the ticket was handled.",
    ),
    # -- live support ----------------------------------------------------------
    ActionSpec(
        "live_support_answer", "-123", "Live Support Antwort", "Answer live support chat",
        "Send `text` as an answer in a live support chat.",
    ),
    ActionSpec(
        "live_support_to_ticket", "-124", "Live Support in Ticket umwandeln",
        "Convert live support to ticket",
        "Turn a live support chat into a regular ticket and keep it open.",
    ),
    ActionSpec(
        "close_live_support_to_ticket", "-125", "Beenden und in Ticket umwandeln",
        "End live support as ticket", "End a live support chat and turn it into a ticket.",
    ),
    ActionSpec(
        "live_support_ai_answer", "-126", "Live Support KI-Antwort", "Live support AI answer",
        "Let the AI answer in a live support chat.",
    ),
    ActionSpec(
        "request_supporter_answer", "-127", "Supporter soll antworten",
        "Hand chat to a supporter", "Hand a live support chat from the AI to a human supporter.",
    ),
)

SPECS_BY_NAME: dict[str, ActionSpec] = {spec.name: spec for spec in ACTION_SPECS}


def parse_action_id_overrides(raw: str | None) -> tuple[tuple[str, str], ...]:
    """Parse ``close_ticket=12,authorize_ticket=5`` into validated pairs."""
    if not raw:
        return ()
    overrides: list[tuple[str, str]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        name, separator, action_id = part.partition("=")
        name, action_id = name.strip(), action_id.strip()
        if not separator or not name or not action_id:
            raise ConfigurationError(
                f"Invalid action id override {part!r}; write it as tool=id, "
                "e.g. close_ticket=12."
            )
        if name not in SPECS_BY_NAME:
            raise ConfigurationError(
                f"Unknown action tool {name!r} in the action id overrides. Known tools: "
                f"{', '.join(sorted(SPECS_BY_NAME))}."
            )
        overrides.append((name, action_id))
    return tuple(overrides)


def resolve_action_ids(overrides: tuple[tuple[str, str], ...]) -> dict[str, str]:
    """The effective action id of every catalogue tool."""
    ids = {spec.name: spec.action_id for spec in ACTION_SPECS}
    ids.update(dict(overrides))
    return ids


def to_millis(value: int | str) -> str:
    """Accept epoch milliseconds or an ISO 8601 timestamp, return milliseconds.

    The Web-API wants timestamps as millisecond strings; agents think in dates.
    A date without a time zone is read as UTC.
    """
    if isinstance(value, int):
        return str(value)
    text = str(value).strip()
    if text.lstrip("-").isdigit():
        return text
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HelpdeskError(
            f"Cannot read {value!r} as a point in time; pass epoch milliseconds or an "
            "ISO 8601 timestamp such as 2026-10-31T16:00:00+01:00."
        ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return str(int(moment.timestamp() * 1000))


def describe_available(actions: Mapping[str, object]) -> str:
    """Render an ``/actions`` answer for an error message."""
    if not actions:
        return "none"
    return ", ".join(f"{key} ({value})" for key, value in actions.items())
