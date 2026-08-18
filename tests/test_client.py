from __future__ import annotations

import base64
import json
import pathlib
import ssl

import httpx
import pytest

from inet_helpdesk_mcp.client import HelpdeskClient, build_ssl_verify
from inet_helpdesk_mcp.config import RequestConfig
from inet_helpdesk_mcp.errors import ApiError, TransportError

from conftest import BASE_URL, RecordingHelpDesk


@pytest.fixture
def ca_bundle(tmp_path: pathlib.Path) -> str:
    """A CA bundle with exactly one real certificate, taken from certifi."""
    import certifi

    marker = "-----END CERTIFICATE-----\n"
    first = pathlib.Path(certifi.where()).read_text()
    bundle = tmp_path / "company-ca.pem"
    bundle.write_text(first[: first.index(marker) + len(marker)])
    return str(bundle)


async def test_search_tickets_posts_json(helpdesk: RecordingHelpDesk) -> None:
    helpdesk.route("POST", "/api/ticket/search", {"ticketList": [1, 2], "hasMore": False})

    async with helpdesk.client() as client:
        result = await client.search_tickets("Resource:", limit=10, start=0, locale="en")

    assert result["ticketList"] == [1, 2]
    request = helpdesk.last_request
    assert request.headers["authorization"] == "Bearer test-token"
    assert helpdesk.last_json() == {
        "query": "Resource:",
        "limit": 10,
        "start": 0,
        "locale": "en",
    }


async def test_get_ticket_passes_fields(helpdesk: RecordingHelpDesk) -> None:
    helpdesk.route("GET", "/api/ticket/1", {"ticketId": 1, "fields": {"subject": "Hi"}})

    async with helpdesk.client() as client:
        result = await client.get_ticket("1", fields="subject,closeddate")

    assert result["ticketId"] == 1
    assert helpdesk.last_request.url.params["fields"] == "subject,closeddate"


async def test_get_ticket_omits_empty_params(helpdesk: RecordingHelpDesk) -> None:
    helpdesk.route("GET", "/api/ticket/1", {"ticketId": 1})

    async with helpdesk.client() as client:
        await client.get_ticket("1")

    assert "fields" not in helpdesk.last_request.url.params


async def test_steps_and_step_details(helpdesk: RecordingHelpDesk) -> None:
    helpdesk.route("GET", "/api/ticket/1/steps", [{"id": 1, "actionID": "4"}])
    helpdesk.route("GET", "/api/ticket/1/steps/1", {"id": 1, "text": "hello"})

    async with helpdesk.client() as client:
        steps = await client.get_ticket_steps("1", since=1234)
        step = await client.get_ticket_step("1", "1")

    assert steps[0]["id"] == 1
    assert step["text"] == "hello"


async def test_create_ticket_without_attachments(helpdesk: RecordingHelpDesk) -> None:
    helpdesk.route("POST", "/api/ticket/create", "1234")

    async with helpdesk.client() as client:
        result = await client.create_ticket({"text": "Hello World", "htmlContent": False})

    assert result == "1234"
    assert helpdesk.last_json() == {"text": "Hello World", "htmlContent": False}
    assert helpdesk.last_request.headers["content-type"].startswith("application/json")


async def test_create_ticket_with_attachments_uses_multipart(
    helpdesk: RecordingHelpDesk,
) -> None:
    helpdesk.route("POST", "/api/ticket/create", "42")
    description = {"name": "note.txt", "lastModified": 0, "attachmentType": "Attachment"}

    async with helpdesk.client() as client:
        result = await client.create_ticket(
            {"text": "with file"}, attachments=[(description, b"hello bytes")]
        )

    assert result == "42"
    request = helpdesk.last_request
    assert request.headers["content-type"].startswith("multipart/form-data")

    body = request.content.decode("utf-8", errors="replace")
    assert 'name="json"' in body
    assert 'filename="json.txt"' in body
    assert 'name="attachment0"' in body
    assert 'filename="note.txt"' in body
    assert "hello bytes" in body

    # The JSON part carries the attachment descriptions in list order.
    start = body.index("{", body.index('name="json"'))
    payload = json.loads(body[start : body.index("}\r\n", start) + 1])
    assert payload["text"] == "with file"
    assert payload["attachments"] == [description]


async def test_apply_action_path_and_payload(helpdesk: RecordingHelpDesk) -> None:
    helpdesk.route("POST", "/api/ticket/7/apply", "99")

    async with helpdesk.client() as client:
        result = await client.apply_action("7", {"actionId": "-12", "text": "done"})

    assert result == "99"
    assert helpdesk.last_request.url.path == "/api/ticket/7/apply"
    assert helpdesk.last_json()["actionId"] == "-12"


async def test_api_error_carries_status_and_hint(helpdesk: RecordingHelpDesk) -> None:
    helpdesk.route("GET", "/api/ticket/1", None, status_code=403)

    with pytest.raises(ApiError) as excinfo:
        async with helpdesk.client() as client:
            await client.get_ticket("1")

    assert excinfo.value.status_code == 403
    assert "Web API" in str(excinfo.value)


async def test_unknown_ticket_reports_404(helpdesk: RecordingHelpDesk) -> None:
    with pytest.raises(ApiError) as excinfo:
        async with helpdesk.client() as client:
            await client.get_ticket("does-not-exist")

    assert excinfo.value.status_code == 404


async def test_transport_error_is_wrapped() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    config = RequestConfig(base_url="https://hd.example.com", authorization="Bearer t")
    http_client = httpx.AsyncClient(
        base_url=config.base_url, transport=httpx.MockTransport(boom)
    )

    with pytest.raises(TransportError) as excinfo:
        async with HelpdeskClient(config, client=http_client) as client:
            await client.get_ticket("1")

    assert "hd.example.com" in str(excinfo.value)


async def test_basic_auth_is_sent(helpdesk: RecordingHelpDesk) -> None:
    helpdesk.route("GET", "/api/ticket/1", {"ticketId": 1})
    config = RequestConfig(base_url="https://hd.example.com", username="joe", password="pw")

    async with helpdesk.client(config) as client:
        await client.get_ticket("1")

    header = helpdesk.last_request.headers["authorization"]
    assert header.startswith("Basic ")
    assert base64.b64decode(header.split(" ", 1)[1]).decode() == "joe:pw"


def test_ca_bundle_becomes_the_only_trusted_store(ca_bundle: str) -> None:
    context = build_ssl_verify(ca_bundle)

    assert isinstance(context, ssl.SSLContext)
    # Exactly the one certificate from the bundle, not the certifi default.
    assert len(context.get_ca_certs()) == 1


def test_ssl_verify_passes_booleans_through() -> None:
    assert build_ssl_verify(True) is True
    assert build_ssl_verify(False) is False


async def test_ca_bundle_is_handed_to_httpx(
    monkeypatch: pytest.MonkeyPatch, ca_bundle: str
) -> None:
    recorded: dict[str, object] = {}
    real_client = httpx.AsyncClient

    def record(**kwargs: object) -> httpx.AsyncClient:
        recorded.update(kwargs)
        return real_client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))

    monkeypatch.setattr(httpx, "AsyncClient", record)
    config = RequestConfig(base_url=BASE_URL, authorization="Bearer t")

    async with HelpdeskClient(config, verify=ca_bundle):
        pass

    context = recorded["verify"]
    assert isinstance(context, ssl.SSLContext)
    assert len(context.get_ca_certs()) == 1
