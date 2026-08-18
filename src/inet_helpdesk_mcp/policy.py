"""Guard rails for the writing tools.

``--read-only`` is all or nothing: it removes ``create_ticket`` and
``apply_ticket_action`` from the tool list. In practice an operator often wants
something in between - an agent that may answer tickets but never close or
escalate them, or one that may act but must not trigger auto-mails to end
users. That is what this module decides; the tools in :mod:`server` only ask.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .errors import ConfigurationError, HelpdeskError

#: The ticket action argument that decides about auto-mails, as documented for
#: /api/ticket/create and /api/ticket/<id>/apply.
AUTOMAIL_ARGUMENT = "ticketextension.automail"

#: Its four documented values.
AUTOMAIL_VALUES = ("NEVER", "NO_MAILS_TO_ENDUSER", "SERVERSETTING", "ALWAYS")


def parse_action_ids(raw: str | None) -> tuple[str, ...]:
    """Split a comma separated list of ticket action ids into a tuple."""
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def validate_automail(value: str | None) -> str | None:
    """Check an automail default at startup instead of losing it silently.

    An unknown action argument value is discarded by the HelpDesk with nothing
    but a debug log entry, so a typo here would quietly send the mails it was
    meant to prevent.
    """
    if value is None:
        return None
    normalized = value.strip().upper()
    if normalized not in AUTOMAIL_VALUES:
        raise ConfigurationError(
            f"Unknown automail default {value!r}; use one of {', '.join(AUTOMAIL_VALUES)}."
        )
    return normalized


@dataclass(frozen=True)
class ActionPolicy:
    """Which ticket actions may be applied, and how mails are handled."""

    allowed: tuple[str, ...] = ()
    denied: tuple[str, ...] = ()
    default_automail: str | None = None

    @property
    def restricts_actions(self) -> bool:
        return bool(self.allowed or self.denied)

    def permits(self, action_id: Any) -> bool:
        action = str(action_id).strip()
        if self.denied and action in self.denied:
            return False
        if self.allowed and action not in self.allowed:
            return False
        return True

    def check(self, action_id: Any) -> None:
        """Raise before the request leaves the process if the action is barred."""
        if self.permits(action_id):
            return
        if self.allowed:
            detail = f"Allowed ticket actions: {', '.join(self.allowed)}."
        else:
            detail = f"Blocked ticket actions: {', '.join(self.denied)}."
        raise HelpdeskError(
            f"The ticket action {str(action_id).strip()!r} is not allowed by this "
            f"MCP server's configuration. {detail}"
        )

    def filter_actions(self, actions: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
        """Drop barred entries from a ``/actions`` response.

        Returns the remaining actions and how many were hidden, so the agent can
        tell "not permitted here" apart from "not available for this ticket".
        """
        if not self.restricts_actions:
            return dict(actions), 0
        kept = {key: value for key, value in actions.items() if self.permits(key)}
        return kept, len(actions) - len(kept)

    def apply_automail(
        self, action_arguments: Mapping[str, str] | None
    ) -> dict[str, str] | None:
        """Add the configured automail default unless the caller set one."""
        if not self.default_automail:
            return dict(action_arguments) if action_arguments else None
        arguments = dict(action_arguments or {})
        arguments.setdefault(AUTOMAIL_ARGUMENT, self.default_automail)
        return arguments
