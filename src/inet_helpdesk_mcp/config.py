"""Configuration for the i-net HelpDesk MCP server.

All settings can be supplied as environment variables (``INET_*``) and most of
them can be overridden on the command line.  In HTTP mode the per-request
``Authorization`` header takes precedence over any configured credentials, so a
single server process can serve several users.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from typing import Mapping

from .errors import AuthenticationError, ConfigurationError

logger = logging.getLogger(__name__)

ENV_PREFIX = "INET_"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

#: Standard CA bundle variables of OpenSSL and requests, honoured when no
#: INET_CA_BUNDLE is configured. httpx itself only looks at SSL_CERT_FILE.
CA_BUNDLE_ENV_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")


def _env(name: str) -> str | None:
    value = os.environ.get(ENV_PREFIX + name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_bool(name: str) -> bool | None:
    raw = _env(name)
    if raw is None:
        return None
    lowered = raw.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigurationError(
        f"Environment variable {ENV_PREFIX}{name} must be a boolean "
        f"(one of {sorted(_TRUE | _FALSE)}), got {raw!r}."
    )


def _env_number(name: str, cast):
    raw = _env(name)
    if raw is None:
        return None
    try:
        return cast(raw)
    except ValueError as exc:
        raise ConfigurationError(
            f"Environment variable {ENV_PREFIX}{name} must be a number, got {raw!r}."
        ) from exc


def ca_bundle_from_env() -> str | None:
    """The first usable CA bundle from the standard environment variables.

    A variable that points nowhere is skipped instead of failing the request
    later: these are ambient settings of the machine, not of this server.
    """
    for name in CA_BUNDLE_ENV_VARS:
        value = (os.environ.get(name) or "").strip()
        if not value:
            continue
        if not os.path.isfile(value):
            logger.warning("Ignoring %s=%r: not a readable file.", name, value)
            continue
        return value
    return None


def normalize_base_url(base_url: str) -> str:
    """Return *base_url* without a trailing slash and with an explicit scheme."""
    url = base_url.strip().rstrip("/")
    if not url:
        raise ConfigurationError("The i-net HelpDesk base URL must not be empty.")
    if "://" not in url:
        url = "http://" + url
    if not url.startswith(("http://", "https://")):
        raise ConfigurationError(
            f"The i-net HelpDesk base URL must use http or https, got {base_url!r}."
        )
    # Tolerate people pasting the API path itself.
    if url.endswith("/api"):
        url = url[: -len("/api")]
    return url


@dataclass(frozen=True)
class Settings:
    """Static configuration of one server process."""

    base_url: str | None = None
    token: str | None = None
    username: str | None = None
    password: str | None = None
    transport: str = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000
    http_path: str = "/mcp"
    timeout: float = 30.0
    verify_tls: bool = True
    #: CA bundle used instead of the certifi default, e.g. an internal company CA.
    ca_bundle: str | None = None
    allow_local_files: bool = True
    allow_url_header: bool = False
    ignore_client_auth: bool = False
    read_only: bool = False
    default_locale: str = "en"
    # Remember which of the two derived flags the operator set explicitly, so
    # finalize() can recompute the others deterministically after an override.
    url_header_explicit: bool = field(default=False, repr=False, compare=False)
    local_files_explicit: bool = field(default=False, repr=False, compare=False)

    @classmethod
    def from_env(cls) -> "Settings":
        base_url = _env("BASE_URL") or _env("URL")
        allow_url_header = _env_bool("ALLOW_URL_HEADER")
        verify_tls = _env_bool("VERIFY_TLS")
        allow_local_files = _env_bool("ALLOW_LOCAL_FILES")
        timeout = _env_number("TIMEOUT", float)
        port = _env_number("PORT", int)

        return cls(
            base_url=normalize_base_url(base_url) if base_url else None,
            token=_env("TOKEN") or _env("API_TOKEN"),
            username=_env("USERNAME"),
            password=_env("PASSWORD"),
            transport=(_env("TRANSPORT") or "stdio").lower(),
            host=_env("HOST") or "127.0.0.1",
            port=port if port is not None else 8000,
            http_path=_env("HTTP_PATH") or "/mcp",
            timeout=timeout if timeout is not None else 30.0,
            verify_tls=True if verify_tls is None else verify_tls,
            ca_bundle=_env("CA_BUNDLE"),
            allow_local_files=True if allow_local_files is None else allow_local_files,
            allow_url_header=bool(allow_url_header),
            ignore_client_auth=bool(_env_bool("IGNORE_CLIENT_AUTH")),
            read_only=bool(_env_bool("READ_ONLY")),
            default_locale=_env("LOCALE") or "en",
            url_header_explicit=allow_url_header is not None,
            local_files_explicit=allow_local_files is not None,
        ).finalize()

    def finalize(self) -> "Settings":
        """Apply the defaults that depend on other settings."""
        if self.transport not in ("stdio", "http", "streamable-http", "sse"):
            raise ConfigurationError(
                f"Unknown transport {self.transport!r}; use 'stdio', 'http' or 'sse'."
            )
        if self.ca_bundle:
            if not self.verify_tls:
                raise ConfigurationError(
                    "--ca-bundle (INET_CA_BUNDLE) and --no-verify-tls "
                    "(INET_VERIFY_TLS=false) contradict each other: either check the "
                    "HelpDesk certificate against that CA bundle or do not check it at all."
                )
            if not os.path.isfile(self.ca_bundle):
                raise ConfigurationError(
                    f"The CA bundle {self.ca_bundle!r} is not a readable file. Pass the "
                    "PEM file of the issuing CA, e.g. /etc/ssl/certs/ca-certificates.crt."
                )
        updates: dict[str, object] = {}
        if self.transport == "streamable-http":
            updates["transport"] = "http"
        transport = str(updates.get("transport", self.transport))
        # Without a configured base URL the client has to name the HelpDesk
        # itself, so the X-Inet-Base-Url header is enabled by default there.
        # Derived, not sticky: a later --base-url turns it off again.
        if not self.url_header_explicit:
            updates["allow_url_header"] = self.base_url is None
        # Reading files from the server's disk only makes sense when agent and
        # server share that disk, i.e. for the stdio transport.
        if transport != "stdio":
            updates["allow_local_files"] = False
        elif not self.local_files_explicit:
            updates["allow_local_files"] = True
        return replace(self, **updates) if updates else self

    @property
    def has_credentials(self) -> bool:
        return bool(self.token or (self.username and self.password))

    def tls_verify(self) -> str | bool:
        """How the HelpDesk certificate is verified, as the client's ``verify``.

        ``False`` switches verification off, a string is the CA bundle to trust
        and ``True`` keeps the client's own default bundle (certifi).  A
        configured CA bundle wins over the SSL_CERT_FILE / REQUESTS_CA_BUNDLE
        variables, which in turn win over certifi.
        """
        if not self.verify_tls:
            return False
        return self.ca_bundle or ca_bundle_from_env() or True

    def describe_tls(self) -> str:
        """A short, human readable form of :meth:`tls_verify` for diagnostics."""
        verify = self.tls_verify()
        if verify is False:
            return "certificate check disabled"
        if verify is True:
            return "default CA bundle (certifi)"
        return f"CA bundle {verify}"

    def describe(self) -> str:
        """A short, secret-free summary used for logging and the ``server_info`` tool."""
        if self.token:
            auth = "bearer token from configuration"
        elif self.username and self.password:
            auth = f"basic auth as {self.username} (from configuration)"
        else:
            auth = "per-request Authorization header"
        if self.ignore_client_auth:
            auth += " (client Authorization headers ignored)"
        return (
            f"transport={self.transport} "
            f"base_url={self.base_url or '<from X-Inet-Base-Url header>'} "
            f"auth={auth} read_only={self.read_only} "
            f"tls={self.describe_tls()}"
        )


@dataclass(frozen=True)
class RequestConfig:
    """The effective connection settings for a single tool call."""

    base_url: str
    authorization: str | None = None
    username: str | None = None
    password: str | None = None
    #: Where the credentials came from, for diagnostics. Never the secret itself.
    source: str = "configuration"

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": self.authorization} if self.authorization else {}


def resolve_request_config(
    settings: Settings, headers: Mapping[str, str] | None = None
) -> RequestConfig:
    """Merge the static settings with the headers of the current request.

    Client supplied headers win: that is what lets one HTTP server instance
    serve several users, each with their own i-net HelpDesk access token.
    With ``ignore_client_auth`` the configured credentials win instead, for
    deployments that authenticate every call as one service account.
    """
    header_map = {k.lower(): v for k, v in (headers or {}).items()}

    base_url = settings.base_url
    header_url = header_map.get("x-inet-base-url")
    if header_url:
        if not settings.allow_url_header:
            raise ConfigurationError(
                "The X-Inet-Base-Url header was sent but is not allowed. "
                "Start the server with --allow-url-header (or INET_ALLOW_URL_HEADER=true) "
                "to let clients choose the HelpDesk server."
            )
        base_url = normalize_base_url(header_url)
    if not base_url:
        raise ConfigurationError(
            "No i-net HelpDesk base URL configured. Set INET_BASE_URL "
            "(or pass --base-url), or send an X-Inet-Base-Url header."
        )

    authorization = header_map.get("authorization")
    if authorization and authorization.strip() and not settings.ignore_client_auth:
        return RequestConfig(
            base_url=base_url,
            authorization=authorization.strip(),
            source="Authorization header of this request",
        )
    if settings.token:
        return RequestConfig(
            base_url=base_url,
            authorization=f"Bearer {settings.token}",
            source="bearer token from the server configuration",
        )
    if settings.username and settings.password:
        return RequestConfig(
            base_url=base_url,
            username=settings.username,
            password=settings.password,
            source=f"basic auth as {settings.username}, from the server configuration",
        )
    raise AuthenticationError(
        "No credentials available. Set INET_TOKEN (bearer token) or "
        "INET_USERNAME/INET_PASSWORD, or send an Authorization header with the request."
    )
