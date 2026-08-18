"""End-to-end tests: MCP tool call in, i-net Web-API request out."""

from __future__ import annotations

import base64

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from inet_helpdesk_mcp.config import Settings
from inet_helpdesk_mcp.server import build_server

from conftest import BASE_URL, RecordingHelpDesk


@pytest.fixture
def patched_client(monkeypatch: pytest.MonkeyPatch, helpdesk: RecordingHelpDesk):
    """Route every HelpdeskClient the server builds through the fake HelpDesk."""

    def factory(config, **_kwargs):
        return helpdesk.client(config)

    monkeypatch.setattr("inet_helpdesk_mcp.server.HelpdeskClient", factory)
    return helpdesk


async def call(server, name: str, arguments: dict | None = None):
    result = await server.call_tool(name, arguments or {})
    assert not result.is_error, result.content
    return result.structured_content


def build(**overrides):
    settings = Settings(base_url=BASE_URL, token="test-token", **overrides).finalize()
    return build_server(settings)


async def test_tool_inventory() -> None:
    names = {tool.name for tool in await build().list_tools()}
    assert names == {
        "server_info",
        "search_tickets",
        "get_ticket",
        "list_ticket_actions",
        "list_ticket_steps",
        "get_ticket_step",
        "create_ticket",
        "apply_ticket_action",
    }


async def test_read_only_hides_write_tools() -> None:
    names = {tool.name for tool in await build(read_only=True).list_tools()}
    assert "create_ticket" not in names
    assert "apply_ticket_action" not in names
    assert "get_ticket" in names


async def test_search_tickets_tool(patched_client: RecordingHelpDesk) -> None:
    patched_client.route(
        "POST", "/api/ticket/search", {"ticketList": [1, 2, 3], "hasMore": True}
    )

    payload = await call(build(), "search_tickets", {"query": "printer", "limit": 3})

    assert payload["ticketList"] == [1, 2, 3]
    assert patched_client.last_json() == {
        "query": "printer",
        "limit": 3,
        "start": 0,
        "locale": "en",
    }


async def test_get_ticket_tool(patched_client: RecordingHelpDesk) -> None:
    patched_client.route(
        "GET", "/api/ticket/1", {"ticketId": 1, "fields": {"subject": "Welcome"}}
    )

    payload = await call(build(), "get_ticket", {"ticket_id": "1", "fields": "subject"})

    assert payload["fields"]["subject"] == "Welcome"
    assert patched_client.last_request.url.params["fields"] == "subject"


async def test_list_ticket_actions_tool(patched_client: RecordingHelpDesk) -> None:
    patched_client.route("GET", "/api/ticket/1/actions", {"-9": "E-Mail empfangen"})

    payload = await call(build(), "list_ticket_actions", {"ticket_id": "1"})

    assert payload == {"ticketId": "1", "actions": {"-9": "E-Mail empfangen"}}


async def test_list_ticket_steps_tool(patched_client: RecordingHelpDesk) -> None:
    patched_client.route("GET", "/api/ticket/1/steps", [{"id": 1, "actionID": "4"}])

    payload = await call(build(), "list_ticket_steps", {"ticket_id": "1", "since": 5})

    assert payload["steps"] == [{"id": 1, "actionID": "4"}]
    assert patched_client.last_request.url.params["since"] == "5"


async def test_get_ticket_step_tool(patched_client: RecordingHelpDesk) -> None:
    patched_client.route("GET", "/api/ticket/1/steps/2", {"id": 2, "text": "answered"})

    payload = await call(build(), "get_ticket_step", {"ticket_id": "1", "step_id": "2"})

    assert payload["text"] == "answered"


async def test_create_ticket_tool(patched_client: RecordingHelpDesk) -> None:
    patched_client.route("POST", "/api/ticket/create", "1234")

    payload = await call(
        build(),
        "create_ticket",
        {
            "text": "Drucker defekt",
            "action_arguments": {"ticketextension.automail": "NEVER"},
        },
    )

    assert payload == {"ticketId": "1234"}
    assert patched_client.last_json() == {
        "text": "Drucker defekt",
        "htmlContent": False,
        "actionArguments": {"ticketextension.automail": "NEVER"},
    }


async def test_create_ticket_tool_with_inline_attachment(
    patched_client: RecordingHelpDesk,
) -> None:
    patched_client.route("POST", "/api/ticket/create", "77")

    payload = await call(
        build(),
        "create_ticket",
        {
            "text": "mit Anhang",
            "attachments": [
                {
                    "name": "log.txt",
                    "content_base64": base64.b64encode(b"stack trace").decode(),
                }
            ],
        },
    )

    assert payload == {"ticketId": "77"}
    body = patched_client.last_request.content.decode("utf-8", errors="replace")
    assert 'filename="log.txt"' in body
    assert "stack trace" in body


async def test_apply_ticket_action_tool(patched_client: RecordingHelpDesk) -> None:
    patched_client.route("POST", "/api/ticket/5/apply", "890")

    payload = await call(
        build(),
        "apply_ticket_action",
        {"ticket_id": "5", "action_id": "-12", "text": "Erledigt"},
    )

    assert payload == {"ticketId": "5", "stepId": "890"}
    assert patched_client.last_json() == {
        "actionId": "-12",
        "text": "Erledigt",
        "htmlContent": False,
    }


async def test_server_info_reports_ok(patched_client: RecordingHelpDesk) -> None:
    patched_client.route("POST", "/api/ticket/search", {"ticketList": [], "hasMore": False})

    payload = await call(build(), "server_info")

    assert payload["connection"] == "ok"
    assert payload["base_url"] == BASE_URL
    assert payload["read_only"] is False


async def test_server_info_reports_the_ca_bundle(
    patched_client: RecordingHelpDesk, tmp_path
) -> None:
    patched_client.route("POST", "/api/ticket/search", {"ticketList": [], "hasMore": False})
    bundle = tmp_path / "company-ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n")

    payload = await call(build(ca_bundle=str(bundle)), "server_info")

    assert payload["tls"] == f"CA bundle {bundle}"


async def test_tools_use_the_configured_ca_bundle(
    monkeypatch: pytest.MonkeyPatch, helpdesk: RecordingHelpDesk, tmp_path
) -> None:
    """The CA bundle has to reach the HTTP client, not just the settings."""
    bundle = tmp_path / "company-ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n")
    verify: list[object] = []

    def factory(config, **kwargs):
        verify.append(kwargs.get("verify"))
        return helpdesk.client(config)

    monkeypatch.setattr("inet_helpdesk_mcp.server.HelpdeskClient", factory)
    helpdesk.route("GET", "/api/ticket/1", {"ticketId": 1})

    await call(build(ca_bundle=str(bundle)), "get_ticket", {"ticket_id": "1"})

    assert verify == [str(bundle)]


async def test_tools_keep_the_default_store_without_a_ca_bundle(
    monkeypatch: pytest.MonkeyPatch, helpdesk: RecordingHelpDesk
) -> None:
    verify: list[object] = []

    def factory(config, **kwargs):
        verify.append(kwargs.get("verify"))
        return helpdesk.client(config)

    monkeypatch.setattr("inet_helpdesk_mcp.server.HelpdeskClient", factory)
    helpdesk.route("GET", "/api/ticket/1", {"ticketId": 1})

    await call(build(), "get_ticket", {"ticket_id": "1"})

    assert verify == [True]


async def test_server_info_reports_missing_credentials() -> None:
    server = build_server(Settings(base_url=BASE_URL).finalize())

    result = await server.call_tool("server_info", {})

    assert not result.is_error
    assert result.structured_content["connection"] == "not configured"


async def test_api_error_surfaces_with_a_helpful_message(
    patched_client: RecordingHelpDesk,
) -> None:
    # The tool manager raises; the protocol layer turns that into a
    # CallToolResult(is_error=True) whose text is exactly this message.
    patched_client.route("GET", "/api/ticket/1", None, status_code=401)

    with pytest.raises(ToolError) as excinfo:
        await build().call_tool("get_ticket", {"ticket_id": "1"})

    message = str(excinfo.value)
    assert "401" in message
    assert "Web API" in message


async def test_configuration_error_surfaces_with_a_helpful_message() -> None:
    server = build_server(Settings(base_url=BASE_URL).finalize())

    with pytest.raises(ToolError, match="No credentials available"):
        await server.call_tool("get_ticket", {"ticket_id": "1"})
