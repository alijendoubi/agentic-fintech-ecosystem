"""Regime label vocabulary shared by every module.

The names must match ``RegimeLabel`` in ``agent-core/shared/proto/market_snapshot.proto``
byte-for-byte, because the Rust sensory-array and Zone B consume them as strings.
``tests/test_labels.py`` guards this against the proto file.
"""

from __future__ import annotations

from enum import StrEnum


class RegimeLabel(StrEnum):
    """Regime labels; ``UNKNOWN`` is the fail-closed value (proto ``REGIME_UNKNOWN``)."""

    UNKNOWN = "REGIME_UNKNOWN"
    TRENDING_BULL = "TRENDING_BULL"
    TRENDING_BEAR = "TRENDING_BEAR"
    HIGH_VOL_CHOP = "HIGH_VOL_CHOP"
    LOW_VOL_CHOP = "LOW_VOL_CHOP"
    CRISIS = "CRISIS"


#: The five labels a trained HMM state can carry (everything except UNKNOWN).
STATE_REGIMES: tuple[RegimeLabel, ...] = (
    RegimeLabel.TRENDING_BULL,
    RegimeLabel.TRENDING_BEAR,
    RegimeLabel.HIGH_VOL_CHOP,
    RegimeLabel.LOW_VOL_CHOP,
    RegimeLabel.CRISIS,
)

N_STATES: int = len(STATE_REGIMES)
