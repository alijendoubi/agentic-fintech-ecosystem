"""Trusted price source for the notional cap.

The attestation signs ``limit_price`` and ``stop_price`` only; a MARKET order carries no signed
price at all. The cap price for such an order therefore has to come from the motor's OWN quote
source, never from the caller of ``execute``/``handle_decision`` (that value is unsigned).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class PriceQuote:
    """Last known price for a symbol and when it was observed (ns since epoch)."""

    price: Decimal
    as_of_ns: int


class QuoteSource(Protocol):
    def latest(self, symbol: str) -> PriceQuote | None:
        """Latest quote for ``symbol`` or None. Implementations must be trusted (in-process
        market-data feed), not derived from the order request."""
        ...
