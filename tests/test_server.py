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
        "get_ticket_conversation",
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


# -- conversation ---------------------------------------------------------


def _route_conversation(helpdesk: RecordingHelpDesk) -> None:
    helpdesk.route("GET", "/api/ticket/1", {"ticketId": 1, "fields": {"subject": "Drucker"}})
    helpdesk.route(
        "GET",
        "/api/ticket/1/steps",
        [
            {"id": 1, "actionID": 4, "lastModified": 1601011754753},
            {"id": 2, "actionID": 5, "lastModified": 1601011754999, "secondaryTicketId": 456},
        ],
    )
    helpdesk.route("GET", "/api/ticket/1/steps/1", {"id": 1, "text": "Anfrage"})
    helpdesk.route("GET", "/api/ticket/456/steps/2", {"id": 2, "text": "Antwort"})


async def test_conversation_reads_ticket_and_steps_in_one_call(
    patched_client: RecordingHelpDesk,
) -> None:
    _route_conversation(patched_client)

    payload = await call(build(), "get_ticket_conversation", {"ticket_id": "1"})

    assert payload["ticket"]["fields"]["subject"] == "Drucker"
    assert [step["text"] for step in payload["steps"]] == ["Anfrage", "Antwort"]
    assert payload["stepCount"] == 2
    assert payload["truncated"] is False
    assert payload["steps"][0]["lastModified_iso"] == "2020-09-25T05:29:14Z"


async def test_conversation_reads_a_merged_step_from_its_own_ticket(
    patched_client: RecordingHelpDesk,
) -> None:
    """A step with secondaryTicketId belongs to that ticket - the primary answers 404."""
    _route_conversation(patched_client)

    await call(build(), "get_ticket_conversation", {"ticket_id": "1"})

    paths = [request.url.path for request in patched_client.requests]
    assert "/api/ticket/456/steps/2" in paths
    assert "/api/ticket/1/steps/2" not in paths


async def test_conversation_keeps_the_newest_steps(patched_client: RecordingHelpDesk) -> None:
    _route_conversation(patched_client)

    payload = await call(build(), "get_ticket_conversation", {"ticket_id": "1", "limit": 1})

    assert [step["id"] for step in payload["steps"]] == [2]
    assert payload["truncated"] is True
    assert payload["stepCount"] == 2


async def test_conversation_without_texts_asks_for_no_details(
    patched_client: RecordingHelpDesk,
) -> None:
    _route_conversation(patched_client)

    payload = await call(
        build(), "get_ticket_conversation", {"ticket_id": "1", "include_text": False}
    )

    assert "text" not in payload["steps"][0]
    assert len(patched_client.requests) == 2  # ticket and step list, nothing else


async def test_conversation_converts_html_and_truncates(
    patched_client: RecordingHelpDesk,
) -> None:
    patched_client.route("GET", "/api/ticket/1", {"ticketId": 1})
    patched_client.route("GET", "/api/ticket/1/steps", [{"id": 1}])
    patched_client.route(
        "GET",
        "/api/ticket/1/steps/1",
        {"id": 1, "htmlContent": True, "text": "<p>Hallo <b>Welt</b></p>"},
    )

    payload = await call(
        build(), "get_ticket_conversation", {"ticket_id": "1", "max_text_chars": 5}
    )

    step = payload["steps"][0]
    assert step["text"].startswith("Hallo")
    assert "truncated, 5 of 10 characters" in step["text"]
    assert step["htmlContent"] is False
    assert step["textWasHtml"] is True


async def test_conversation_survives_an_unreadable_step(
    patched_client: RecordingHelpDesk,
) -> None:
    patched_client.route("GET", "/api/ticket/1", {"ticketId": 1})
    patched_client.route("GET", "/api/ticket/1/steps", [{"id": 1}])
    patched_client.route("GET", "/api/ticket/1/steps/1", None, status_code=403)

    payload = await call(build(), "get_ticket_conversation", {"ticket_id": "1"})

    assert "403" in payload["steps"][0]["error"]


# -- normalisation --------------------------------------------------------


async def test_get_ticket_merges_display_values(patched_client: RecordingHelpDesk) -> None:
    patched_client.route(
        "GET",
        "/api/ticket/1",
        {
            "ticketId": 1,
            "attributes": {"statusid": 400, "statusid_display": "Geschlossen"},
        },
    )

    payload = await call(build(), "get_ticket", {"ticket_id": "1"})

    assert payload["attributes"]["statusid"] == {"value": 400, "display": "Geschlossen"}


async def test_raw_returns_the_web_api_answer_unchanged(
    patched_client: RecordingHelpDesk,
) -> None:
    record = {"ticketId": 1, "attributes": {"statusid": 400, "statusid_display": "Zu"}}
    patched_client.route("GET", "/api/ticket/1", record)

    payload = await call(build(), "get_ticket", {"ticket_id": "1", "raw": True})

    assert payload == record


async def test_no_normalize_switches_the_reshaping_off(
    patched_client: RecordingHelpDesk,
) -> None:
    record = {"ticketId": 1, "attributes": {"statusid": 400, "statusid_display": "Zu"}}
    patched_client.route("GET", "/api/ticket/1", record)

    payload = await call(build(normalize=False), "get_ticket", {"ticket_id": "1"})

    assert payload == record


async def test_get_ticket_step_converts_html(patched_client: RecordingHelpDesk) -> None:
    patched_client.route(
        "GET", "/api/ticket/1/steps/1", {"id": 1, "htmlContent": True, "text": "<p>Hi</p>"}
    )

    payload = await call(build(), "get_ticket_step", {"ticket_id": "1", "step_id": "1"})

    assert payload["text"] == "Hi"
    assert payload["textWasHtml"] is True


# -- search with details --------------------------------------------------


async def test_search_can_resolve_its_hits(patched_client: RecordingHelpDesk) -> None:
    patched_client.route("POST", "/api/ticket/search", {"ticketList": [1, 2], "hasMore": False})
    patched_client.route("GET", "/api/ticket/1", {"ticketId": 1, "fields": {"subject": "A"}})
    patched_client.route("GET", "/api/ticket/2", {"ticketId": 2, "fields": {"subject": "B"}})

    payload = await call(
        build(), "search_tickets", {"query": "printer", "include_details": True}
    )

    assert [entry["fields"]["subject"] for entry in payload["tickets"]] == ["A", "B"]
    assert patched_client.requests[-1].url.params["fields"].startswith("subject,")


async def test_search_skips_details_for_too_many_hits(
    patched_client: RecordingHelpDesk,
) -> None:
    patched_client.route(
        "POST", "/api/ticket/search", {"ticketList": list(range(51)), "hasMore": False}
    )

    payload = await call(
        build(), "search_tickets", {"query": "", "include_details": True}
    )

    assert "tickets" not in payload
    assert "51 hits" in payload["detailsOmitted"]
    assert len(patched_client.requests) == 1


# -- policy, dry run, annotations -----------------------------------------


async def test_list_ticket_actions_hides_what_the_policy_forbids(
    patched_client: RecordingHelpDesk,
) -> None:
    patched_client.route(
        "GET", "/api/ticket/1/actions", {"-9": "E-Mail empfangen", "-2": "Reaktivieren"}
    )

    payload = await call(
        build(denied_actions=("-2",)), "list_ticket_actions", {"ticket_id": "1"}
    )

    assert payload["actions"] == {"-9": "E-Mail empfangen"}
    assert payload["hiddenByPolicy"] == 1


async def test_a_forbidden_action_never_reaches_the_helpdesk(
    patched_client: RecordingHelpDesk,
) -> None:
    patched_client.route("POST", "/api/ticket/5/apply", "890")

    with pytest.raises(ToolError, match="not allowed"):
        await build(allowed_actions=("-9",)).call_tool(
            "apply_ticket_action", {"ticket_id": "5", "action_id": "-12"}
        )

    assert patched_client.requests == []


async def test_the_automail_default_is_added_but_never_overwritten(
    patched_client: RecordingHelpDesk,
) -> None:
    patched_client.route("POST", "/api/ticket/create", "1")
    server = build(default_automail="NEVER")

    await call(server, "create_ticket", {"text": "ohne Mail"})
    assert patched_client.last_json()["actionArguments"] == {
        "ticketextension.automail": "NEVER"
    }

    await call(
        server,
        "create_ticket",
        {"text": "mit Mail", "action_arguments": {"ticketextension.automail": "ALWAYS"}},
    )
    assert patched_client.last_json()["actionArguments"] == {
        "ticketextension.automail": "ALWAYS"
    }


async def test_dry_run_reports_the_request_without_sending_it(
    patched_client: RecordingHelpDesk,
) -> None:
    payload = await call(
        build(dry_run=True),
        "apply_ticket_action",
        {"ticket_id": "5", "action_id": "-12", "text": "Erledigt"},
    )

    assert payload["dryRun"] is True
    assert payload["path"] == "/api/ticket/5/apply"
    assert payload["payload"]["actionId"] == "-12"
    assert patched_client.requests == []


async def test_dry_run_still_validates_the_action_policy() -> None:
    with pytest.raises(ToolError, match="not allowed"):
        await build(dry_run=True, denied_actions=("-12",)).call_tool(
            "apply_ticket_action", {"ticket_id": "5", "action_id": "-12"}
        )


async def test_tools_declare_whether_they_change_anything() -> None:
    tools = {tool.name: tool.annotations for tool in await build().list_tools()}

    assert tools["get_ticket"].read_only_hint is True
    assert tools["get_ticket_conversation"].read_only_hint is True
    assert tools["create_ticket"].read_only_hint is False
    assert tools["create_ticket"].destructive_hint is False
    assert tools["apply_ticket_action"].destructive_hint is True


async def test_server_info_reports_the_new_guard_rails(
    patched_client: RecordingHelpDesk,
) -> None:
    patched_client.route("POST", "/api/ticket/search", {"ticketList": [], "hasMore": False})

    payload = await call(build(dry_run=True, denied_actions=("-2",)), "server_info")

    assert payload["dry_run"] is True
    assert payload["denied_actions"] == ["-2"]
    assert payload["retries"] == 2


async def test_the_same_user_keeps_one_connection(
    monkeypatch: pytest.MonkeyPatch, helpdesk: RecordingHelpDesk
) -> None:
    """One client per user, not one per tool call - basic auth would open a
    HelpDesk session every single time."""
    created = []

    def factory(config, **_kwargs):
        client = helpdesk.client(config)
        created.append(client)
        return client

    monkeypatch.setattr("inet_helpdesk_mcp.server.HelpdeskClient", factory)
    helpdesk.route("GET", "/api/ticket/1", {"ticketId": 1})
    server = build()

    await call(server, "get_ticket", {"ticket_id": "1"})
    await call(server, "get_ticket", {"ticket_id": "1"})

    assert len(created) == 1
    assert len(helpdesk.requests) == 2


async def test_shutdown_closes_the_pooled_clients(
    monkeypatch: pytest.MonkeyPatch, helpdesk: RecordingHelpDesk
) -> None:
    created = []

    def factory(config, **_kwargs):
        client = helpdesk.client(config)
        created.append(client)
        return client

    monkeypatch.setattr("inet_helpdesk_mcp.server.HelpdeskClient", factory)
    helpdesk.route("GET", "/api/ticket/1", {"ticketId": 1})
    server = build()
    await call(server, "get_ticket", {"ticket_id": "1"})

    lowlevel = server._lowlevel_server
    async with lowlevel.lifespan(lowlevel):
        pass

    assert created[0]._client.is_closed
