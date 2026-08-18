from __future__ import annotations

import pathlib

import pytest

from inet_helpdesk_mcp.cli import build_parser, main, settings_from_args
from inet_helpdesk_mcp.errors import ConfigurationError


def parse(argv: list[str]):
    return settings_from_args(build_parser().parse_args(argv))


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(__import__("os").environ):
        if name.startswith("INET_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def ca_bundle(tmp_path: pathlib.Path) -> str:
    bundle = tmp_path / "company-ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\nnot a real certificate\n")
    return str(bundle)


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


def test_ca_bundle_is_taken_from_the_command_line(ca_bundle: str) -> None:
    settings = parse(["--base-url", "https://hd", "--ca-bundle", ca_bundle])

    assert settings.ca_bundle == ca_bundle
    assert settings.tls_verify() == ca_bundle


def test_ca_bundle_on_command_line_beats_environment(
    monkeypatch: pytest.MonkeyPatch, ca_bundle: str, tmp_path: pathlib.Path
) -> None:
    from_env = tmp_path / "from-env.pem"
    from_env.write_text("-----BEGIN CERTIFICATE-----\n")
    monkeypatch.setenv("INET_CA_BUNDLE", str(from_env))

    settings = parse(["--base-url", "https://hd", "--ca-bundle", ca_bundle])

    assert settings.ca_bundle == ca_bundle


def test_ca_bundle_conflicts_with_no_verify_tls(ca_bundle: str) -> None:
    with pytest.raises(ConfigurationError, match="contradict"):
        parse(["--base-url", "https://hd", "--ca-bundle", ca_bundle, "--no-verify-tls"])


def test_conflicting_tls_options_exit_with_an_error(
    ca_bundle: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        ["--base-url", "https://hd", "--token", "t", "--ca-bundle", ca_bundle, "--no-verify-tls"]
    )

    assert code == 2
    assert "contradict" in capsys.readouterr().err


def test_without_ca_bundle_the_default_store_is_kept() -> None:
    settings = parse(["--base-url", "https://hd"])

    assert settings.ca_bundle is None
    assert settings.tls_verify() is True


def test_unknown_transport_is_rejected() -> None:
    with pytest.raises(SystemExit):
        parse(["--transport", "carrier-pigeon"])


def test_ignore_client_auth_flag() -> None:
    settings = parse(
        ["--transport", "http", "--base-url", "https://hd", "--token", "t",
         "--ignore-client-auth"]
    )

    assert settings.ignore_client_auth is True


def test_client_auth_is_forwarded_by_default() -> None:
    settings = parse(["--transport", "http", "--base-url", "https://hd"])

    assert settings.ignore_client_auth is False
