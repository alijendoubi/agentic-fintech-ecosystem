"""Paper-only enforcement: the structural guarantee that no live order can be sent by accident."""

from __future__ import annotations

import pytest
from execution_motor.alpaca import (
    LIVE_BASE_URL,
    LIVE_CONFIRM_ENV,
    LIVE_CONFIRM_VALUE,
    LIVE_OPT_IN_ENV,
    PAPER_BASE_URL,
    AlpacaBroker,
    AlpacaCredentials,
    AlpacaPaperBroker,
    alpaca_broker_from_env,
    resolve_base_url,
)
from execution_motor.errors import ConfigError, LiveTradingRefused

CREDS = AlpacaCredentials(api_key="PKTESTKEY123", secret_key="SECRETVALUE456")
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
    broker = AlpacaPaperBroker(CREDS)
    assert broker.base_url == PAPER_BASE_URL
    with pytest.raises(TypeError):
        AlpacaPaperBroker(CREDS, base_url=LIVE_BASE_URL)  # type: ignore[call-arg]


def test_base_broker_defaults_to_paper_and_refuses_live_without_opt_in() -> None:
    assert AlpacaBroker(CREDS).base_url == PAPER_BASE_URL
    with pytest.raises(LiveTradingRefused):
        AlpacaBroker(CREDS, base_url=LIVE_BASE_URL)
    with pytest.raises(LiveTradingRefused):
        AlpacaBroker(CREDS, base_url=LIVE_BASE_URL, live_authorisation={LIVE_OPT_IN_ENV: "true"})


def test_from_env_default_path_is_paper() -> None:
    env = {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"}
    broker = alpaca_broker_from_env(env)
    assert isinstance(broker, AlpacaPaperBroker)
    assert broker.base_url == PAPER_BASE_URL


def test_from_env_with_live_url_but_no_opt_in_refuses() -> None:
    env = {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s", "ALPACA_BASE_URL": LIVE_BASE_URL}
    with pytest.raises(LiveTradingRefused):
        alpaca_broker_from_env(env)


def test_from_env_with_live_url_and_one_flag_refuses() -> None:
    env = {
        "ALPACA_API_KEY": "k",
        "ALPACA_SECRET_KEY": "s",
        "ALPACA_BASE_URL": LIVE_BASE_URL,
        LIVE_OPT_IN_ENV: "true",
    }
    with pytest.raises(LiveTradingRefused):
        alpaca_broker_from_env(env)


def test_paper_broker_class_never_goes_live_even_when_env_has_both_flags() -> None:
    broker = AlpacaPaperBroker(CREDS)
    assert broker.base_url == PAPER_BASE_URL  # flags are not even consulted by this class


def test_from_env_requires_credentials() -> None:
    with pytest.raises(ConfigError):
        alpaca_broker_from_env({})
    with pytest.raises(ConfigError):
        alpaca_broker_from_env({"ALPACA_API_KEY": "k"})
    with pytest.raises(ConfigError):
        alpaca_broker_from_env({"ALPACA_API_KEY": " ", "ALPACA_SECRET_KEY": "s"})


def test_credentials_never_appear_in_repr_or_str() -> None:
    for text in (repr(CREDS), str(CREDS), repr(AlpacaPaperBroker(CREDS))):
        assert "PKTESTKEY123" not in text
        assert "SECRETVALUE456" not in text
