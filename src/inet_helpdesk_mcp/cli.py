"""Command line entry point for the i-net HelpDesk MCP server."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace

from . import __version__
from .config import Settings, normalize_base_url
from .errors import HelpdeskError
from .policy import AUTOMAIL_VALUES, parse_action_ids
from .server import build_server

logger = logging.getLogger("inet_helpdesk_mcp")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inet-helpdesk-mcp",
        description=(
            "MCP server for the i-net HelpDesk Ticket Web-API. Every option can also "
            "be given as an INET_* environment variable (e.g. INET_BASE_URL)."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--base-url",
        help="Base URL of the HelpDesk server, e.g. https://helpdesk.example.com:9000",
    )
    parser.add_argument("--token", help="Access token used as 'Authorization: Bearer <token>'.")
    parser.add_argument("--username", help="User name for basic authentication.")
    parser.add_argument("--password", help="Password for basic authentication.")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "sse"],
        help="MCP transport (default: stdio). 'http' serves streamable HTTP.",
    )
    parser.add_argument("--host", help="Bind address for the HTTP transports (default 127.0.0.1).")
    parser.add_argument("--port", type=int, help="Port for the HTTP transports (default 8000).")
    parser.add_argument("--http-path", help="Path of the streamable HTTP endpoint (default /mcp).")
    parser.add_argument("--timeout", type=float, help="HTTP timeout in seconds (default 30).")
    parser.add_argument(
        "--no-verify-tls",
        action="store_true",
        help="Do not verify the HelpDesk TLS certificate (self-signed test systems only).",
    )
    parser.add_argument(
        "--ca-bundle",
        metavar="PATH",
        help=(
            "PEM file with the CA certificates to trust, for a HelpDesk behind an "
            "internal company CA, e.g. /etc/ssl/certs/ca-certificates.crt. Without it "
            "SSL_CERT_FILE and REQUESTS_CA_BUNDLE are honoured, then the bundled "
            "certifi store. Cannot be combined with --no-verify-tls."
        ),
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Only expose reading tools; create_ticket and apply_ticket_action are hidden.",
    )
    parser.add_argument(
        "--allow-url-header",
        action="store_true",
        help="Let clients select the HelpDesk server with an X-Inet-Base-Url header.",
    )
    parser.add_argument(
        "--ignore-client-auth",
        action="store_true",
        help=(
            "Always use the configured credentials and ignore Authorization headers "
            "sent by clients. Use this when the server owns a service account token."
        ),
    )
    parser.add_argument(
        "--no-local-files",
        action="store_true",
        help="Refuse attachments that reference a path on the server's file system.",
    )
    parser.add_argument("--locale", help="Default locale for ticket searches (default 'en').")
    parser.add_argument(
        "--no-normalize",
        action="store_true",
        help=(
            "Return the Web-API answers unchanged instead of merging display values, "
            "adding ISO timestamps and converting HTML step texts to text."
        ),
    )
    parser.add_argument(
        "--retries",
        type=int,
        help="Repetitions of a failed GET request (default 2). POST is never repeated.",
    )
    parser.add_argument(
        "--pool-size",
        type=int,
        help="How many HelpDesk connections are kept alive at once (default 8).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Validate create_ticket and apply_ticket_action and report the request "
            "that would be sent, without sending it."
        ),
    )
    parser.add_argument(
        "--allowed-actions",
        metavar="IDS",
        help="Comma separated ticket action ids that apply_ticket_action may use.",
    )
    parser.add_argument(
        "--denied-actions",
        metavar="IDS",
        help="Comma separated ticket action ids that apply_ticket_action must not use.",
    )
    parser.add_argument(
        "--default-automail",
        choices=list(AUTOMAIL_VALUES),
        help=(
            "Value for the ticketextension.automail action argument when a call does "
            "not set one, e.g. NEVER to keep an agent from mailing end users."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Log level for the server's own messages (default INFO).",
    )
    return parser


def settings_from_args(args: argparse.Namespace) -> Settings:
    """Environment first, command line wins."""
    settings = Settings.from_env()
    updates: dict[str, object] = {}

    if args.base_url:
        updates["base_url"] = normalize_base_url(args.base_url)
    if args.token:
        updates["token"] = args.token
    if args.username:
        updates["username"] = args.username
    if args.password:
        updates["password"] = args.password
    if args.transport:
        updates["transport"] = args.transport
    if args.host:
        updates["host"] = args.host
    if args.port:
        updates["port"] = args.port
    if args.http_path:
        updates["http_path"] = args.http_path
    if args.timeout:
        updates["timeout"] = args.timeout
    if args.no_verify_tls:
        updates["verify_tls"] = False
    if args.ca_bundle:
        updates["ca_bundle"] = args.ca_bundle
    if args.read_only:
        updates["read_only"] = True
    if args.allow_url_header:
        updates["allow_url_header"] = True
        updates["url_header_explicit"] = True
    if args.ignore_client_auth:
        updates["ignore_client_auth"] = True
    if args.no_local_files:
        updates["allow_local_files"] = False
        updates["local_files_explicit"] = True
    if args.locale:
        updates["default_locale"] = args.locale
    if args.no_normalize:
        updates["normalize"] = False
    if args.retries is not None:
        updates["retries"] = args.retries
    if args.pool_size is not None:
        updates["pool_size"] = args.pool_size
    if args.dry_run:
        updates["dry_run"] = True
    if args.allowed_actions:
        updates["allowed_actions"] = parse_action_ids(args.allowed_actions)
    if args.denied_actions:
        updates["denied_actions"] = parse_action_ids(args.denied_actions)
    if args.default_automail:
        updates["default_automail"] = args.default_automail

    return replace(settings, **updates).finalize() if updates else settings


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        settings = settings_from_args(args)
    except HelpdeskError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if not settings.has_credentials and (
        settings.transport == "stdio" or settings.ignore_client_auth
    ):
        reason = (
            "the stdio transport"
            if settings.transport == "stdio"
            else "--ignore-client-auth"
        )
        print(
            f"Warning: no token and no user name configured, but {reason} needs one. "
            "Set INET_TOKEN (or INET_USERNAME/INET_PASSWORD) - tool calls will fail "
            "otherwise.",
            file=sys.stderr,
        )

    server = build_server(settings)
    logger.info("Starting i-net HelpDesk MCP server (%s)", settings.describe())

    if settings.transport == "stdio":
        server.run("stdio")
    elif settings.transport == "sse":
        server.run("sse", host=settings.host, port=settings.port)
    else:
        server.run(
            "streamable-http",
            host=settings.host,
            port=settings.port,
            streamable_http_path=settings.http_path,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
