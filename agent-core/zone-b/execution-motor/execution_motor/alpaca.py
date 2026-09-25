"""Alpaca v2 REST broker client over httpx. PAPER-ONLY unless BOTH live opt-ins are given.

Safety design
  * ``AlpacaPaperBroker`` has NO base-url parameter: its endpoint is the pinned constant.
  * ``AlpacaBroker`` accepts the live URL only when ``live_authorisation`` contains BOTH
    ``AFE_ENABLE_LIVE_TRADING=true`` and ``AFE_LIVE_TRADING_CONFIRM=<exact phrase>``; it is
    only handed ``os.environ`` by ``alpaca_broker_from_env``. Any other host is refused always.
  * Redirects are never followed (credentials must not leave the pinned host).
  * Retries with jittered exponential backoff apply ONLY to safe GET reads. A submit is sent
    exactly once. On an ambiguous outcome (timeout, transport error, 408/429/5xx, other
    non-2xx/4xx, malformed 2xx) it reconciles via GET by client_order_id and returns the
    broker's truth, or raises SubmitOutcomeUnknown (fail closed; the caller must not resubmit).
  * Credentials come from env only and are never logged or shown in repr.

ASSUMED API shape (Alpaca public docs, unverified here): POST /v2/orders,
GET /v2/orders:by_client_order_id?client_order_id=, GET /v2/orders?status=open (JSON array),
DELETE /v2/orders/{id},
GET /v2/positions, GET /v2/account; auth headers APCA-API-KEY-ID / APCA-API-SECRET-KEY;
4xx (other than 408/429) means the request was refused and no order was created; duplicate
client_order_id yields 422. Response parsing assumptions: see ``alpaca_parse``.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final

import httpx
import structlog

from . import alpaca_parse as parse
from .broker import (
    AccountSnapshot,
    Broker,
    BrokerOrder,
    BrokerOrderRequest,
    Position,
)
from .errors import (
    BrokerError,
    BrokerReadError,
    BrokerRejectedError,
    ConfigError,
    LiveTradingRefused,
    SubmitOutcomeUnknown,
)

PAPER_BASE_URL: Final = "https://paper-api.alpaca.markets"
LIVE_BASE_URL: Final = "https://api.alpaca.markets"
LIVE_OPT_IN_ENV: Final = "AFE_ENABLE_LIVE_TRADING"
LIVE_CONFIRM_ENV: Final = "AFE_LIVE_TRADING_CONFIRM"
LIVE_CONFIRM_VALUE: Final = "I-UNDERSTAND-THIS-TRADES-REAL-MONEY"

_BACKOFF_BASE_S: Final = 0.1
_BACKOFF_CAP_S: Final = 2.0
_RETRYABLE_STATUS: Final = frozenset({408, 429})
_ID_RE: Final = re.compile(r"^[A-Za-z0-9\-]{1,64}$")
_MAX_MESSAGE_CHARS: Final = 200
_OPEN_ORDERS_PAGE: Final = 500  # Alpaca's documented maximum for GET /v2/orders (ASSUMED)
_log = structlog.get_logger("execution_motor.alpaca")


@dataclass(frozen=True)
class AlpacaCredentials:
    api_key: str = field(repr=False)
    secret_key: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.api_key.strip() or not self.secret_key.strip():
            raise ConfigError("Alpaca credentials must be non-empty")

    def __repr__(self) -> str:
        return "AlpacaCredentials(<redacted>)"

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
        raise LiveTradingRefused(
            f"live endpoint requires {LIVE_OPT_IN_ENV}=true AND {LIVE_CONFIRM_ENV}"
        )
    raise ConfigError(
        "unrecognised Alpaca endpoint; only the paper (or gated live) host is allowed"
    )


class AlpacaBroker(Broker):
    def __init__(
        self,
        credentials: AlpacaCredentials,
        *,
        base_url: str | None = None,
        live_authorisation: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
        read_retries: int = 3,
        timeout_s: float = 5.0,
    ) -> None:
        if read_retries < 0:
            raise ConfigError("read_retries must be >= 0")
        self._base_url = resolve_base_url(base_url, live_authorisation or {})
        self._sleep = sleep
        self._rng = rng
        self._read_retries = read_retries
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={
                "APCA-API-KEY-ID": credentials.api_key,
                "APCA-API-SECRET-KEY": credentials.secret_key,
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, 2.0)),
            follow_redirects=False,
            transport=transport,
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base_url={self._base_url!r})"

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def venue(self) -> str:
        return "alpaca-paper" if self._base_url == PAPER_BASE_URL else "alpaca-live"

    # ------------------------------------------------------------------ submit

    def submit_order(self, request: BrokerOrderRequest) -> BrokerOrder:
        try:
            resp = self._client.post("/v2/orders", json=_order_body(request))
        except httpx.HTTPError as exc:
            return self._reconcile(request, f"transport:{type(exc).__name__}")
        status = resp.status_code
        if 200 <= status < 300:
            try:
                order = parse.parse_order(parse.loads(resp.text))
            except (parse.MalformedResponse, ValueError):
                return self._reconcile(request, "malformed_2xx")
            if order.client_order_id != request.client_order_id or order.symbol != request.symbol:
                return self._reconcile(request, "mismatched_2xx")
            return order
        if 400 <= status < 500 and status not in _RETRYABLE_STATUS:
            raise BrokerRejectedError(
                f"broker rejected order (HTTP {status}): {_error_message(resp)}",
                http_status=status,
            )
        return self._reconcile(request, f"http_{status}")

    def _reconcile(self, request: BrokerOrderRequest, cause: str) -> BrokerOrder:
        _log.warning(
            "alpaca_submit_ambiguous",
            client_order_id=request.client_order_id,
            cause=cause,
        )
        try:
            found = self.get_order_by_client_id(request.client_order_id)
        except BrokerReadError as exc:
            raise SubmitOutcomeUnknown(
                f"submit outcome unknown ({cause}); reconciliation failed"
            ) from exc
        if found is None:
            raise SubmitOutcomeUnknown(
                f"submit outcome unknown ({cause}); not found at broker, may still be in flight"
            )
        _log.info("alpaca_submit_reconciled", client_order_id=request.client_order_id)
        return found

    # ------------------------------------------------------------------- reads

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        resp = self._read("/v2/orders:by_client_order_id", {"client_order_id": client_order_id})
        if resp.status_code == 404:
            return None
        order = self._parsed(resp, parse.parse_order)
        if order.client_order_id != client_order_id:
            raise BrokerReadError("broker returned a different client_order_id")
        return order

    def list_open_orders(self) -> tuple[BrokerOrder, ...]:
        resp = self._read(
            "/v2/orders",
            {"status": "open", "limit": str(_OPEN_ORDERS_PAGE), "direction": "asc"},
        )
        orders = self._parsed(resp, parse.parse_orders)
        if len(orders) >= _OPEN_ORDERS_PAGE:
            # More may exist beyond this page. The kill-switch sweep re-lists while the
            # level stays elevated, so the remainder is picked up on the next pass.
            _log.warning("alpaca_open_orders_page_full", count=len(orders))
        return orders

    def get_positions(self) -> tuple[Position, ...]:
        return self._parsed(self._read("/v2/positions", None), parse.parse_positions)

    def get_account(self) -> AccountSnapshot:
        return self._parsed(self._read("/v2/account", None), parse.parse_account)

    def _parsed[T](self, resp: httpx.Response, fn: Callable[[Any], T]) -> T:
        if resp.status_code >= 400:
            raise BrokerReadError(f"read failed: HTTP {resp.status_code}")
        try:
            return fn(parse.loads(resp.text))
        except (parse.MalformedResponse, ValueError) as exc:
            raise BrokerReadError(f"malformed response: {exc}") from exc

    def _read(self, path: str, params: dict[str, str] | None) -> httpx.Response:
        """GET with jittered exponential backoff. The ONLY retrying call path."""
        last = "unknown"
        for attempt in range(self._read_retries + 1):
            try:
                resp = self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                last = type(exc).__name__
            else:
                code = resp.status_code
                if code < 400 or code == 404:
                    return resp
                if code not in _RETRYABLE_STATUS and code < 500:
                    raise BrokerReadError(f"read failed: HTTP {code}")
                last = f"HTTP {code}"
            if attempt < self._read_retries:
                cap = min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * 2**attempt)
                self._sleep(self._rng() * cap)  # full jitter
        raise BrokerReadError(f"read failed after retries: {last}")

    # ------------------------------------------------------------------ cancel

    def cancel_order(self, broker_order_id: str) -> None:
        if not _ID_RE.fullmatch(broker_order_id):
            raise BrokerError("invalid broker order id")
        try:
            resp = self._client.delete(f"/v2/orders/{broker_order_id}")
        except httpx.HTTPError as exc:
            raise BrokerError(f"cancel failed: {type(exc).__name__}") from exc
        if not 200 <= resp.status_code < 300:
            raise BrokerError(f"cancel failed: HTTP {resp.status_code}")


class AlpacaPaperBroker(AlpacaBroker):
    """Pinned to the paper endpoint. Deliberately has no base_url / live parameter."""

    def __init__(
        self,
        credentials: AlpacaCredentials,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
        read_retries: int = 3,
        timeout_s: float = 5.0,
    ) -> None:
        super().__init__(
            credentials,
            base_url=PAPER_BASE_URL,
            live_authorisation=None,
            transport=transport,
            sleep=sleep,
            rng=rng,
            read_retries=read_retries,
            timeout_s=timeout_s,
        )


def alpaca_broker_from_env(environ: Mapping[str, str]) -> AlpacaBroker:
    """Build the broker from env. Defaults to paper; live needs both explicit opt-ins."""
    credentials = AlpacaCredentials.from_env(environ)
    url = resolve_base_url(environ.get("ALPACA_BASE_URL"), environ)
    if url == PAPER_BASE_URL:
        return AlpacaPaperBroker(credentials)
    return AlpacaBroker(credentials, base_url=url, live_authorisation=environ)


def _money(value: Decimal) -> str:
    return format(value, "f")


def _order_body(request: BrokerOrderRequest) -> dict[str, str]:
    body = {
        "symbol": request.symbol,
        "qty": _money(request.quantity),
        "side": request.side.value,
        "type": request.order_type.value,
        "time_in_force": request.time_in_force,
        "client_order_id": request.client_order_id,
    }
    if request.limit_price is not None:
        body["limit_price"] = _money(request.limit_price)
    if request.stop_price is not None:
        body["stop_price"] = _money(request.stop_price)
    return body


def _error_message(resp: httpx.Response) -> str:
    try:
        payload = parse.loads(resp.text)
        message = payload.get("message", "") if isinstance(payload, dict) else ""
    except parse.MalformedResponse:
        message = ""
    return str(message)[:_MAX_MESSAGE_CHARS]
