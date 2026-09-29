"""Parsing of Alpaca v2 REST JSON into domain types.

ASSUMPTIONS (from Alpaca's public v2 docs; NOT verified against the live service here):
  * Order objects carry: id, client_order_id, symbol, qty, filled_qty, filled_avg_price,
    status, submitted_at, filled_at. Money/quantity fields are JSON strings; numbers are
    tolerated and parsed without float (``parse_float=Decimal``).
  * Timestamps are RFC 3339 with up to nanosecond fractional seconds.
  * The order object exposes only the AGGREGATE fill (filled_qty at filled_avg_price);
    per-execution fills would need the account-activities endpoint (not used).
  * Position ``qty`` is signed for shorts; account has ``buying_power``, ``cash``, ``equity``,
    ``status``, ``trading_blocked`` and ``account_blocked``.
Anything missing or unparseable raises ``MalformedResponse``; callers treat that as ambiguous.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from .broker import AccountSnapshot, BrokerOrder, Position
from .models import ExecutionStatus

_TS_RE: Final = re.compile(
    r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)$"
)

# Unlisted statuses map to UNKNOWN (fail closed). "stopped" is deliberately UNKNOWN.
_STATUS: Final = {
    "new": ExecutionStatus.ACCEPTED,
    "accepted": ExecutionStatus.ACCEPTED,
    "pending_new": ExecutionStatus.ACCEPTED,
    "accepted_for_bidding": ExecutionStatus.ACCEPTED,
    "calculated": ExecutionStatus.ACCEPTED,
    "held": ExecutionStatus.ACCEPTED,
    "pending_cancel": ExecutionStatus.ACCEPTED,  # still live until cancelled
    "pending_replace": ExecutionStatus.ACCEPTED,
    "partially_filled": ExecutionStatus.PARTIALLY_FILLED,
    "filled": ExecutionStatus.FILLED,
    "canceled": ExecutionStatus.CANCELLED,
    "replaced": ExecutionStatus.CANCELLED,
    "expired": ExecutionStatus.EXPIRED,
    "done_for_day": ExecutionStatus.EXPIRED,
    "rejected": ExecutionStatus.REJECTED,
    "suspended": ExecutionStatus.REJECTED,
}


class MalformedResponse(ValueError):
    """The broker response is not shaped as assumed."""


def loads(text: str) -> Any:
    try:
        return json.loads(text, parse_float=Decimal)
    except ValueError as exc:
        raise MalformedResponse("body is not valid JSON") from exc


def _dec(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
        raise MalformedResponse(f"{name} is not a decimal string")
    try:
        out = Decimal(str(value))
    except InvalidOperation as exc:
        raise MalformedResponse(f"{name} is not a valid decimal") from exc
    if not out.is_finite():
        raise MalformedResponse(f"{name} is not finite")
    return out


def _opt_dec(value: Any, name: str) -> Decimal | None:
    return None if value is None else _dec(value, name)


def _str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise MalformedResponse(f"{name} missing")
    return value


def parse_ts_ns(value: Any, name: str) -> int | None:
    if value is None:
        return None
    match = _TS_RE.match(value) if isinstance(value, str) else None
    if match is None:
        raise MalformedResponse(f"{name} is not an RFC 3339 timestamp")
    *parts, frac, tz = match.groups()
    year, month, day, hour, minute, second = (int(p) for p in parts)
    try:
        dt = datetime(year, month, day, hour, minute, second, tzinfo=UTC)
    except ValueError as exc:
        raise MalformedResponse(f"{name} is not a valid time") from exc
    offset_s = 0
    if tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        offset_s = sign * (int(tz[1:3]) * 3600 + int(tz[4:6]) * 60)
    seconds = int(dt.timestamp()) - offset_s
    return seconds * 1_000_000_000 + int((frac or "").ljust(9, "0"))


def parse_order(body: Any) -> BrokerOrder:
    if not isinstance(body, dict):
        raise MalformedResponse("order is not an object")
    raw_status = _str(body.get("status"), "status")
    return BrokerOrder(
        broker_order_id=_str(body.get("id"), "id"),
        client_order_id=_str(body.get("client_order_id"), "client_order_id"),
        symbol=_str(body.get("symbol"), "symbol"),
        status=_STATUS.get(raw_status, ExecutionStatus.UNKNOWN),
        raw_status=raw_status,
        quantity=_dec(body.get("qty"), "qty"),
        filled_quantity=_dec(body.get("filled_qty", "0"), "filled_qty"),
        filled_avg_price=_opt_dec(body.get("filled_avg_price"), "filled_avg_price"),
        submitted_at_ns=parse_ts_ns(body.get("submitted_at"), "submitted_at"),
        filled_at_ns=parse_ts_ns(body.get("filled_at"), "filled_at"),
    )


def parse_orders(body: Any) -> tuple[BrokerOrder, ...]:
    """Strict: any item that is not a well-formed order fails the whole read. Unlike
    positions, a skipped open order is one the kill-switch sweep would never cancel."""
    if not isinstance(body, list):
        raise MalformedResponse("orders is not a list")
    return tuple(parse_order(item) for item in body)


def parse_positions(body: Any) -> tuple[Position, ...]:
    if not isinstance(body, list):
        raise MalformedResponse("positions is not a list")
    return tuple(
        Position(
            symbol=_str(p.get("symbol"), "symbol"),
            quantity=_dec(p.get("qty"), "qty"),
            avg_entry_price=_dec(p.get("avg_entry_price"), "avg_entry_price"),
            market_value=_dec(p.get("market_value"), "market_value"),
        )
        for p in body
        if isinstance(p, dict)
    )


def parse_account(body: Any) -> AccountSnapshot:
    if not isinstance(body, dict):
        raise MalformedResponse("account is not an object")
    blocked = bool(body.get("trading_blocked", True)) or bool(body.get("account_blocked", True))
    return AccountSnapshot(
        status=_str(body.get("status"), "status"),
        buying_power=_dec(body.get("buying_power"), "buying_power"),
        cash=_dec(body.get("cash"), "cash"),
        equity=_dec(body.get("equity"), "equity"),
        trading_blocked=blocked,
    )
