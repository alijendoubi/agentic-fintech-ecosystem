"""The only code in the platform that holds and presents Alpaca credentials (ADR-004).

``AlpacaForwarder`` knows exactly six broker calls, each a fixed method + path; there is no
generic "forward this URL" operation. It does not parse responses: status and body go back to
execution-motor unchanged, and the motor's Alpaca client (``execution_motor/alpaca.py``) keeps
all parsing, reconciliation and read-retry logic. The forwarder itself never retries: a
submit is sent exactly once, and read retries are the motor's decision.

Safety design (moved here from the motor's client, same rules):
  * Endpoint: the pinned paper host, unless BOTH ``AFE_ENABLE_LIVE_TRADING=true`` and
    ``AFE_LIVE_TRADING_CONFIRM=<exact phrase>`` are set; any other host is refused always.
  * Redirects are never followed (credentials must not leave the pinned host), and proxy
    settings are NOT read from the environment (``trust_env=False``). TODO(owner): if
    production egress must go through a proxy, configure it explicitly here.
  * Credentials come from env only, are refused when they look like a template value, and are
    never logged or shown in ``repr``.

ASSUMED API shape (Alpaca public v2 docs, unverified here, same assumptions the motor made):
POST /v2/orders, GET /v2/orders:by_client_order_id?client_order_id=,
GET /v2/orders?status=open&limit=500&direction=asc, DELETE /v2/orders/{id}, GET /v2/positions,
GET /v2/account; auth headers APCA-API-KEY-ID / APCA-API-SECRET-KEY.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

import httpx
from execution_motor.errors import ConfigError

PAPER_BASE_URL: Final = "https://paper-api.alpaca.markets"
LIVE_BASE_URL: Final = "https://api.alpaca.markets"
LIVE_OPT_IN_ENV: Final = "AFE_ENABLE_LIVE_TRADING"
LIVE_CONFIRM_ENV: Final = "AFE_LIVE_TRADING_CONFIRM"
LIVE_CONFIRM_VALUE: Final = "I-UNDERSTAND-THIS-TRADES-REAL-MONEY"

# Same page the motor's kill-switch sweep expects (execution_motor/alpaca.py _OPEN_ORDERS_PAGE).
OPEN_ORDERS_PARAMS: Final = {"status": "open", "limit": "500", "direction": "asc"}
# Broker order ids (cancel) and client order ids (lookup): the motor's own id alphabets.
_BROKER_ID_RE: Final = re.compile(r"^[A-Za-z0-9\-]{1,64}$")
_CLIENT_ID_RE: Final = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")

#: Template fragments refused as credentials (ALI-21; same list the motor used).
_PLACEHOLDER_FRAGMENTS: Final = (
    "change-me",
    "change_me",
    "changeme",
    "replace-me",
    "replaceme",
    "placeholder",
    "your-secret",
    "yoursecret",
    "your-key",
    "yourkey",
    "example",
)


class BrokerTransportError(Exception):
    """No HTTP response came back (connect error, timeout, protocol error). The request may
    or may not have reached the broker. The message never contains credentials."""


class InvalidIdentifier(ValueError):
    """An order id failed the allow-list regex; nothing was sent."""


@dataclass(frozen=True)
class AlpacaCredentials:
    api_key: str = field(repr=False)
    secret_key: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.api_key.strip() or not self.secret_key.strip():
            raise ConfigError("Alpaca credentials must be non-empty")
        for value in (self.api_key, self.secret_key):
            lowered = value.lower()
            if any(fragment in lowered for fragment in _PLACEHOLDER_FRAGMENTS):
                raise ConfigError("Alpaca credentials look like a placeholder; set real keys")

    def __repr__(self) -> str:
        return "AlpacaCredentials(<redacted>)"

    __str__ = __repr__

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> AlpacaCredentials:
        key = environ.get("ALPACA_API_KEY", "")
        secret = environ.get("ALPACA_SECRET_KEY", "")
        if not key.strip() or not secret.strip():
            raise ConfigError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required")
        return cls(api_key=key, secret_key=secret)


def resolve_base_url(requested: str | None, environ: Mapping[str, str]) -> str:
    """Return the endpoint to use. Default and only unconditional answer: paper."""
    if requested is None or not requested.strip():
        return PAPER_BASE_URL
    url = requested.strip().rstrip("/")
    if url == PAPER_BASE_URL:
        return PAPER_BASE_URL
    if url == LIVE_BASE_URL:
        if (
            environ.get(LIVE_OPT_IN_ENV) == "true"
            and environ.get(LIVE_CONFIRM_ENV) == LIVE_CONFIRM_VALUE
        ):
            return LIVE_BASE_URL
        raise ConfigError(f"live endpoint requires {LIVE_OPT_IN_ENV}=true AND {LIVE_CONFIRM_ENV}")
    raise ConfigError(
        "unrecognised Alpaca endpoint; only the paper (or gated live) host is allowed"
    )


@dataclass(frozen=True)
class HttpReply:
    status: int
    body: bytes


class AlpacaForwarder:
    def __init__(
        self,
        credentials: AlpacaCredentials,
        *,
        base_url: str = PAPER_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = 5.0,
    ) -> None:
        if base_url not in (PAPER_BASE_URL, LIVE_BASE_URL):
            raise ConfigError("base_url must come from resolve_base_url")
        self._base_url = base_url
        self._client = httpx.Client(
            base_url=base_url,
            headers={
                "APCA-API-KEY-ID": credentials.api_key,
                "APCA-API-SECRET-KEY": credentials.secret_key,
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, 2.0)),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def __repr__(self) -> str:
        return f"AlpacaForwarder(base_url={self._base_url!r})"

    @property
    def base_url(self) -> str:
        return self._base_url

    def submit(self, body: Mapping[str, str]) -> HttpReply:
        return self._send("POST", "/v2/orders", json=dict(body))

    def get_order_by_client_order_id(self, client_order_id: str) -> HttpReply:
        if not _CLIENT_ID_RE.fullmatch(client_order_id):
            raise InvalidIdentifier("client_order_id")
        return self._send(
            "GET", "/v2/orders:by_client_order_id", params={"client_order_id": client_order_id}
        )

    def list_open_orders(self) -> HttpReply:
        return self._send("GET", "/v2/orders", params=dict(OPEN_ORDERS_PARAMS))

    def cancel(self, broker_order_id: str) -> HttpReply:
        if not _BROKER_ID_RE.fullmatch(broker_order_id):
            raise InvalidIdentifier("broker_order_id")
        return self._send("DELETE", f"/v2/orders/{broker_order_id}")

    def positions(self) -> HttpReply:
        return self._send("GET", "/v2/positions")

    def account(self) -> HttpReply:
        return self._send("GET", "/v2/account")

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, str] | None = None,
    ) -> HttpReply:
        try:
            resp = self._client.request(method, path, params=params, json=json)
        except httpx.HTTPError as exc:
            # Only the exception type: httpx messages can echo request details.
            raise BrokerTransportError(type(exc).__name__) from None
        return HttpReply(status=resp.status_code, body=resp.content)
