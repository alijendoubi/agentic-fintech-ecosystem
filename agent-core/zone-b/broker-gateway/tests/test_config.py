"""GatewayConfig / AlpacaForwarder: fail-closed configuration and endpoint pinning."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from execution_motor.errors import ConfigError

from broker_gateway.alpaca import (
    LIVE_BASE_URL,
    LIVE_CONFIRM_ENV,
    LIVE_CONFIRM_VALUE,
    LIVE_OPT_IN_ENV,
    PAPER_BASE_URL,
    AlpacaCredentials,
    AlpacaForwarder,
    BrokerTransportError,
    resolve_base_url,
)
from broker_gateway.config import GatewayConfig

from .helpers import API_KEY, CREDS, SECRET

BASE: dict[str, str] = {
    "GATEWAY_TLS_CERT": "/tls/server.pem",
    "GATEWAY_TLS_KEY": "/tls/server.key",
    "GATEWAY_TLS_CLIENT_CA": "/tls/ca.pem",
    "GATEWAY_ALLOWED_CLIENT_CNS": "execution-motor",
    "GATEWAY_ATTESTATION_KEYS_FILE": "/cfg/attestation-keys.json",
    "ALPACA_API_KEY": API_KEY,
    "ALPACA_SECRET_KEY": SECRET,
}


def env(**overrides: Any) -> dict[str, str]:
    out = dict(BASE)
    for key, value in overrides.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = value
    return out


def test_minimal_env_is_production_paper_with_spec_defaults() -> None:
    cfg = GatewayConfig.from_env(env())
    assert cfg.is_production
    assert cfg.base_url == PAPER_BASE_URL
    assert cfg.listen_addr == "0.0.0.0:50071"
    assert cfg.allowed_client_cns == frozenset({"execution-motor"})
    assert cfg.max_ttl_ns == 5_000_000_000
    assert cfg.max_clock_skew_ns == 1_000_000_000


@pytest.mark.parametrize(
    "missing",
    [
        "GATEWAY_TLS_CERT",
        "GATEWAY_TLS_KEY",
        "GATEWAY_TLS_CLIENT_CA",
        "GATEWAY_ALLOWED_CLIENT_CNS",
        "GATEWAY_ATTESTATION_KEYS_FILE",
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
    ],
)
def test_every_required_setting_is_required(missing: str) -> None:
    with pytest.raises(ConfigError):
        GatewayConfig.from_env(env(**{missing: None}))
    with pytest.raises(ConfigError):
        GatewayConfig.from_env(env(**{missing: " "}))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GATEWAY_ENV", "prod"),
        ("GATEWAY_LISTEN_ADDR", "no-port"),
        ("GATEWAY_MAX_ATTESTATION_TTL_MS", "0"),
        ("GATEWAY_MAX_CLOCK_SKEW_MS", "abc"),
        ("GATEWAY_ALLOWED_CLIENT_CNS", " , "),
        ("ALPACA_API_KEY", "change-me"),
        ("ALPACA_SECRET_KEY", "your-secret-here"),
        ("ALPACA_BASE_URL", "https://evil.example"),
        ("ALPACA_BASE_URL", LIVE_BASE_URL),
    ],
)
def test_invalid_settings_refuse_to_start(name: str, value: str) -> None:
    with pytest.raises(ConfigError):
        GatewayConfig.from_env(env(**{name: value}))


def test_live_needs_both_exact_opt_ins() -> None:
    with pytest.raises(ConfigError):
        resolve_base_url(LIVE_BASE_URL, {LIVE_OPT_IN_ENV: "true"})
    with pytest.raises(ConfigError):
        resolve_base_url(LIVE_BASE_URL, {LIVE_CONFIRM_ENV: LIVE_CONFIRM_VALUE})
    both = {LIVE_OPT_IN_ENV: "true", LIVE_CONFIRM_ENV: LIVE_CONFIRM_VALUE}
    assert resolve_base_url(LIVE_BASE_URL, both) == LIVE_BASE_URL
    cfg = GatewayConfig.from_env(env(ALPACA_BASE_URL=LIVE_BASE_URL, **both))
    assert cfg.base_url == LIVE_BASE_URL


def test_forwarder_refuses_any_other_base_url() -> None:
    with pytest.raises(ConfigError):
        AlpacaForwarder(CREDS, base_url="https://evil.example")


def test_config_and_credentials_repr_are_redacted() -> None:
    cfg = GatewayConfig.from_env(env())
    for text in (repr(cfg), str(cfg), repr(cfg.credentials), str(cfg.credentials)):
        assert API_KEY not in text and SECRET not in text


def test_config_errors_never_echo_a_credential() -> None:
    with pytest.raises(ConfigError) as info:
        GatewayConfig.from_env(env(ALPACA_SECRET_KEY=f"{SECRET}-placeholder"))
    assert SECRET not in str(info.value)


def test_redirects_are_not_followed() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(307, headers={"location": "https://evil.example/v2/account"})

    reply = AlpacaForwarder(CREDS, transport=httpx.MockTransport(handler)).account()
    assert reply.status == 307
    assert [r.url.host for r in seen] == ["paper-api.alpaca.markets"]


def test_transport_error_message_is_only_the_exception_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed {request.headers['APCA-API-SECRET-KEY']}")

    forwarder = AlpacaForwarder(CREDS, transport=httpx.MockTransport(handler))
    with pytest.raises(BrokerTransportError) as info:
        forwarder.positions()
    assert str(info.value) == "ConnectError"
    assert info.value.__cause__ is None and info.value.__suppress_context__


def test_proxy_environment_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.example:3128")
    forwarder = AlpacaForwarder(AlpacaCredentials(api_key=API_KEY, secret_key=SECRET))
    assert forwarder._client.trust_env is False  # noqa: SLF001
