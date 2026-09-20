"""Core value types for the backtesting engine.

Everything here is an immutable value object. Enum members mirror the protobuf
contracts in ``agent-core/shared/proto`` (``market_snapshot.proto`` and
``trade_signal.proto``) so that strategies written against the backtester speak the
same vocabulary as the live Cognitive Core.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import IntEnum
from typing import Final


class BacktestError(Exception):
    """Base class for every error raised deliberately by this package."""


class LookaheadError(BacktestError):
    """A strategy attempted to read data with a timestamp after the simulated time."""


class DataValidationError(BacktestError):
    """Input market data is malformed (ordering, NaN, non-positive prices, ...)."""


class ConfigError(BacktestError):
    """A configuration value is out of its valid range."""


class AccountingInvariantError(BacktestError):
    """A portfolio accounting invariant (cash conservation, equity identity) failed."""


class RegimeLabel(IntEnum):
    """Mirror of ``RegimeLabel`` in ``market_snapshot.proto`` (same names and numbers)."""

    REGIME_UNKNOWN = 0
    TRENDING_BULL = 1
    TRENDING_BEAR = 2
    HIGH_VOL_CHOP = 3
    LOW_VOL_CHOP = 4
    CRISIS = 5


#: The five HMM states that SHARP promotion (docs/processes/sharp-promotion.md) requires.
HMM_STATES: Final[tuple[RegimeLabel, ...]] = (
    RegimeLabel.TRENDING_BULL,
    RegimeLabel.TRENDING_BEAR,
    RegimeLabel.HIGH_VOL_CHOP,
    RegimeLabel.LOW_VOL_CHOP,
    RegimeLabel.CRISIS,
)


def parse_regime(value: object) -> RegimeLabel:
    """Parse a regime label from a proto name (case-insensitive), an int, or an enum.

    Empty strings and ``None`` map to ``REGIME_UNKNOWN``. Anything unrecognised raises
    ``DataValidationError`` rather than silently degrading to UNKNOWN.
    """
    if isinstance(value, RegimeLabel):
        return value
    if value is None:
        return RegimeLabel.REGIME_UNKNOWN
    if isinstance(value, bool):
        raise DataValidationError(f"invalid regime label: {value!r}")
    if isinstance(value, int):
        try:
            return RegimeLabel(value)
        except ValueError as exc:
            raise DataValidationError(f"invalid regime number: {value}") from exc
    text = str(value).strip().upper()
    if text == "":
        return RegimeLabel.REGIME_UNKNOWN
    if text.isdigit():
        return parse_regime(int(text))
    try:
        return RegimeLabel[text]
    except KeyError as exc:
        raise DataValidationError(f"unknown regime label: {value!r}") from exc


class Side(IntEnum):
    """Mirror of ``SignalSide`` in ``trade_signal.proto`` (without SIDE_UNKNOWN)."""

    BUY = 1
    SELL = 2
    SELL_SHORT = 3


@dataclass(frozen=True)
class Bar:
    """One OHLCV observation.

    ``timestamp_ns`` is the time at which the bar is *complete and observable*
    (its close time), in nanoseconds since the Unix epoch, matching
    ``MarketSnapshot.ingestion_timestamp_ns``.
    """

    symbol: str
    timestamp_ns: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    regime: RegimeLabel = RegimeLabel.REGIME_UNKNOWN


@dataclass(frozen=True)
class OrderIntent:
    """What a strategy returns; mirrors the relevant ``TradeSignal`` fields.

    ``omega`` is the Judge confidence in [0, 1] (optional). ``price_limit`` is a limit
    price (``None`` = market order, unlike the proto's 0 sentinel). ``valid_for_ns`` is
    the intent's lifetime measured from the decision time (``None`` = engine default).
    """

    symbol: str
    side: Side
    quantity: Decimal
    omega: float | None = None
    price_limit: Decimal | None = None
    valid_for_ns: int | None = None
    tag: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.quantity, Decimal):
            raise TypeError("OrderIntent.quantity must be Decimal; use OrderIntent.of()")
        if not self.quantity.is_finite() or self.quantity <= 0:
            raise ConfigError(f"order quantity must be positive, got {self.quantity}")
        if self.omega is not None and not 0.0 <= self.omega <= 1.0:
            raise ConfigError(f"omega must be within [0, 1], got {self.omega}")
        if self.price_limit is not None and self.price_limit <= 0:
            raise ConfigError("price_limit must be positive when given")
        if self.valid_for_ns is not None and self.valid_for_ns <= 0:
            raise ConfigError("valid_for_ns must be positive when given")

    @classmethod
    def of(
        cls,
        symbol: str,
        side: Side,
        quantity: int | float | Decimal,
        *,
        omega: float | None = None,
        price_limit: float | Decimal | None = None,
        valid_for_ns: int | None = None,
        tag: str = "",
    ) -> OrderIntent:
        """Convenience constructor accepting plain numbers."""
        qty = quantity if isinstance(quantity, Decimal) else Decimal(str(quantity))
        limit = (
            price_limit
            if price_limit is None or isinstance(price_limit, Decimal)
            else Decimal(str(price_limit))
        )
        return cls(symbol, side, qty, omega, limit, valid_for_ns, tag)


@dataclass(frozen=True)
class Fill:
    """A (possibly partial) execution. All money fields are Decimal."""

    order_id: int
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    reference_price: Decimal
    fee: Decimal
    timestamp_ns: int
    signal_ts_ns: int
    regime: RegimeLabel = RegimeLabel.REGIME_UNKNOWN
    tag: str = ""

    @property
    def signed_quantity(self) -> Decimal:
        """Positive for buys, negative for sells."""
        return self.quantity if self.side == Side.BUY else -self.quantity

    @property
    def notional(self) -> Decimal:
        """Absolute traded value, ``quantity * price``."""
        return self.quantity * self.price

    @property
    def slippage_cost(self) -> Decimal:
        """Adverse price move vs the reference price in currency (>= 0 for adverse fills)."""
        return (self.price - self.reference_price) * self.signed_quantity


@dataclass(frozen=True)
class ClosedTrade:
    """One completed round trip (flat -> position -> flat, or a closing leg of a flip)."""

    symbol: str
    direction: str  # "LONG" or "SHORT"
    entry_ts_ns: int
    exit_ts_ns: int
    quantity: Decimal
    avg_entry: Decimal
    avg_exit: Decimal
    gross_pnl: Decimal
    fees: Decimal
    regime: RegimeLabel = RegimeLabel.REGIME_UNKNOWN

    @property
    def net_pnl(self) -> Decimal:
        """Gross P&L minus all fees attributed to this round trip."""
        return self.gross_pnl - self.fees

    @property
    def return_fraction(self) -> float:
        """Net P&L divided by entry notional (``quantity * avg_entry``)."""
        entry_notional = self.quantity * self.avg_entry
        if entry_notional == 0:
            return 0.0
        return float(self.net_pnl / entry_notional)

    @property
    def holding_ns(self) -> int:
        """Time between the opening fill and the closing fill."""
        return self.exit_ts_ns - self.entry_ts_ns


@dataclass(frozen=True)
class Rejection:
    """An order (or its remainder) that was refused, with a machine-readable reason."""

    order_id: int
    symbol: str
    timestamp_ns: int
    reason: str
    quantity: Decimal
