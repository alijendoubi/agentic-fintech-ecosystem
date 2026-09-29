"""Submit authorisation: the gateway's whole security decision, pure and synchronous.

A submit is forwarded only when ALL of these hold, checked in this order (ADR-004 Option C,
Phase 3 spec section 7):

1. ``canonical_text`` parses as ``afe-attest-v2`` and re-serialises byte for byte through the
   same ``build_canonical_text`` Aegis's text is verified with in execution-motor (any other
   version, v1 included, is refused).
2. The proof's ``key_id`` equals the text's ``key_id`` line, and the signature verifies under
   that key with Aegis's PUBLIC key from the gateway's own registry (``AegisAttestationVerifier``,
   shared with the motor; dev keys are refused in production).
3. Timing, on the gateway's clock: not expired (``now < expires_at_ns``), not decided in the
   future beyond the allowed skew, lifetime ``expires_at_ns - decided_at_ns`` no longer than
   the configured maximum (spec: 5 s), and decided after this process started (see below).
4. Every field of the outgoing order equals the attested value.
5. Single use: the attested ``signal_id`` has not been forwarded before.

The broker body is then built from the ATTESTED fields, never from the caller's intent.

Replay store: IN MEMORY (a dict under a lock). Entries are dropped once their attestation can
no longer pass the expiry check. A restart empties it, so the gateway also refuses every
attestation decided before ``started_at_ns + max_clock_skew_ns``: an attestation consumed by
a previous process was decided at most ``max_clock_skew_ns`` after that process last accepted
anything, so none of them can be accepted again. The cost is that attestations in flight
across a restart are refused (fail closed). Residual risk: a backwards step of the wall clock
larger than the skew allowance could make an expired entry look valid again after it was
pruned; the broker's own duplicate client_order_id refusal (ASSUMED, Alpaca returns 422) is
the next layer. No Redis is used: the motor has none, and a shared store would add a network
dependency to the smallest component.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Protocol

from execution_motor.canonical import (
    CANONICAL_VERSION,
    SIDE_BUY,
    SIDE_SELL,
    SIDE_SELL_SHORT,
    build_canonical_text,
)
from execution_motor.errors import OrderValidationError

_TEXT_FIELDS: Final = (
    "signal_id",
    "strategy_id",
    "symbol",
    "side",
    "order_type",
    "qty_nanos",
    "limit_price_nanos",
    "stop_price_nanos",
    "decided_at_ns",
    "expires_at_ns",
    "aegis_state_seq",
    "limits_config_sha256",
    "key_id",
)
_INT_FIELDS: Final = frozenset(
    {
        "qty_nanos",
        "limit_price_nanos",
        "stop_price_nanos",
        "decided_at_ns",
        "expires_at_ns",
        "aegis_state_seq",
    }
)
_INT_RE: Final = re.compile(r"^(0|[1-9][0-9]{0,19})$")  # canonical, non-negative decimal
_BROKER_SIDE: Final = {SIDE_BUY: "buy", SIDE_SELL: "sell", SIDE_SELL_SHORT: "sell"}
_BROKER_TYPE: Final = {
    "MARKET": "market",
    "LIMIT": "limit",
    "STOP": "stop",
    "STOP_LIMIT": "stop_limit",
}
ALLOWED_TIME_IN_FORCE: Final = frozenset({"day"})
_NANOS: Final = 9


class Refusal(StrEnum):
    MALFORMED_REQUEST = "malformed_request"
    UNSUPPORTED_VERSION = "unsupported_version"
    NON_CANONICAL_TEXT = "non_canonical_text"
    KEY_ID_MISMATCH = "key_id_mismatch"
    SIGNATURE_INVALID = "signature_invalid"
    EXPIRED = "expired"
    FROM_FUTURE = "from_future"
    LIFETIME_TOO_LONG = "lifetime_too_long"
    PREDATES_GATEWAY_START = "predates_gateway_start"
    ORDER_MISMATCH = "order_mismatch"
    TIME_IN_FORCE_NOT_ALLOWED = "time_in_force_not_allowed"
    REPLAYED = "replayed"


class Refused(Exception):
    def __init__(self, reason: Refusal, detail: str = "") -> None:
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class AttestedOrder:
    """The fields of one verified ``afe-attest-v2`` text."""

    signal_id: str
    strategy_id: str
    symbol: str
    side: str
    order_type: str
    qty_nanos: int
    limit_price_nanos: int
    stop_price_nanos: int
    decided_at_ns: int
    expires_at_ns: int
    aegis_state_seq: int
    limits_config_sha256: str
    key_id: str


@dataclass(frozen=True)
class Intent:
    """What the caller asks to send (``OrderIntent`` in broker_gateway.proto)."""

    client_order_id: str
    symbol: str
    side: str
    order_type: str
    qty_nanos: int
    limit_price_nanos: int
    stop_price_nanos: int
    time_in_force: str

    @classmethod
    def from_proto(cls, msg: Any) -> Intent:
        return cls(
            client_order_id=str(msg.client_order_id),
            symbol=str(msg.symbol),
            side=str(msg.side),
            order_type=str(msg.order_type),
            qty_nanos=int(msg.qty_nanos),
            limit_price_nanos=int(msg.limit_price_nanos),
            stop_price_nanos=int(msg.stop_price_nanos),
            time_in_force=str(msg.time_in_force),
        )


class SignatureVerifier(Protocol):
    """Structural type of ``execution_motor.verifiers.AegisAttestationVerifier``."""

    def verify(self, payload: bytes, signature: bytes, key_id: str) -> bool: ...


def parse_canonical_text(text: bytes) -> AttestedOrder:
    """Strict parser for the v2 text. Raises ``Refused``; never returns a partial result."""
    try:
        decoded = text.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise Refused(Refusal.NON_CANONICAL_TEXT, "not UTF-8") from None
    lines = decoded.split("\n")
    if not lines or lines[0] != CANONICAL_VERSION:
        raise Refused(Refusal.UNSUPPORTED_VERSION, "only afe-attest-v2 is accepted")
    # "\n"-terminated: the split leaves exactly one empty string at the end.
    if len(lines) != len(_TEXT_FIELDS) + 2 or lines[-1] != "":
        raise Refused(Refusal.NON_CANONICAL_TEXT, "wrong number of lines")
    values: dict[str, Any] = {}
    for name, line in zip(_TEXT_FIELDS, lines[1:-1], strict=True):
        prefix = f"{name}="
        if not line.startswith(prefix):
            raise Refused(Refusal.NON_CANONICAL_TEXT, f"expected {name}")
        raw = line[len(prefix) :]
        if name in _INT_FIELDS:
            if not _INT_RE.fullmatch(raw):
                raise Refused(Refusal.NON_CANONICAL_TEXT, f"{name} is not a canonical integer")
            values[name] = int(raw)
        else:
            values[name] = raw
    if values["side"] not in _BROKER_SIDE or values["order_type"] not in _BROKER_TYPE:
        raise Refused(Refusal.NON_CANONICAL_TEXT, "unknown side or order_type")
    attested = AttestedOrder(**values)
    try:
        rebuilt = build_canonical_text(**values)
    except OrderValidationError:
        raise Refused(Refusal.NON_CANONICAL_TEXT, "forbidden characters") from None
    if rebuilt != text:
        raise Refused(Refusal.NON_CANONICAL_TEXT, "does not re-serialise identically")
    return attested


def nanos_to_decimal_text(value: int) -> str:
    """Exact int nanos -> plain decimal string for the broker body (no float anywhere)."""
    return format(Decimal(value).scaleb(-_NANOS).normalize(), "f")


def broker_body(order: AttestedOrder, time_in_force: str) -> dict[str, str]:
    """Alpaca POST /v2/orders body, built only from attested fields."""
    body = {
        "symbol": order.symbol,
        "qty": nanos_to_decimal_text(order.qty_nanos),
        "side": _BROKER_SIDE[order.side],
        "type": _BROKER_TYPE[order.order_type],
        "time_in_force": time_in_force,
        "client_order_id": order.signal_id,
    }
    if order.limit_price_nanos:
        body["limit_price"] = nanos_to_decimal_text(order.limit_price_nanos)
    if order.stop_price_nanos:
        body["stop_price"] = nanos_to_decimal_text(order.stop_price_nanos)
    return body


def _mismatched_fields(order: AttestedOrder, intent: Intent) -> list[str]:
    expected = {
        "client_order_id": order.signal_id,
        "symbol": order.symbol,
        "side": _BROKER_SIDE[order.side],
        "order_type": _BROKER_TYPE[order.order_type],
        "qty_nanos": order.qty_nanos,
        "limit_price_nanos": order.limit_price_nanos,
        "stop_price_nanos": order.stop_price_nanos,
    }
    return [name for name, value in expected.items() if getattr(intent, name) != value]


class ReplayStore:
    """In-memory single-use set of forwarded ``signal_id`` values (see module docstring)."""

    def __init__(self, retention_after_expiry_ns: int) -> None:
        self._retention = retention_after_expiry_ns
        self._seen: dict[str, int] = {}
        self._lock = threading.Lock()

    def claim(self, signal_id: str, expires_at_ns: int, now_ns: int) -> bool:
        """True the first time ``signal_id`` is claimed; False afterwards."""
        with self._lock:
            stale = [k for k, exp in self._seen.items() if exp + self._retention < now_ns]
            for key in stale:
                del self._seen[key]
            if signal_id in self._seen:
                return False
            self._seen[signal_id] = expires_at_ns
            return True

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)


@dataclass(frozen=True)
class Authorised:
    order: AttestedOrder
    body: dict[str, str]


class SubmitAuthoriser:
    def __init__(
        self,
        verifier: SignatureVerifier,
        *,
        max_ttl_ns: int,
        max_clock_skew_ns: int,
        started_at_ns: int,
        clock_ns: Callable[[], int],
    ) -> None:
        if max_ttl_ns <= 0 or max_clock_skew_ns < 0:
            raise ValueError("max_ttl_ns must be > 0 and max_clock_skew_ns >= 0")
        self._verifier = verifier
        self._max_ttl = max_ttl_ns
        self._skew = max_clock_skew_ns
        self._not_before = started_at_ns + max_clock_skew_ns
        self._clock = clock_ns
        self._replay = ReplayStore(retention_after_expiry_ns=max_clock_skew_ns)

    def authorise(
        self, canonical_text: bytes, signature: bytes, key_id: str, intent: Intent
    ) -> Authorised:
        """Return the broker body to send, or raise ``Refused``. Claims the signal last."""
        if not canonical_text or not signature or not key_id:
            raise Refused(Refusal.MALFORMED_REQUEST, "attestation is missing")
        order = parse_canonical_text(canonical_text)
        if order.key_id != key_id:
            raise Refused(Refusal.KEY_ID_MISMATCH)
        if not self._verified(canonical_text, signature, key_id):
            raise Refused(Refusal.SIGNATURE_INVALID)
        now = self._clock()
        self._check_timing(order, now)
        mismatched = _mismatched_fields(order, intent)
        if mismatched:
            raise Refused(Refusal.ORDER_MISMATCH, ",".join(mismatched))
        if intent.time_in_force not in ALLOWED_TIME_IN_FORCE:
            raise Refused(Refusal.TIME_IN_FORCE_NOT_ALLOWED)
        if not self._replay.claim(order.signal_id, order.expires_at_ns, now):
            raise Refused(Refusal.REPLAYED)
        return Authorised(order=order, body=broker_body(order, intent.time_in_force))

    def _verified(self, text: bytes, signature: bytes, key_id: str) -> bool:
        try:
            verdict = self._verifier.verify(text, signature, key_id)
        except Exception:  # noqa: BLE001 - a verifier failure is a denial (fail closed)
            return False
        return verdict is True  # truthy junk from a buggy verifier must not pass

    def _check_timing(self, order: AttestedOrder, now: int) -> None:
        if now >= order.expires_at_ns:
            raise Refused(Refusal.EXPIRED)
        if order.decided_at_ns > now + self._skew:
            raise Refused(Refusal.FROM_FUTURE)
        if order.expires_at_ns - order.decided_at_ns > self._max_ttl:
            raise Refused(Refusal.LIFETIME_TOO_LONG)
        if order.decided_at_ns < self._not_before:
            raise Refused(Refusal.PREDATES_GATEWAY_START)
