from __future__ import annotations

import pathlib

import pytest

from inet_helpdesk_mcp.config import (
    CA_BUNDLE_ENV_VARS,
    Settings,
    normalize_base_url,
    resolve_request_config,
)
from inet_helpdesk_mcp.errors import AuthenticationError, ConfigurationError


@pytest.fixture
def ca_bundle(tmp_path: pathlib.Path) -> str:
    bundle = tmp_path / "company-ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\nnot a real certificate\n")
    return str(bundle)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://hd.example.com:9000/", "https://hd.example.com:9000"),
        ("hd.example.com:9000", "http://hd.example.com:9000"),
        ("https://hd.example.com/api", "https://hd.example.com"),
    ],
)
def test_normalize_base_url(raw: str, expected: str) -> None:
    assert normalize_base_url(raw) == expected


def test_normalize_base_url_rejects_other_schemes() -> None:
    with pytest.raises(ConfigurationError):
        normalize_base_url("ftp://hd.example.com")


def test_env_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INET_BASE_URL", "https://hd.example.com:9000/")
    monkeypatch.setenv("INET_TOKEN", "abc")
    monkeypatch.setenv("INET_READ_ONLY", "yes")
    monkeypatch.setenv("INET_PORT", "9999")

    settings = Settings.from_env()

    assert settings.base_url == "https://hd.example.com:9000"
    assert settings.token == "abc"
    assert settings.read_only is True
    assert settings.port == 9999
    # A configured base URL disables the override header unless asked for.
    assert settings.allow_url_header is False


def test_env_settings_reject_bad_boolean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INET_READ_ONLY", "maybe")
    with pytest.raises(ConfigurationError):
        Settings.from_env()


def test_http_transport_disables_local_files() -> None:
    settings = Settings(base_url="https://hd", token="t", transport="http").finalize()
    assert settings.allow_local_files is False


def test_url_header_allowed_when_no_base_url() -> None:
    settings = Settings(transport="http").finalize()
    assert settings.allow_url_header is True


def test_request_header_beats_configured_token() -> None:
    settings = Settings(base_url="https://hd", token="configured").finalize()
    config = resolve_request_config(settings, {"Authorization": "Bearer from-client"})
    assert config.authorization == "Bearer from-client"


def test_configured_token_used_without_header() -> None:
    settings = Settings(base_url="https://hd", token="configured").finalize()
    assert resolve_request_config(settings).authorization == "Bearer configured"


def test_basic_auth_from_settings() -> None:
    settings = Settings(base_url="https://hd", username="u", password="p").finalize()
    config = resolve_request_config(settings)
    assert config.authorization is None
    assert (config.username, config.password) == ("u", "p")


def test_base_url_header_used_when_allowed() -> None:
    settings = Settings(transport="http").finalize()
    config = resolve_request_config(
        settings, {"authorization": "Bearer t", "X-Inet-Base-Url": "hd.example.com:9000/"}
    )
    assert config.base_url == "http://hd.example.com:9000"


def test_base_url_header_rejected_when_not_allowed() -> None:
    settings = Settings(base_url="https://hd", token="t", transport="http").finalize()
    with pytest.raises(ConfigurationError):
        resolve_request_config(settings, {"X-Inet-Base-Url": "https://evil.example.com"})


def test_missing_base_url_is_reported() -> None:
    settings = Settings(transport="http").finalize()
    with pytest.raises(ConfigurationError):
        resolve_request_config(settings, {"authorization": "Bearer t"})


def test_missing_credentials_are_reported() -> None:
    settings = Settings(base_url="https://hd").finalize()
    with pytest.raises(AuthenticationError):
        resolve_request_config(settings, {})


def test_tls_verify_defaults_to_the_bundled_store() -> None:
    settings = Settings(base_url="https://hd", token="t").finalize()

    assert settings.tls_verify() is True


def test_tls_verify_uses_configured_ca_bundle(ca_bundle: str) -> None:
    settings = Settings(base_url="https://hd", token="t", ca_bundle=ca_bundle).finalize()

    assert settings.tls_verify() == ca_bundle
    assert ca_bundle in settings.describe()


@pytest.mark.parametrize("variable", CA_BUNDLE_ENV_VARS)
def test_tls_verify_honours_standard_env_variables(
    monkeypatch: pytest.MonkeyPatch, ca_bundle: str, variable: str
) -> None:
    monkeypatch.setenv(variable, ca_bundle)
    settings = Settings(base_url="https://hd", token="t").finalize()

    assert settings.tls_verify() == ca_bundle


def test_configured_ca_bundle_beats_env_variable(
    monkeypatch: pytest.MonkeyPatch, ca_bundle: str, tmp_path: pathlib.Path
) -> None:
    from_env = tmp_path / "from-env.pem"
    from_env.write_text("-----BEGIN CERTIFICATE-----\n")
    monkeypatch.setenv("SSL_CERT_FILE", str(from_env))

    settings = Settings(base_url="https://hd", token="t", ca_bundle=ca_bundle).finalize()

    assert settings.tls_verify() == ca_bundle


def test_unusable_env_bundle_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "gone.pem"))
    settings = Settings(base_url="https://hd", token="t").finalize()

    assert settings.tls_verify() is True


def test_no_verify_tls_wins_over_env_bundle(
    monkeypatch: pytest.MonkeyPatch, ca_bundle: str
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", ca_bundle)
    settings = Settings(base_url="https://hd", token="t", verify_tls=False).finalize()

    assert settings.tls_verify() is False


def test_ca_bundle_and_no_verify_tls_are_rejected(ca_bundle: str) -> None:
    with pytest.raises(ConfigurationError, match="contradict"):
        Settings(
            base_url="https://hd", token="t", ca_bundle=ca_bundle, verify_tls=False
        ).finalize()


def test_missing_ca_bundle_is_rejected(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ConfigurationError, match="not a readable file"):
        Settings(base_url="https://hd", token="t", ca_bundle=str(tmp_path / "gone.pem")).finalize()


def test_ca_bundle_from_env(monkeypatch: pytest.MonkeyPatch, ca_bundle: str) -> None:
    monkeypatch.setenv("INET_BASE_URL", "https://hd")
    monkeypatch.setenv("INET_TOKEN", "t")
    monkeypatch.setenv("INET_CA_BUNDLE", ca_bundle)

    assert Settings.from_env().ca_bundle == ca_bundle


def test_describe_hides_secrets() -> None:
    settings = Settings(base_url="https://hd", token="super-secret").finalize()
    assert "super-secret" not in settings.describe()


def test_ignore_client_auth_keeps_configured_token() -> None:
    settings = Settings(
        base_url="https://hd", token="service-account", ignore_client_auth=True
    ).finalize()

    config = resolve_request_config(settings, {"Authorization": "Bearer from-client"})

    assert config.authorization == "Bearer service-account"


def test_ignore_client_auth_keeps_configured_basic_auth() -> None:
    settings = Settings(
        base_url="https://hd", username="svc", password="pw", ignore_client_auth=True
    ).finalize()

    config = resolve_request_config(settings, {"Authorization": "Bearer from-client"})

    assert config.authorization is None
    assert (config.username, config.password) == ("svc", "pw")


def test_ignore_client_auth_without_credentials_is_reported() -> None:
    settings = Settings(base_url="https://hd", ignore_client_auth=True).finalize()

    with pytest.raises(AuthenticationError):
        resolve_request_config(settings, {"Authorization": "Bearer from-client"})


def test_ignore_client_auth_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INET_BASE_URL", "https://hd")
    monkeypatch.setenv("INET_TOKEN", "t")
    monkeypatch.setenv("INET_IGNORE_CLIENT_AUTH", "true")

    assert Settings.from_env().ignore_client_auth is True


def test_describe_mentions_ignored_client_headers() -> None:
    settings = Settings(base_url="https://hd", token="t", ignore_client_auth=True).finalize()

    assert "client Authorization headers ignored" in settings.describe()


def test_credential_source_is_reported() -> None:
    from_header = resolve_request_config(
        Settings(base_url="https://hd", token="t").finalize(),
        {"Authorization": "Bearer client"},
    )
    from_config = resolve_request_config(Settings(base_url="https://hd", token="t").finalize())
    from_basic = resolve_request_config(
        Settings(base_url="https://hd", username="svc", password="pw").finalize()
    )

    assert from_header.source == "Authorization header of this request"
    assert from_config.source == "bearer token from the server configuration"
    assert "svc" in from_basic.source
    # The source string is for diagnostics and must never leak the secret.
    assert "t" != from_config.source and "pw" not in from_basic.source
