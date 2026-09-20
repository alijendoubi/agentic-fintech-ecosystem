"""Frozen domain contracts: Order (from OrderRequest), attestation, ExecutionReport.

All money and quantities are Decimal. The proto carries doubles; conversion happens once,
in ``proto_adapter``, via ``Decimal(str(x))`` (shortest round-trip repr, never binary noise).
"""

from __future__ import annotations

import re
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
# Doubles as the broker client_order_id (Alpaca limit: 128 chars); keep it strict and log-safe.
_ID_RE = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")
_ZERO = Decimal(0)
_BPS = Decimal(10_000)


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class ExecAlgo(StrEnum):
    DIRECT = "direct"
    ICEBERG = "iceberg"
    POV = "pov"


class ExecutionStatus(StrEnum):
    REJECTED = "rejected"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    UNKNOWN = "unknown"  # outcome unresolved: the order MAY exist. Fail closed, page a human.


class RejectReason(StrEnum):
    HALTED = "halted"
    INVALID_ORDER = "invalid_order"
    ATTESTATION_MISSING = "attestation_missing"
    ATTESTATION_INVALID = "attestation_invalid"
    ORDER_EXPIRED = "order_expired"
    ORDER_FROM_FUTURE = "order_from_future"
    DUPLICATE_ORDER = "duplicate_order"
    NOTIONAL_UNDETERMINABLE = "notional_undeterminable"
    NOTIONAL_CAP_EXCEEDED = "notional_cap_exceeded"
    SESSION_NOTIONAL_CAP_EXCEEDED = "session_notional_cap_exceeded"
    ALGO_UNSUPPORTED = "algo_unsupported"
    NO_ELIGIBLE_VENUE = "no_eligible_venue"
    BROKER_REJECTED = "broker_rejected"
    SUBMIT_OUTCOME_UNKNOWN = "submit_outcome_unknown"
    INTERNAL_ERROR = "internal_error"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _finite_non_negative(value: Decimal, name: str) -> Decimal:
    if not value.is_finite() or value < _ZERO:
        raise ValueError(f"{name} must be a finite, non-negative Decimal")
    return value


class Order(_Frozen):
    """Domain view of ``afe.shared.OrderRequest`` (signature/status handled separately)."""

    order_id: str
    signal_id: str  # SIGNED by Aegis, so it is also the replay key (order_id is not signed)
    symbol: str
    created_at_ns: int  # informational only: NOT signed, never used for expiry
    side: Side
    order_type: OrderType
    quantity: Decimal
    limit_price: Decimal = _ZERO  # 0 = not set (proto convention)
    stop_price: Decimal = _ZERO  # 0 = not set
    preferred_venue: str = ""
    max_venue_toxicity: Decimal = _ZERO  # 0 = not set; otherwise an upper bound in [0, 1]
    algo: ExecAlgo = ExecAlgo.DIRECT
    pov_target_rate: Decimal = _ZERO
    iceberg_display_size: Decimal = _ZERO

    @property
    def client_order_id(self) -> str:
        """Idempotency key sent to the broker. Equal to order_id by design."""
        return self.order_id

    @field_validator("order_id")
    @classmethod
    def _check_order_id(cls, value: str) -> str:
        if not _ID_RE.fullmatch(value):
            raise ValueError("order_id must match [A-Za-z0-9._-]{1,64}")
        return value

    @field_validator("symbol")
    @classmethod
    def _check_symbol(cls, value: str) -> str:
        if not _SYMBOL_RE.fullmatch(value):
            raise ValueError("symbol must be 1-10 upper-case characters")
        return value

    @field_validator("created_at_ns")
    @classmethod
    def _check_created_at(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("created_at_ns must be positive")
        return value

    @field_validator("quantity")
    @classmethod
    def _check_quantity(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or value <= _ZERO:
            raise ValueError("quantity must be a finite Decimal > 0")
        return value

    @field_validator("limit_price", "stop_price", "pov_target_rate", "iceberg_display_size")
    @classmethod
    def _check_non_negative(cls, value: Decimal) -> Decimal:
        return _finite_non_negative(value, "value")

    @field_validator("max_venue_toxicity")
    @classmethod
    def _check_toxicity(cls, value: Decimal) -> Decimal:
        if not value.is_finite() or not (_ZERO <= value <= Decimal(1)):
            raise ValueError("max_venue_toxicity must be within [0, 1]")
        return value

    @model_validator(mode="after")
    def _check_type_consistency(self) -> Order:
        has_limit = self.limit_price > _ZERO
        has_stop = self.stop_price > _ZERO
        required = {
            OrderType.MARKET: (False, False),
            OrderType.LIMIT: (True, False),
            OrderType.STOP: (False, True),
            OrderType.STOP_LIMIT: (True, True),
        }[self.order_type]
        if (has_limit, has_stop) != required:
            raise ValueError(f"price fields inconsistent with order_type {self.order_type.value}")
        return self


class Attestation(_Frozen):
    """Aegis attestation: signature over ``signed_payload`` under ``key_id``.

    Empty signature/key_id are representable on purpose so a missing attestation produces a
    REJECTED(ATTESTATION_MISSING) report instead of an exception.
    """

    signature: bytes
    key_id: str
    signed_payload: bytes  # canonical text the signature covers (see proto_adapter)
    payload_sha256: bytes = b""  # Attestation.payload_sha256; must equal SHA-256(signed_payload)
    decided_at_ns: int = 0  # signed by Aegis
    expires_at_ns: int = 0  # signed by Aegis; 0 = absent -> treated as expired


class AttestedOrder(_Frozen):
    order: Order
    attestation: Attestation


class Fill(_Frozen):
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    timestamp_ns: int
    venue: str


class ExecutionReport(_Frozen):
    """Result of one execution attempt; carries what the ComplianceManifest needs."""

    order_id: str
    client_order_id: str
    signal_id: str
    symbol: str
    side: Side | None = None
    status: ExecutionStatus
    requested_quantity: Decimal
    reject_reason: RejectReason | None = None
    reject_detail: str = ""
    broker_order_id: str | None = None
    venue: str | None = None
    fills: tuple[Fill, ...] = ()
    arrival_price: Decimal | None = None  # reference price at decision time, if supplied
    received_at_ns: int
    submitted_at_ns: int | None = None
    updated_at_ns: int

    @model_validator(mode="after")
    def _check_reason_matches_status(self) -> ExecutionReport:
        rejected = self.status is ExecutionStatus.REJECTED
        if rejected and self.reject_reason is None:
            raise ValueError("a REJECTED report requires a reject_reason")
        if not rejected and self.reject_reason is not None:
            if self.status is ExecutionStatus.UNKNOWN:
                return self
            raise ValueError("reject_reason is only valid on REJECTED or UNKNOWN reports")
        return self

    @property
    def filled_quantity(self) -> Decimal:
        return sum((f.quantity for f in self.fills), _ZERO)

    @property
    def avg_fill_price(self) -> Decimal | None:
        qty = self.filled_quantity
        if qty == _ZERO:
            return None
        return sum((f.quantity * f.price for f in self.fills), _ZERO) / qty

    @property
    def slippage_bps(self) -> Decimal | None:
        """Signed cost vs arrival price in bps; positive = worse for us (paid up / sold down)."""
        avg = self.avg_fill_price
        if avg is None or self.arrival_price is None or self.side is None:
            return None
        raw = (avg - self.arrival_price) / self.arrival_price * _BPS
        return raw if self.side is Side.BUY else -raw
