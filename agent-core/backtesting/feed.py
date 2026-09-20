"""Point-in-time data feed: look-ahead is structurally unavailable to strategies.

A strategy never receives the underlying arrays. It receives a :class:`MarketView`
whose :class:`SymbolHistory` objects were constructed with an ``end`` cursor equal to
the number of bars with ``timestamp <= now``. Every accessor either

* returns a *copy* (so ``ndarray.base`` cannot be used to reach the full array), or
* raises :class:`LookaheadError` when asked for anything beyond the cursor.

There is deliberately no public attribute that exposes the full series. (Python cannot
stop a determined caller from reading ``_series`` - this defends against accidental
look-ahead, which is the failure mode that matters for research code, not malice.)
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from .data import MarketData, SymbolSeries
from .models import Bar, DataValidationError, LookaheadError


class SymbolHistory:
    """Read-only view of one symbol's bars with ``timestamp <= now_ns``."""

    __slots__ = ("_end", "_series", "now_ns", "symbol")

    def __init__(self, series: SymbolSeries, end: int, now_ns: int) -> None:
        self._series = series
        self._end = end
        self.now_ns = now_ns
        self.symbol = series.symbol

    def __len__(self) -> int:
        return self._end

    def __getitem__(self, index: int | slice) -> Bar | tuple[Bar, ...]:
        if isinstance(index, slice):
            return self._slice(index)
        if index >= self._end or index < -self._end:
            raise LookaheadError(
                f"{self.symbol}: index {index} is beyond the {self._end} bars visible at "
                f"t={self.now_ns}"
            )
        return self._series.bar(index if index >= 0 else self._end + index)

    def _slice(self, index: slice) -> tuple[Bar, ...]:
        if index.stop is not None and index.stop > self._end:
            raise LookaheadError(
                f"{self.symbol}: slice stop {index.stop} is beyond the {self._end} visible bars"
            )
        indices = range(*index.indices(self._end))
        return tuple(self._series.bar(i) for i in indices)

    @property
    def latest(self) -> Bar | None:
        """The most recent visible bar, or ``None`` if the symbol has not started."""
        return None if self._end == 0 else self._series.bar(self._end - 1)

    def at(self, timestamp_ns: int) -> Bar | None:
        """Bar stamped exactly ``timestamp_ns``; raises if that time is in the future."""
        if timestamp_ns > self.now_ns:
            raise LookaheadError(
                f"{self.symbol}: requested t={timestamp_ns} is after now={self.now_ns}"
            )
        idx = int(np.searchsorted(self._series.ts[: self._end], timestamp_ns, side="left"))
        if idx < self._end and int(self._series.ts[idx]) == timestamp_ns:
            return self._series.bar(idx)
        return None

    def _tail(self, column: NDArray[np.float64], n: int | None) -> NDArray[np.float64]:
        if n is not None and n < 0:
            raise ValueError("n must be non-negative")
        start = 0 if n is None else max(0, self._end - n)
        return np.array(column[start : self._end], copy=True)

    def closes(self, n: int | None = None) -> NDArray[np.float64]:
        """Copy of the last ``n`` visible closes (all visible if ``n`` is None)."""
        return self._tail(self._series.close, n)

    def opens(self, n: int | None = None) -> NDArray[np.float64]:
        """Copy of the last ``n`` visible opens."""
        return self._tail(self._series.open, n)

    def highs(self, n: int | None = None) -> NDArray[np.float64]:
        """Copy of the last ``n`` visible highs."""
        return self._tail(self._series.high, n)

    def lows(self, n: int | None = None) -> NDArray[np.float64]:
        """Copy of the last ``n`` visible lows."""
        return self._tail(self._series.low, n)

    def volumes(self, n: int | None = None) -> NDArray[np.float64]:
        """Copy of the last ``n`` visible volumes."""
        return self._tail(self._series.volume, n)

    def timestamps(self, n: int | None = None) -> NDArray[np.int64]:
        """Copy of the last ``n`` visible timestamps (ns)."""
        start = 0 if n is None else max(0, self._end - n)
        return np.array(self._series.ts[start : self._end], copy=True)


class MarketView:
    """Everything a strategy may know about the market at ``now_ns``."""

    __slots__ = ("_histories", "now_ns")

    def __init__(self, now_ns: int, histories: Mapping[str, SymbolHistory]) -> None:
        self.now_ns = now_ns
        self._histories = dict(histories)

    @property
    def symbols(self) -> tuple[str, ...]:
        """All symbols in the universe (some may not have started trading yet)."""
        return tuple(self._histories)

    def history(self, symbol: str) -> SymbolHistory:
        """Point-in-time history for ``symbol``."""
        try:
            return self._histories[symbol]
        except KeyError as exc:
            raise DataValidationError(f"unknown symbol {symbol!r}") from exc

    def latest(self, symbol: str) -> Bar | None:
        """Most recent bar for ``symbol`` at or before ``now_ns``."""
        return self.history(symbol).latest

    def at(self, symbol: str, timestamp_ns: int) -> Bar | None:
        """Bar at an exact past timestamp; raises :class:`LookaheadError` for the future."""
        return self.history(symbol).at(timestamp_ns)


@dataclass(frozen=True)
class FeedEvent:
    """One simulated instant: the bars that just became observable plus the safe view."""

    ts_ns: int
    new_bars: Mapping[str, Bar]
    view: MarketView


class PointInTimeFeed:
    """Iterates the merged timeline of a :class:`MarketData`, never running ahead."""

    def __init__(self, data: MarketData) -> None:
        self._data = data

    @property
    def data(self) -> MarketData:
        """The underlying data (engine-side only; strategies get views)."""
        return self._data

    def events(self) -> Iterator[FeedEvent]:
        """Yield one :class:`FeedEvent` per timestamp on the merged timeline."""
        symbols = self._data.symbols
        series = {s: self._data.series(s) for s in symbols}
        cursor = dict.fromkeys(symbols, 0)
        for raw_ts in self._data.timeline:
            ts = int(raw_ts)
            new_bars: dict[str, Bar] = {}
            for s in symbols:
                i = cursor[s]
                if i < len(series[s]) and int(series[s].ts[i]) == ts:
                    new_bars[s] = series[s].bar(i)
                    cursor[s] = i + 1
            histories = {s: SymbolHistory(series[s], cursor[s], ts) for s in symbols}
            yield FeedEvent(ts, new_bars, MarketView(ts, histories))
