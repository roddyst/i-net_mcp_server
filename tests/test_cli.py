from __future__ import annotations

import pytest

from inet_helpdesk_mcp.cli import build_parser, settings_from_args


def parse(argv: list[str]):
    return settings_from_args(build_parser().parse_args(argv))


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(__import__("os").environ):
        if name.startswith("INET_"):
            monkeypatch.delenv(name, raising=False)


def test_command_line_beats_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INET_BASE_URL", "https://from-env")
    monkeypatch.setenv("INET_TOKEN", "env-token")

    settings = parse(["--base-url", "https://from-cli:9000"])

    assert settings.base_url == "https://from-cli:9000"
    assert settings.token == "env-token"


def test_base_url_on_command_line_disables_url_header() -> None:
    """Regression: the derived flag must not stay enabled from the empty env."""
    settings = parse(["--transport", "http", "--base-url", "https://hd.example.com"])

    assert settings.allow_url_header is False


def test_url_header_can_be_enabled_explicitly() -> None:
    settings = parse(
        ["--transport", "http", "--base-url", "https://hd", "--allow-url-header"]
    )

    assert settings.allow_url_header is True


def test_http_transport_disables_local_files() -> None:
    settings = parse(["--transport", "http", "--base-url", "https://hd"])

    assert settings.allow_local_files is False


def test_stdio_keeps_local_files_unless_switched_off() -> None:
    assert parse(["--base-url", "https://hd"]).allow_local_files is True
    assert parse(["--base-url", "https://hd", "--no-local-files"]).allow_local_files is False


def test_read_only_and_tls_switches() -> None:
    settings = parse(["--base-url", "https://hd", "--read-only", "--no-verify-tls"])

    assert settings.read_only is True
    assert settings.verify_tls is False


def test_unknown_transport_is_rejected() -> None:
    with pytest.raises(SystemExit):
        parse(["--transport", "carrier-pigeon"])
