"""Abstract broker interface and its frozen value types. Money is Decimal throughout."""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from .models import ExecutionStatus, OrderType, Side


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AttestationProof(_Frozen):
    """The Aegis attestation for one order, forwarded so the broker gateway (ADR-004) can
    verify it independently: the canonical text the motor verified, its signature, key id."""

    canonical_text: bytes = Field(min_length=1)
    signature: bytes = Field(min_length=1)
    key_id: str = Field(min_length=1)

    def __repr__(self) -> str:
        return f"AttestationProof(key_id={self.key_id!r})"


class BrokerOrderRequest(_Frozen):
    client_order_id: str = Field(min_length=1, max_length=128)
    symbol: str
    side: Side
    order_type: OrderType
    quantity: Decimal = Field(gt=0)
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    time_in_force: str = "day"
    # Required by the broker gateway for a submit (it refuses one without it); ignored by
    # in-memory brokers. Set by ExecutionMotor from the attestation it verified.
    attestation: AttestationProof | None = None


class BrokerOrder(_Frozen):
    broker_order_id: str
    client_order_id: str
    symbol: str
    status: ExecutionStatus
    raw_status: str
    quantity: Decimal
    filled_quantity: Decimal
    filled_avg_price: Decimal | None = None
    submitted_at_ns: int | None = None
    filled_at_ns: int | None = None


class Position(_Frozen):
    symbol: str
    quantity: Decimal  # negative = short
    avg_entry_price: Decimal
    market_value: Decimal


class AccountSnapshot(_Frozen):
    status: str
    buying_power: Decimal
    cash: Decimal
    equity: Decimal
    trading_blocked: bool


class Broker(ABC):
    """A venue gateway. Implementations must never blind-retry a submit."""

    @property
    @abstractmethod
    def venue(self) -> str:
        """Stable venue name used by the router and reports."""

    @abstractmethod
    def submit_order(self, request: BrokerOrderRequest) -> BrokerOrder:
        """Submit once. Raises BrokerRejectedError (definitive reject) or SubmitOutcomeUnknown
        (ambiguous and not reconcilable). Returns the broker's truth otherwise."""

    @abstractmethod
    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        """Safe read. None = broker says the order does not exist."""

    @abstractmethod
    def list_open_orders(self) -> tuple[BrokerOrder, ...]:
        """Safe read of every order still live at the venue. Must raise rather than
        silently drop an order it cannot parse: the kill-switch sweep cancels only what
        this returns."""

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> None:
        """Cancel one order. Never retried."""

    @abstractmethod
    def get_positions(self) -> tuple[Position, ...]:
        """Safe read."""

    @abstractmethod
    def get_account(self) -> AccountSnapshot:
        """Safe read."""
