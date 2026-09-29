"""Paper-only enforcement: the structural guarantee that no live order can be sent by accident.

Since ADR-004 the motor's client holds no credentials and talks only through an injected
transport (the broker gateway); credential handling and its tests live in broker-gateway.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

import execution_motor.alpaca as alpaca_module
from execution_motor.alpaca import (
    LIVE_BASE_URL,
    LIVE_CONFIRM_ENV,
    LIVE_CONFIRM_VALUE,
    LIVE_OPT_IN_ENV,
    PAPER_BASE_URL,
    AlpacaBroker,
    AlpacaPaperBroker,
    resolve_base_url,
)
from execution_motor.errors import ConfigError, LiveTradingRefused

from .test_alpaca_http import Script, jresp, make_broker, order_json, request

T = httpx.MockTransport(lambda r: httpx.Response(500))
BOTH = {LIVE_OPT_IN_ENV: "true", LIVE_CONFIRM_ENV: LIVE_CONFIRM_VALUE}


def test_paper_url_is_the_documented_alpaca_paper_host() -> None:
    assert PAPER_BASE_URL == "https://paper-api.alpaca.markets"


def test_no_url_means_paper() -> None:
    assert resolve_base_url(None, {}) == PAPER_BASE_URL
    assert resolve_base_url("", {}) == PAPER_BASE_URL


def test_paper_url_with_trailing_slash_is_normalised() -> None:
    assert resolve_base_url(PAPER_BASE_URL + "/", {}) == PAPER_BASE_URL


def test_live_url_refused_by_default() -> None:
    with pytest.raises(LiveTradingRefused):
        resolve_base_url(LIVE_BASE_URL, {})


@pytest.mark.parametrize(
    "env",
    [
        {LIVE_OPT_IN_ENV: "true"},
        {LIVE_CONFIRM_ENV: LIVE_CONFIRM_VALUE},
        {LIVE_OPT_IN_ENV: "true", LIVE_CONFIRM_ENV: "yes"},
        {LIVE_OPT_IN_ENV: "1", LIVE_CONFIRM_ENV: LIVE_CONFIRM_VALUE},
        {LIVE_OPT_IN_ENV: "false", LIVE_CONFIRM_ENV: LIVE_CONFIRM_VALUE},
        {LIVE_OPT_IN_ENV: "", LIVE_CONFIRM_ENV: ""},
    ],
)
def test_live_url_refused_without_both_exact_opt_ins(env: dict[str, str]) -> None:
    with pytest.raises(LiveTradingRefused):
        resolve_base_url(LIVE_BASE_URL, env)


def test_live_url_needs_both_opt_ins_and_only_then_resolves() -> None:
    assert resolve_base_url(LIVE_BASE_URL, BOTH) == LIVE_BASE_URL


@pytest.mark.parametrize(
    "url",
    [
        "http://paper-api.alpaca.markets",
        "https://paper-api.alpaca.markets.evil.example",
        "https://evil.example",
        "https://paper-api.alpaca.markets/v2",
        "https://api.alpaca.markets/v2",
        "ftp://api.alpaca.markets",
        "not a url",
    ],
)
def test_unknown_endpoints_refused_even_with_both_opt_ins(url: str) -> None:
    with pytest.raises(ConfigError):
        resolve_base_url(url, BOTH)


def test_paper_broker_is_pinned_and_has_no_url_parameter() -> None:
    broker = AlpacaPaperBroker(transport=T)
    assert broker.base_url == PAPER_BASE_URL
    with pytest.raises(TypeError):
        AlpacaPaperBroker(transport=T, base_url=LIVE_BASE_URL)  # type: ignore[call-arg]


def test_base_broker_defaults_to_paper_and_refuses_live_without_opt_in() -> None:
    assert AlpacaBroker(transport=T).base_url == PAPER_BASE_URL
    with pytest.raises(LiveTradingRefused):
        AlpacaBroker(transport=T, base_url=LIVE_BASE_URL)
    with pytest.raises(LiveTradingRefused):
        AlpacaBroker(
            transport=T, base_url=LIVE_BASE_URL, live_authorisation={LIVE_OPT_IN_ENV: "true"}
        )


def test_a_transport_is_mandatory() -> None:
    """Without the gateway transport the client has no way to reach anything."""
    with pytest.raises(TypeError):
        AlpacaPaperBroker()  # type: ignore[call-arg]


def test_motor_client_has_no_credential_code_path() -> None:
    """ADR-004: credentials, their env loader and the auth headers live in broker-gateway."""
    assert not hasattr(alpaca_module, "AlpacaCredentials")
    assert not hasattr(alpaca_module, "alpaca_broker_from_env")
    source = Path(alpaca_module.__file__).read_text(encoding="utf-8")
    assert "ALPACA_API_KEY" not in source and "ALPACA_SECRET_KEY" not in source


def test_no_request_ever_carries_broker_auth_headers() -> None:
    script = Script(jresp(200, order_json()), jresp(200, []), jresp(200, order_json()))
    broker = make_broker(script)
    broker.submit_order(request())
    broker.get_positions()
    broker.get_order_by_client_id(order_json()["client_order_id"])
    for req in script.requests:
        assert "APCA-API-KEY-ID" not in req.headers
        assert "APCA-API-SECRET-KEY" not in req.headers
