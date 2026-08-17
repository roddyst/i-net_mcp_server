"""Exception types raised by the i-net HelpDesk MCP server."""

from __future__ import annotations


class HelpdeskError(Exception):
    """Base class for every error this server reports back to the agent."""


class ConfigurationError(HelpdeskError):
    """The server (or the current request) is not configured well enough to run."""


class AuthenticationError(HelpdeskError):
    """No usable credentials, or the HelpDesk rejected the ones we sent."""


class ApiError(HelpdeskError):
    """The HelpDesk Web-API answered with a non-2xx status."""

    def __init__(self, status_code: int, method: str, path: str, body: str) -> None:
        self.status_code = status_code
        self.method = method
        self.path = path
        self.body = body
        snippet = body.strip().replace("\n", " ")
        if len(snippet) > 500:
            snippet = snippet[:500] + " ..."
        hint = ""
        if status_code in (401, 403):
            hint = (
                " Check the access token and make sure the user has the "
                '"Web API" permission in the i-net HelpDesk.'
            )
        elif status_code == 404:
            hint = " The ticket, step or endpoint does not exist (or is not visible to this user)."
        super().__init__(
            f"i-net HelpDesk returned HTTP {status_code} for {method} {path}."
            f"{hint} Response: {snippet or '<empty>'}"
        )


class TransportError(HelpdeskError):
    """The HelpDesk could not be reached at all (DNS, TLS, timeout, ...)."""
