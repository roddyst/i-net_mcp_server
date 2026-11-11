"""The guard rails against a misled agent and against untrusted clients."""

from __future__ import annotations

import os
import pathlib

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from inet_helpdesk_mcp.attachments import MAX_ATTACHMENTS, Attachment, prepare_attachments
from inet_helpdesk_mcp.cli import build_http_app
from inet_helpdesk_mcp.client import path_segment
from inet_helpdesk_mcp.config import (
    RequestConfig,
    Settings,
    normalize_base_url,
    resolve_request_config,
)
from inet_helpdesk_mcp.errors import ConfigurationError, HelpdeskError, TransportError
from inet_helpdesk_mcp.http_auth import ClientKeyMiddleware
from inet_helpdesk_mcp.policy import ActionPolicy, canonical_action_id, parse_action_ids
from inet_helpdesk_mcp.server import build_server

from conftest import BASE_URL, RecordingHelpDesk

KEY = "k" * 32


# -- ids in request paths --------------------------------------------------------


@pytest.mark.parametrize(
    "ticket_id",
    ["../../admin", "1/../../secret?x=", "1/actions", "5#", "5?fields=x", "..", ".",
     "..%2f..%2fadmin", "1\\2", "1 2", "1\n2", "", "9" * 200],
)
def test_ids_cannot_leave_their_path_segment(ticket_id: str) -> None:
    with pytest.raises(HelpdeskError, match="not a valid ticket id"):
        path_segment(ticket_id)


@pytest.mark.parametrize("ticket_id", ["4711", " 4711 ", "-12", "AbC-12_x.y", 4711])
def test_ordinary_ids_pass(ticket_id) -> None:
    assert path_segment(ticket_id) == str(ticket_id).strip()


async def test_a_traversing_ticket_id_never_reaches_the_helpdesk(
    helpdesk: RecordingHelpDesk,
) -> None:
    client = helpdesk.client()
    for call in (
        lambda: client.get_ticket("../../admin/users"),
        lambda: client.get_ticket_actions("1/../../x?"),
        lambda: client.get_ticket_steps("../x"),
        lambda: client.get_ticket_step("1", "../../x"),
        lambda: client.apply_action("../../admin/delete?", {"actionId": "1"}),
    ):
        with pytest.raises(HelpdeskError):
            await call()
    assert helpdesk.requests == []


@pytest.fixture
def patched_client(monkeypatch: pytest.MonkeyPatch, helpdesk: RecordingHelpDesk):
    monkeypatch.setattr(
        "inet_helpdesk_mcp.server.HelpdeskClient", lambda config, **_: helpdesk.client(config)
    )
    return helpdesk


def build(**overrides):
    return build_server(Settings(base_url=BASE_URL, token="test-token", **overrides).finalize())


async def test_tools_reject_a_traversing_ticket_id(patched_client: RecordingHelpDesk) -> None:
    server = build()
    for name, arguments in (
        ("get_ticket", {"ticket_id": "../../admin"}),
        ("list_ticket_actions", {"ticket_id": "1/../../x?"}),
        ("apply_ticket_action", {"ticket_id": "../x?", "action_id": "-9"}),
    ):
        with pytest.raises(ToolError, match="not a valid ticket id"):
            await server.call_tool(name, arguments)
    assert patched_client.requests == []


async def test_dry_run_validates_the_ticket_id_too() -> None:
    with pytest.raises(ToolError, match="not a valid ticket id"):
        await build(dry_run=True).call_tool(
            "apply_ticket_action", {"ticket_id": "../x", "action_id": "-9"}
        )


# -- redirects ---------------------------------------------------------------------


async def test_redirects_are_reported_not_followed(helpdesk: RecordingHelpDesk) -> None:
    helpdesk.routes[("GET", "/api/ticket/1")] = lambda request: httpx.Response(
        302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
    )

    with pytest.raises(TransportError, match="redirect"):
        await helpdesk.client().get_ticket("1")
    assert len(helpdesk.requests) == 1


def test_the_real_client_does_not_follow_redirects() -> None:
    from inet_helpdesk_mcp.client import HelpdeskClient

    client = HelpdeskClient(RequestConfig(base_url=BASE_URL, authorization="Bearer x"))
    assert client._client.follow_redirects is False


# -- action policy -----------------------------------------------------------------


@pytest.mark.parametrize("spelling", ["5", " 5 ", "+5", "05", "005", "+05"])
def test_every_spelling_of_a_denied_action_is_denied(spelling: str) -> None:
    policy = ActionPolicy(denied=("5",))
    assert canonical_action_id(spelling) == "5"
    assert not policy.permits(spelling)
    with pytest.raises(HelpdeskError, match="not allowed"):
        policy.check(spelling)


@pytest.mark.parametrize("value", ["", "abc", "5.0", "5;7", "-", "1e3", "5 OR 1"])
def test_non_numeric_action_ids_are_rejected(value: str) -> None:
    with pytest.raises(HelpdeskError, match="not a ticket action id"):
        canonical_action_id(value)
    assert not ActionPolicy().permits(value)


def test_negative_zero_padded_ids_are_folded() -> None:
    assert canonical_action_id("-029") == "-29"
    assert not ActionPolicy(denied=("-29",)).permits("-029")


def test_a_typo_in_the_deny_list_fails_at_startup() -> None:
    assert parse_action_ids(" -2 , +05 ,") == ("-2", "5")
    with pytest.raises(ConfigurationError, match="Invalid list"):
        parse_action_ids("-2,close")


async def test_apply_sends_the_canonical_action_id(patched_client: RecordingHelpDesk) -> None:
    patched_client.route("POST", "/api/ticket/7/apply", 99)

    await build().call_tool("apply_ticket_action", {"ticket_id": "7", "action_id": " +09 "})

    assert patched_client.last_json()["actionId"] == "9"


async def test_a_denied_action_cannot_be_smuggled_past_the_policy(
    patched_client: RecordingHelpDesk,
) -> None:
    server = build(denied_actions=("7",))
    for spelling in ("07", "+7", " 7"):
        with pytest.raises(ToolError, match="not allowed"):
            await server.call_tool(
                "apply_ticket_action", {"ticket_id": "1", "action_id": spelling}
            )
    assert patched_client.requests == []


# -- attachments -------------------------------------------------------------------


def test_a_path_outside_the_released_directory_is_refused(tmp_path: pathlib.Path) -> None:
    released = tmp_path / "outbox"
    released.mkdir()
    secret = tmp_path / "id_rsa"
    secret.write_text("private key")

    for path in (secret, released / ".." / "id_rsa"):
        with pytest.raises(HelpdeskError, match="outside the directories"):
            prepare_attachments(
                [Attachment(path=str(path))], allow_local_files=True, allowed_dirs=[str(released)]
            )


def test_a_symlink_cannot_point_out_of_the_released_directory(tmp_path: pathlib.Path) -> None:
    released = tmp_path / "outbox"
    released.mkdir()
    secret = tmp_path / "id_rsa"
    secret.write_text("private key")
    (released / "harmless.txt").symlink_to(secret)

    with pytest.raises(HelpdeskError, match="outside the directories"):
        prepare_attachments(
            [Attachment(path=str(released / "harmless.txt"))],
            allow_local_files=True,
            allowed_dirs=[str(released)],
        )


def test_a_sibling_directory_with_the_same_prefix_is_outside(tmp_path: pathlib.Path) -> None:
    (tmp_path / "outbox").mkdir()
    (tmp_path / "outbox-private").mkdir()
    secret = tmp_path / "outbox-private" / "a.txt"
    secret.write_text("x")

    with pytest.raises(HelpdeskError, match="outside the directories"):
        prepare_attachments(
            [Attachment(path=str(secret))],
            allow_local_files=True,
            allowed_dirs=[str(tmp_path / "outbox")],
        )


def test_a_file_inside_the_released_directory_is_read(tmp_path: pathlib.Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "report.pdf").write_bytes(b"%PDF")

    prepared = prepare_attachments(
        [Attachment(path=str(tmp_path / "sub" / "report.pdf"))],
        allow_local_files=True,
        allowed_dirs=[str(tmp_path)],
    )

    assert prepared[0][0]["name"] == "report.pdf"
    assert prepared[0][1] == b"%PDF"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_only_regular_files_are_read(tmp_path: pathlib.Path) -> None:
    os.mkfifo(tmp_path / "pipe")

    with pytest.raises(HelpdeskError, match="not a regular file"):
        prepare_attachments([Attachment(path=str(tmp_path / "pipe"))], allow_local_files=True)
    with pytest.raises(HelpdeskError, match="not a regular file|Could not read"):
        prepare_attachments([Attachment(path=str(tmp_path))], allow_local_files=True)


def test_the_number_of_attachments_is_limited() -> None:
    many = [Attachment(name="a", content_base64="QQ==")] * (MAX_ATTACHMENTS + 1)

    with pytest.raises(HelpdeskError, match="At most"):
        prepare_attachments(many, allow_local_files=False)


def test_the_total_size_of_attachments_is_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("inet_helpdesk_mcp.attachments.MAX_TOTAL_ATTACHMENT_BYTES", 3)

    with pytest.raises(HelpdeskError, match="in total"):
        prepare_attachments(
            [Attachment(name="a", content_base64="QUFB"), Attachment(name="b", content_base64="QQ==")],
            allow_local_files=False,
        )


async def test_a_default_stdio_server_reads_no_local_files(tmp_path: pathlib.Path) -> None:
    secret = tmp_path / "id_rsa"
    secret.write_text("private key")

    with pytest.raises(ToolError, match="disabled on this server"):
        await build(dry_run=True).call_tool(
            "create_ticket", {"text": "x", "attachments": [{"path": str(secret)}]}
        )


# -- base URL ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@helpdesk.example.com",
        "https://helpdesk.example.com/?x=",
        "https://helpdesk.example.com/x?",
        "https://helpdesk.example.com/#",
        "https://helpdesk.example.com:port",
        "https://",
        "https://help desk.example.com",
        "https://helpdesk.example.com\\@evil.example",
    ],
)
def test_base_urls_with_more_than_host_and_path_are_rejected(url: str) -> None:
    with pytest.raises(ConfigurationError):
        normalize_base_url(url)


def test_ordinary_base_urls_still_pass() -> None:
    assert normalize_base_url("helpdesk.example.com:9000/") == "http://helpdesk.example.com:9000"
    assert normalize_base_url("https://hd.example.com/helpdesk/api") == "https://hd.example.com/helpdesk"
    assert normalize_base_url("https://[::1]:9000") == "https://[::1]:9000"


def test_the_url_header_is_limited_to_the_listed_servers() -> None:
    settings = Settings(
        transport="http", allowed_base_urls=("https://a.example.com:9000",)
    ).finalize()
    assert settings.allow_url_header is True

    config = resolve_request_config(
        settings,
        {"X-Inet-Base-Url": "https://A.Example.com:9000/", "Authorization": "Bearer t"},
    )
    assert config.base_url == "https://A.Example.com:9000"

    for url in ("https://b.example.com:9000", "http://a.example.com:9000",
                "https://a.example.com", "http://169.254.169.254"):
        with pytest.raises(ConfigurationError, match="not one of the servers"):
            resolve_request_config(
                settings, {"X-Inet-Base-Url": url, "Authorization": "Bearer t"}
            )


# -- startup warnings --------------------------------------------------------------


def test_a_safe_local_setup_has_no_warnings() -> None:
    assert Settings(base_url=BASE_URL, token="t").finalize().security_warnings() == []
    assert (
        Settings(base_url=BASE_URL, token="t", transport="http").finalize().security_warnings()
        == []
    )


def test_an_exposed_service_account_is_warned_about() -> None:
    exposed = Settings(base_url=BASE_URL, token="t", transport="http", host="0.0.0.0").finalize()
    assert any("does not authenticate" in w for w in exposed.security_warnings())

    keyed = Settings(
        base_url=BASE_URL, token="t", transport="http", host="0.0.0.0", client_key=KEY
    ).finalize()
    assert not any("does not authenticate" in w for w in keyed.security_warnings())


def test_other_open_doors_are_warned_about() -> None:
    proxy = Settings(transport="http", host="0.0.0.0").finalize()
    assert any("X-Inet-Base-Url" in w for w in proxy.security_warnings())

    insecure = Settings(base_url="http://hd.example.com", token="t", verify_tls=False).finalize()
    warnings = " ".join(insecure.security_warnings())
    assert "not verified" in warnings and "unencrypted" in warnings

    local = Settings(base_url="http://127.0.0.1:9000", token="t").finalize()
    assert local.security_warnings() == []


def test_a_short_client_key_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="at least"):
        Settings(base_url=BASE_URL, client_key="short").finalize()


def test_the_client_key_never_shows_up_in_repr_or_description() -> None:
    settings = Settings(base_url=BASE_URL, token="t", client_key=KEY).finalize()
    assert KEY not in repr(settings)
    assert KEY not in settings.describe()


# -- client key for the HTTP transports -------------------------------------------------


async def _status(app, headers: dict[str, str]) -> int:
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    transport = httpx.ASGITransport(app=app(inner))
    async with httpx.AsyncClient(transport=transport, base_url="http://mcp") as client:
        return (await client.post("/mcp", headers=headers, json={})).status_code


async def test_requests_without_the_key_are_rejected() -> None:
    def app(inner):
        return ClientKeyMiddleware(inner, KEY)

    assert await _status(app, {}) == 401
    assert await _status(app, {"X-Inet-Mcp-Key": "wrong" * 8}) == 401
    assert await _status(app, {"X-Inet-Mcp-Key": KEY[:-1]}) == 401
    assert await _status(app, {"X-Inet-Mcp-Key": KEY}) == 200
    # Without --ignore-client-auth the Authorization header is the HelpDesk
    # token of the user and must not double as the client key.
    assert await _status(app, {"Authorization": f"Bearer {KEY}"}) == 401


async def test_with_a_service_account_the_key_may_be_a_bearer_token() -> None:
    def app(inner):
        return ClientKeyMiddleware(inner, KEY, accept_authorization=True)

    assert await _status(app, {"Authorization": f"Bearer {KEY}"}) == 200
    assert await _status(app, {"Authorization": f"bearer {KEY}"}) == 200
    assert await _status(app, {"Authorization": f"Basic {KEY}"}) == 401
    assert await _status(app, {"Authorization": "Bearer nope"}) == 401
    assert await _status(app, {"X-Inet-Mcp-Key": KEY}) == 200


async def test_the_mcp_endpoint_itself_sits_behind_the_key() -> None:
    settings = Settings(
        base_url=BASE_URL, token="t", transport="http", ignore_client_auth=True, client_key=KEY
    ).finalize()
    app = build_http_app(build_server(settings), settings)
    assert isinstance(app, ClientKeyMiddleware)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


# -- audit log ---------------------------------------------------------------------


def test_audit_values_cannot_forge_log_lines(caplog: pytest.LogCaptureFixture) -> None:
    from inet_helpdesk_mcp.server import HelpdeskTools

    tools = HelpdeskTools(Settings(base_url=BASE_URL, token="t").finalize())
    with caplog.at_level("INFO", logger="inet_helpdesk_mcp.server"):
        tools.audit("apply", ticket="1\n2026-01-01 INFO forged entry", action="-9")

    assert "\n" not in caplog.records[-1].getMessage()
