"""The per-action tools: right action id, right payload, availability check."""

from __future__ import annotations

import base64

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from inet_helpdesk_mcp.actions import (
    ACTION_SPECS,
    parse_action_id_overrides,
    resolve_action_ids,
    to_millis,
)
from inet_helpdesk_mcp.config import Settings
from inet_helpdesk_mcp.errors import ConfigurationError, HelpdeskError
from inet_helpdesk_mcp.server import build_server

from conftest import BASE_URL, RecordingHelpDesk

#: Every action of the reference ticket 323943, so the availability check passes.
ALL_ACTIONS = {spec.action_id: spec.label for spec in ACTION_SPECS}


@pytest.fixture
def patched_client(monkeypatch: pytest.MonkeyPatch, helpdesk: RecordingHelpDesk):
    def factory(config, **_kwargs):
        return helpdesk.client(config)

    monkeypatch.setattr("inet_helpdesk_mcp.server.HelpdeskClient", factory)
    helpdesk.route("GET", "/api/ticket/7/actions", ALL_ACTIONS)
    helpdesk.route("POST", "/api/ticket/7/apply", "555")
    return helpdesk


def build(**overrides):
    settings = Settings(base_url=BASE_URL, token="test-token", **overrides).finalize()
    return build_server(settings)


async def call(server, name: str, arguments: dict):
    result = await server.call_tool(name, arguments)
    assert not result.is_error, result.content
    return result.structured_content


async def test_every_catalogue_entry_becomes_a_tool() -> None:
    names = {tool.name for tool in await build().list_tools()}

    assert {spec.name for spec in ACTION_SPECS} <= names


async def test_tool_names_are_unique() -> None:
    assert len({spec.name for spec in ACTION_SPECS}) == len(ACTION_SPECS)


@pytest.mark.parametrize(
    ("tool", "arguments", "fields"),
    [
        ("change_priority", {"priority_id": "80"}, {"priorityid": "80"}),
        ("change_category", {"category_id": "12"}, {"categoryid": "12"}),
        ("change_classification", {"classification_id": "3"}, {"classificationid": "3"}),
        ("change_subject", {"subject": "Neu"}, {"subject": "Neu"}),
        ("change_ticket_owner", {"owner_guid": "u1"}, {"ownerid": "u1"}),
        ("change_itil", {"itil_id": "2"}, {"itilid": "2"}),
        ("change_resource", {"resource_guid": "r1"}, {"resourceid": "r1"}),
        ("change_deadline", {"deadline": "2026-10-31T16:00:00Z"}, {"deadline": "1793462400000"}),
        ("change_ticket_fields", {"fields": {"custom1": "x"}}, {"custom1": "x"}),
    ],
)
async def test_field_tools_go_through_change_field_action(
    patched_client: RecordingHelpDesk, tool: str, arguments: dict, fields: dict
) -> None:
    payload = await call(build(), tool, {"ticket_id": "7", **arguments})

    assert payload == {"ticketId": "7", "actionId": "-29", "stepId": "555"}
    assert patched_client.last_json() == {
        "actionId": "-29",
        "text": "",
        "htmlContent": False,
        "ticketFields": fields,
    }


MAIL = {"ticket_id": "7", "to": "anna@example.com", "subject": "Re: Drucker", "text": "Hallo"}


async def test_send_email_sends_recipient_subject_and_apply_answer(
    patched_client: RecordingHelpDesk,
) -> None:
    """Without reastepemailan the HelpDesk answers HTTP 500 "Die E-Mail muss eine
    Empfängeradresse haben." - verified against a live HelpDesk."""
    await call(build(), "send_email", {**MAIL, "send_mode": "SEND_AND_CLOSE"})

    assert patched_client.last_json() == {
        "actionId": "-7",
        "text": "Hallo",
        "htmlContent": False,
        "stepFields": {"reastepemailan": "anna@example.com", "reastepdesc": "Re: Drucker"},
        "actionArguments": {"ticketextension.applyanswer": "SEND_AND_CLOSE"},
    }


async def test_send_email_uses_the_configured_mailbox(patched_client: RecordingHelpDesk) -> None:
    await call(build(mail_from="support@example.com"), "send_email", MAIL)

    assert patched_client.last_json()["stepFields"]["reastepemailin"] == "support@example.com"


async def test_send_email_mailbox_of_the_call_wins(patched_client: RecordingHelpDesk) -> None:
    await call(
        build(mail_from="support@example.com"),
        "send_email",
        {**MAIL, "mail_from": "it@example.com"},
    )

    assert patched_client.last_json()["stepFields"]["reastepemailin"] == "it@example.com"


async def test_send_email_needs_a_recipient(patched_client: RecordingHelpDesk) -> None:
    arguments = {key: value for key, value in MAIL.items() if key != "to"}
    with pytest.raises(ToolError):
        await build().call_tool("send_email", arguments)
    assert patched_client.requests == []


async def test_send_email_rejects_an_unknown_send_mode(patched_client: RecordingHelpDesk) -> None:
    with pytest.raises(ToolError):
        await build().call_tool("send_email", {**MAIL, "send_mode": "LATER"})
    assert patched_client.requests == []


async def test_send_email_keeps_the_automail_default(patched_client: RecordingHelpDesk) -> None:
    await call(build(default_automail="NEVER"), "send_email", MAIL)

    assert patched_client.last_json()["actionArguments"] == {
        "ticketextension.applyanswer": "SEND_ONLY",
        "ticketextension.automail": "NEVER",
    }


async def test_affected_assets_are_stringified(patched_client: RecordingHelpDesk) -> None:
    await call(build(), "change_affected_assets", {"ticket_id": "7", "asset_guids": ["a", "b"]})

    assert patched_client.last_json()["actionArguments"] == {
        "inventory_affectedassets": '["a", "b"]'
    }


async def test_add_attachment_uploads_the_file(patched_client: RecordingHelpDesk) -> None:
    content = base64.b64encode(b"log").decode()
    await call(
        build(),
        "add_attachment",
        {"ticket_id": "7", "attachments": [{"name": "a.txt", "content_base64": content}]},
    )

    body = patched_client.last_request.content.decode()
    assert '"actionId": "-21"' in body
    assert 'filename="a.txt"' in body


@pytest.mark.parametrize(
    ("tool", "action_id"),
    [
        ("authorize_ticket", "5"),
        ("close_ticket", "2"),
        ("delete_ticket", "7"),
        ("bundle_tickets", "-35"),
        ("start_process", "-19"),
        ("live_support_ai_answer", "-126"),
    ],
)
async def test_generic_tools_apply_their_action(
    patched_client: RecordingHelpDesk, tool: str, action_id: str
) -> None:
    await call(build(), tool, {"ticket_id": "7", "action_arguments": {"k": "v"}})

    assert patched_client.last_json() == {
        "actionId": action_id,
        "text": "",
        "htmlContent": False,
        "actionArguments": {"k": "v"},
    }


async def test_unavailable_action_fails_before_applying(
    patched_client: RecordingHelpDesk,
) -> None:
    patched_client.route("GET", "/api/ticket/7/actions", {"-9": "E-Mail empfangen"})

    with pytest.raises(ToolError, match=r"-35 is not available.*-9 \(E-Mail empfangen\)"):
        await build().call_tool("bundle_tickets", {"ticket_id": "7"})

    assert all(request.method == "GET" for request in patched_client.requests)


async def test_dry_run_sends_nothing(patched_client: RecordingHelpDesk) -> None:
    payload = await call(build(dry_run=True), "close_ticket", {"ticket_id": "7"})

    assert payload["dryRun"] is True
    assert payload["payload"]["actionId"] == "2"
    assert patched_client.requests == []


async def test_action_id_override_moves_a_tool(patched_client: RecordingHelpDesk) -> None:
    patched_client.route("GET", "/api/ticket/7/actions", {"12": "Schließen"})

    server = build(action_ids=parse_action_id_overrides("close_ticket=12"))
    await call(server, "close_ticket", {"ticket_id": "7"})

    assert patched_client.last_json()["actionId"] == "12"
    tool = next(tool for tool in await server.list_tools() if tool.name == "close_ticket")
    assert "action 12" in tool.description


async def test_policy_hides_tools_of_barred_actions() -> None:
    names = {tool.name for tool in await build(denied_actions=("7", "-29")).list_tools()}

    assert "delete_ticket" not in names
    assert "change_priority" not in names
    assert "close_ticket" in names


async def test_allow_list_keeps_only_its_tools() -> None:
    names = {tool.name for tool in await build(allowed_actions=("2",)).list_tools()}

    assert names & {spec.name for spec in ACTION_SPECS} == {"close_ticket"}


def test_override_parsing_rejects_unknown_tools() -> None:
    with pytest.raises(ConfigurationError, match="Unknown action tool"):
        parse_action_id_overrides("close=2")
    with pytest.raises(ConfigurationError, match="tool=id"):
        parse_action_id_overrides("close_ticket")


def test_resolve_action_ids_applies_overrides() -> None:
    ids = resolve_action_ids((("authorize_ticket", "15"),))

    assert ids["authorize_ticket"] == "15"
    assert ids["close_ticket"] == "2"


def test_to_millis() -> None:
    assert to_millis(1733875200000) == "1733875200000"
    assert to_millis("1733875200000") == "1733875200000"
    assert to_millis("2024-12-11T00:00:00Z") == "1733875200000"
    assert to_millis("2024-12-11T01:00:00+01:00") == "1733875200000"
    assert to_millis("2024-12-11") == "1733875200000"
    with pytest.raises(HelpdeskError):
        to_millis("morgen")


# -- read tools for the gaps of the search ----------------------------------


async def test_find_unauthorized_tickets_scans_ids(patched_client: RecordingHelpDesk) -> None:
    patched_client.route(
        "GET", "/api/ticket/10", {"ticketId": 10, "attributes": {"statusid": 100}}
    )
    patched_client.route(
        "GET",
        "/api/ticket/9",
        {"ticketId": 9, "fields": {"subject": "Neu"}, "attributes": {"statusid": 0}},
    )
    # 8 is not routed: the fake answers 404, as the HelpDesk does for gaps.

    payload = await call(
        build(), "find_unauthorized_tickets", {"start_id": 10, "count": 3}
    )

    assert [ticket["ticketId"] for ticket in payload["tickets"]] == [9]
    assert payload["checked"] == 3
    assert payload["missing"] == 1
    assert payload["scannedIds"] == [10, 8]


async def test_find_unauthorized_tickets_limits_the_scan() -> None:
    with pytest.raises(ToolError, match="between 1 and 500"):
        await build().call_tool("find_unauthorized_tickets", {"start_id": 10, "count": 5000})


async def test_ticket_statistics_counts_by_display_value(
    patched_client: RecordingHelpDesk,
) -> None:
    patched_client.route("POST", "/api/ticket/search", {"ticketList": [1, 2], "hasMore": False})
    patched_client.route(
        "GET",
        "/api/ticket/1",
        {"ticketId": 1, "attributes": {"statusid": 100, "statusid_display": "Offen"}},
    )
    patched_client.route(
        "GET",
        "/api/ticket/2",
        {"ticketId": 2, "attributes": {"statusid": 100, "statusid_display": "Offen"}},
    )

    payload = await call(build(), "ticket_statistics", {"query": "", "group_by": "statusid"})

    assert payload["counts"] == {"statusid": {"Offen (100)": 2}}
    assert payload["counted"] == 2
    assert patched_client.last_request.url.params["fields"] == "statusid"
