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
from urllib.parse import urlsplit

from .actions import parse_action_id_overrides
from .errors import AuthenticationError, ConfigurationError
from .policy import ActionPolicy, parse_action_ids, validate_automail

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


#: Shortest accepted INET_CLIENT_KEY; shorter ones can be guessed.
MIN_CLIENT_KEY_LENGTH = 16

_LOOPBACK_HOSTS = ("localhost", "::1", "[::1]")


def is_loopback(host: str | None) -> bool:
    """True for an address that only this machine can reach."""
    host = (host or "").strip().lower()
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def normalize_base_url(base_url: str) -> str:
    """Return *base_url* without a trailing slash and with an explicit scheme."""
    url = base_url.strip()
    if not url.rstrip("/"):
        raise ConfigurationError("The i-net HelpDesk base URL must not be empty.")
    if "://" not in url:
        url = "http://" + url
    url = url.rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ConfigurationError(
            f"The i-net HelpDesk base URL must use http or https, got {base_url!r}."
        )
    # The URL may come from a request header. Only scheme, host, port and a
    # context path are accepted: credentials in the URL would be sent along,
    # and a query or fragment would swallow the API path appended later.
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError for a port that is no number
    except ValueError as exc:
        raise ConfigurationError(f"The i-net HelpDesk base URL {base_url!r} is malformed.") from exc
    if (
        not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or "?" in url
        or "#" in url
        or "\\" in url
        or any(char.isspace() or not char.isprintable() for char in url)
    ):
        raise ConfigurationError(
            f"The i-net HelpDesk base URL must consist of scheme, host, port and an "
            f"optional path only, got {base_url!r}."
        )
    # Tolerate people pasting the API path itself.
    if url.endswith("/api"):
        url = url[: -len("/api")]
    return url


def _url_key(url: str) -> str:
    """A base URL in the form two spellings of the same server share."""
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"{parts.scheme.lower()}://{(parts.hostname or '').lower()}:{port}{parts.path}"


def parse_base_urls(raw: str | None) -> tuple[str, ...]:
    """Split a comma separated list of HelpDesk base URLs."""
    if not raw:
        return ()
    return tuple(normalize_base_url(part) for part in raw.split(",") if part.strip())


def parse_directories(raw: str | None) -> tuple[str, ...]:
    """Split a list of directories, separated like PATH (``:`` or ``;`` on Windows)."""
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(os.pathsep) if part.strip())


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
    #: Directories a local attachment has to lie in. Without any, local files
    #: are only read when the operator switched them on explicitly.
    attachment_dirs: tuple[str, ...] = ()
    allow_url_header: bool = False
    #: The HelpDesk servers a client may select with X-Inet-Base-Url; empty
    #: means any address.
    allowed_base_urls: tuple[str, ...] = ()
    #: Shared secret every client of the HTTP transports has to present.
    client_key: str | None = field(default=None, repr=False)
    ignore_client_auth: bool = False
    read_only: bool = False
    default_locale: str = "en"
    #: Reshape Web-API answers for the agent (display values, ISO timestamps,
    #: HTML steps as text). Single tool calls can still ask for raw output.
    normalize: bool = True
    #: Repetitions of a failed GET; POST is never repeated.
    retries: int = 2
    #: Build the request and report it back instead of sending it.
    dry_run: bool = False
    #: Ticket action ids the agent may (not) apply; empty means no restriction.
    allowed_actions: tuple[str, ...] = ()
    denied_actions: tuple[str, ...] = ()
    #: Value for the ticketextension.automail argument when the caller sets none.
    default_automail: str | None = None
    #: Mailbox send_email sends from (step field reastepemailin) when the call
    #: names none, e.g. the HelpDesk's support address.
    mail_from: str | None = None
    #: Per-installation ticket action ids of the action tools, as (tool, id) pairs.
    action_ids: tuple[tuple[str, str], ...] = ()
    #: How many HelpDesk connections are kept alive at the same time.
    pool_size: int = 8
    #: How many HelpDesk requests one tool call runs at the same time.
    concurrency: int = 10
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
        normalize = _env_bool("NORMALIZE")
        retries = _env_number("RETRIES", int)
        pool_size = _env_number("POOL_SIZE", int)
        concurrency = _env_number("CONCURRENCY", int)

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
            attachment_dirs=parse_directories(_env("ATTACHMENT_DIR")),
            allowed_base_urls=parse_base_urls(_env("ALLOWED_BASE_URLS")),
            client_key=_env("CLIENT_KEY"),
            allow_url_header=bool(allow_url_header),
            ignore_client_auth=bool(_env_bool("IGNORE_CLIENT_AUTH")),
            read_only=bool(_env_bool("READ_ONLY")),
            default_locale=_env("LOCALE") or "en",
            normalize=True if normalize is None else normalize,
            retries=retries if retries is not None else 2,
            dry_run=bool(_env_bool("DRY_RUN")),
            allowed_actions=parse_action_ids(_env("ALLOWED_ACTIONS")),
            denied_actions=parse_action_ids(_env("DENIED_ACTIONS")),
            default_automail=_env("DEFAULT_AUTOMAIL"),
            mail_from=_env("MAIL_FROM"),
            action_ids=parse_action_id_overrides(_env("ACTION_IDS")),
            pool_size=pool_size if pool_size is not None else 8,
            concurrency=concurrency if concurrency is not None else 10,
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
        automail = validate_automail(self.default_automail)
        if automail != self.default_automail:
            updates["default_automail"] = automail
        if self.retries < 0:
            raise ConfigurationError(
                f"The number of retries must not be negative, got {self.retries}."
            )
        if self.pool_size < 1:
            raise ConfigurationError(
                f"The connection pool needs room for at least one client, got {self.pool_size}."
            )
        if self.concurrency < 1:
            raise ConfigurationError(
                f"At least one request has to run at a time, got a concurrency of {self.concurrency}."
            )
        if self.client_key is not None and len(self.client_key) < MIN_CLIENT_KEY_LENGTH:
            raise ConfigurationError(
                f"INET_CLIENT_KEY must have at least {MIN_CLIENT_KEY_LENGTH} characters; "
                "generate one with: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
            )
        for directory in self.attachment_dirs:
            if not os.path.isdir(directory):
                raise ConfigurationError(
                    f"The attachment directory {directory!r} does not exist."
                )
        if self.transport == "streamable-http":
            updates["transport"] = "http"
        transport = str(updates.get("transport", self.transport))
        # Without a configured base URL the client has to name the HelpDesk
        # itself, so the X-Inet-Base-Url header is enabled by default there -
        # as it is when the operator lists the servers clients may choose from.
        # Derived, not sticky: a later --base-url turns it off again.
        if not self.url_header_explicit:
            updates["allow_url_header"] = self.base_url is None or bool(self.allowed_base_urls)
        # Reading files from the server's disk only makes sense when agent and
        # server share that disk, i.e. for the stdio transport. Even there it
        # is off until the operator releases a directory (or all files): the
        # path comes from the agent, and an agent that read a manipulated
        # ticket could otherwise attach any file of this machine to a ticket
        # or an email.
        if transport != "stdio":
            updates["allow_local_files"] = False
        elif not self.local_files_explicit:
            updates["allow_local_files"] = bool(self.attachment_dirs)
        return replace(self, **updates) if updates else self

    @property
    def has_credentials(self) -> bool:
        return bool(self.token or (self.username and self.password))

    def policy(self) -> ActionPolicy:
        """The guard rails for the writing tools."""
        return ActionPolicy(
            allowed=self.allowed_actions,
            denied=self.denied_actions,
            default_automail=self.default_automail,
        )

    def security_warnings(self) -> list[str]:
        """Settings that work but leave a door open; logged once at startup."""
        warnings: list[str] = []
        http = self.transport != "stdio"
        exposed = http and not is_loopback(self.host)
        if exposed and not self.client_key and self.has_credentials:
            warnings.append(
                f"The server listens on {self.host} and holds HelpDesk credentials, but "
                "does not authenticate its clients: everyone who reaches the port acts "
                "as the configured HelpDesk user. Set INET_CLIENT_KEY, bind to 127.0.0.1 "
                "or put an authenticating reverse proxy in front."
            )
        if exposed and self.allow_url_header and not self.allowed_base_urls:
            warnings.append(
                f"The server listens on {self.host} and lets clients choose the target "
                "with X-Inet-Base-Url: it can be used to send requests to any address "
                "this machine reaches. Set INET_ALLOWED_BASE_URLS or a fixed INET_BASE_URL."
            )
        if exposed:
            warnings.append(
                "The server does not terminate TLS itself: tokens of the clients travel "
                "unencrypted unless a reverse proxy with HTTPS sits in front."
            )
        if not self.verify_tls:
            warnings.append(
                "The certificate of the HelpDesk is not verified (--no-verify-tls): "
                "credentials can be intercepted. Use --ca-bundle for an internal CA."
            )
        if self.base_url and self.base_url.startswith("http://"):
            if not is_loopback(urlsplit(self.base_url).hostname):
                warnings.append(
                    f"The base URL {self.base_url} uses http: the HelpDesk credentials "
                    "and all ticket data travel unencrypted. Use https:// if the "
                    "HelpDesk offers it."
                )
        if self.allow_local_files and not self.attachment_dirs:
            warnings.append(
                "Local file attachments are allowed without INET_ATTACHMENT_DIR: the "
                "agent can attach any file this process can read."
            )
        return warnings

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
            f"auth={auth} read_only={self.read_only} dry_run={self.dry_run} "
            f"normalize={self.normalize} retries={self.retries} "
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
        if settings.allowed_base_urls and _url_key(base_url) not in {
            _url_key(allowed) for allowed in settings.allowed_base_urls
        }:
            raise ConfigurationError(
                "The HelpDesk server named in the X-Inet-Base-Url header is not one of "
                "the servers this MCP server may connect to (INET_ALLOWED_BASE_URLS)."
            )
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
