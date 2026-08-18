from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest

from inet_helpdesk_mcp.client import HelpdeskClient
from inet_helpdesk_mcp.config import CA_BUNDLE_ENV_VARS, RequestConfig, Settings

BASE_URL = "https://helpdesk.example.com:9000"


@pytest.fixture(autouse=True)
def clean_tls_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The machine running the tests may point at its own CA bundle."""
    for name in CA_BUNDLE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class RecordingHelpDesk:
    """A fake i-net HelpDesk built on httpx.MockTransport."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]] = {}

    def route(self, method: str, path: str, payload: Any, status_code: int = 200) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if isinstance(payload, (dict, list, int, float, bool)) or payload is None:
                return httpx.Response(status_code, json=payload)
            return httpx.Response(
                status_code, content=json.dumps(payload), headers={"content-type": "application/json"}
            )

        self.routes[(method.upper(), path)] = handler

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(404, text=f"no route for {request.method} {request.url.path}")
        return route(request)

    @property
    def last_request(self) -> httpx.Request:
        return self.requests[-1]

    def last_json(self) -> Any:
        return json.loads(self.last_request.content)

    def client(self, config: RequestConfig | None = None) -> HelpdeskClient:
        config = config or RequestConfig(base_url=BASE_URL, authorization="Bearer test-token")
        transport = httpx.MockTransport(self.handler)
        http_client = httpx.AsyncClient(
            base_url=config.base_url,
            transport=transport,
            follow_redirects=True,
            auth=(
                (config.username, config.password)
                if config.username and config.password
                else None
            ),
            headers=config.auth_headers(),
        )
        return HelpdeskClient(config, client=http_client)


@pytest.fixture
def helpdesk() -> RecordingHelpDesk:
    return RecordingHelpDesk()


@pytest.fixture
def settings() -> Settings:
    return Settings(base_url=BASE_URL, token="test-token").finalize()
