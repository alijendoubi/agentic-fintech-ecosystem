"""Market-data containers and loaders.

CSV format (header required, ``#`` comment lines ignored)::

    timestamp,symbol,open,high,low,close,volume[,regime]

* ``timestamp``: ISO-8601 *with* a UTC offset (``2024-03-01T14:31:00Z``), or, when the
  column is named ``timestamp_ns``, integer nanoseconds since the Unix epoch. Naive
  timestamps are rejected - a missing timezone is a silent source of look-ahead.
* ``timestamp`` is the time the bar is complete (its close time).
* ``regime`` (optional): a ``RegimeLabel`` name from ``market_snapshot.proto``
  (``TRENDING_BULL`` ...) or its integer value.

Parquet with the same columns is supported when ``pyarrow`` is installed.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from numpy.typing import NDArray

from .models import Bar, DataValidationError, RegimeLabel, parse_regime

REQUIRED_COLUMNS: tuple[str, ...] = ("symbol", "open", "high", "low", "close", "volume")
SYNTHETIC_MARKER = "# synthetic-data"

IntArray = NDArray[np.int64]
FloatArray = NDArray[np.float64]


def _readonly(array: Any, dtype: Any) -> Any:
    """Return a private, read-only copy of ``array`` with the given dtype."""
    out: Any = np.array(array, dtype=dtype, copy=True)
    out.setflags(write=False)
    return out


@dataclass(frozen=True)
class SymbolSeries:
    """Columnar, read-only OHLCV history for one symbol (sorted, strictly increasing)."""

    symbol: str
    ts: IntArray
    open: FloatArray
    high: FloatArray
    low: FloatArray
    close: FloatArray
    volume: FloatArray
    regime: NDArray[np.int8]

    @classmethod
    def create(
        cls,
        symbol: str,
        ts: Sequence[int] | IntArray,
        open_: Sequence[float] | FloatArray,
        high: Sequence[float] | FloatArray,
        low: Sequence[float] | FloatArray,
        close: Sequence[float] | FloatArray,
        volume: Sequence[float] | FloatArray,
        regime: Sequence[int] | NDArray[np.int8] | None = None,
    ) -> SymbolSeries:
        """Validate and freeze the given columns."""
        n = len(ts)
        regimes = np.zeros(n, dtype=np.int8) if regime is None else np.asarray(regime)
        series = cls(
            symbol,
            _readonly(np.asarray(ts), np.int64),
            _readonly(np.asarray(open_), np.float64),
            _readonly(np.asarray(high), np.float64),
            _readonly(np.asarray(low), np.float64),
            _readonly(np.asarray(close), np.float64),
            _readonly(np.asarray(volume), np.float64),
            _readonly(regimes, np.int8),
        )
        series._validate()
        return series

    def __len__(self) -> int:
        return int(self.ts.shape[0])

    def bar(self, index: int) -> Bar:
        """Materialise the bar at ``index`` (no bounds beyond numpy's)."""
        return Bar(
            self.symbol,
            int(self.ts[index]),
            float(self.open[index]),
            float(self.high[index]),
            float(self.low[index]),
            float(self.close[index]),
            float(self.volume[index]),
            RegimeLabel(int(self.regime[index])),
        )

    def _validate(self) -> None:
        n = len(self)
        columns = (self.open, self.high, self.low, self.close, self.volume, self.regime)
        if any(c.shape[0] != n for c in columns):
            raise DataValidationError(f"{self.symbol}: column length mismatch")
        if n == 0:
            raise DataValidationError(f"{self.symbol}: empty series")
        prices = np.stack((self.open, self.high, self.low, self.close))
        if not np.isfinite(prices).all() or (prices <= 0).any():
            raise DataValidationError(f"{self.symbol}: prices must be finite and positive")
        if not np.isfinite(self.volume).all() or (self.volume < 0).any():
            raise DataValidationError(f"{self.symbol}: volume must be finite and >= 0")
        if (self.high < np.maximum(self.open, self.close)).any() or (
            self.low > np.minimum(self.open, self.close)
        ).any():
            raise DataValidationError(f"{self.symbol}: OHLC bounds violated (high/low)")
        if n > 1 and (np.diff(self.ts) <= 0).any():
            raise DataValidationError(
                f"{self.symbol}: timestamps must be strictly increasing (no duplicates)"
            )
        if (self.regime < 0).any() or (self.regime > max(r.value for r in RegimeLabel)).any():
            raise DataValidationError(f"{self.symbol}: regime value out of range")

    def slice_time(self, start_ns: int, end_ns: int) -> SymbolSeries | None:
        """Bars with ``start_ns <= ts <= end_ns``; ``None`` if the slice is empty."""
        lo = int(np.searchsorted(self.ts, start_ns, side="left"))
        hi = int(np.searchsorted(self.ts, end_ns, side="right"))
        if hi <= lo:
            return None
        return SymbolSeries.create(
            self.symbol,
            self.ts[lo:hi],
            self.open[lo:hi],
            self.high[lo:hi],
            self.low[lo:hi],
            self.close[lo:hi],
            self.volume[lo:hi],
            self.regime[lo:hi],
        )


class MarketData:
    """A set of per-symbol series plus their merged, sorted event timeline."""

    def __init__(self, series: Iterable[SymbolSeries]) -> None:
        by_symbol: dict[str, SymbolSeries] = {}
        for s in series:
            if s.symbol in by_symbol:
                raise DataValidationError(f"duplicate symbol {s.symbol}")
            by_symbol[s.symbol] = s
        if not by_symbol:
            raise DataValidationError("market data has no symbols")
        self._series: dict[str, SymbolSeries] = dict(sorted(by_symbol.items()))
        merged = np.unique(np.concatenate([s.ts for s in self._series.values()]))
        merged.setflags(write=False)
        self._timeline: IntArray = merged

    @property
    def symbols(self) -> tuple[str, ...]:
        """Symbols in sorted order."""
        return tuple(self._series)

    @property
    def timeline(self) -> IntArray:
        """Sorted unique event timestamps across all symbols (read-only)."""
        return self._timeline

    def series(self, symbol: str) -> SymbolSeries:
        """Return the series for ``symbol`` or raise ``DataValidationError``."""
        try:
            return self._series[symbol]
        except KeyError as exc:
            raise DataValidationError(f"unknown symbol {symbol!r}") from exc

    def slice_time(self, start_ns: int, end_ns: int) -> MarketData:
        """Return the data restricted to ``start_ns <= ts <= end_ns`` (inclusive)."""
        parts = [s.slice_time(start_ns, end_ns) for s in self._series.values()]
        kept = [p for p in parts if p is not None]
        if not kept:
            raise DataValidationError(f"no data in [{start_ns}, {end_ns}]")
        return MarketData(kept)

    def with_regimes(self, label_ts_ns: Sequence[int], labels: Sequence[object]) -> MarketData:
        """Attach a regime series (as-of join, never forward-looking).

        Each bar receives the most recent label whose timestamp is ``<=`` the bar's own
        timestamp. Bars before the first label get ``REGIME_UNKNOWN``. ``labels`` may be
        ``RegimeLabel`` names (``"CRISIS"``), ints, or enum members.
        """
        if len(label_ts_ns) != len(labels):
            raise DataValidationError("regime timestamps and labels differ in length")
        if len(labels) == 0:
            raise DataValidationError("empty regime series")
        order = np.argsort(np.asarray(label_ts_ns, dtype=np.int64), kind="stable")
        sorted_ts = np.asarray(label_ts_ns, dtype=np.int64)[order]
        sorted_lab = np.asarray(
            [int(parse_regime(labels[int(i)])) for i in order], dtype=np.int8
        )
        out: list[SymbolSeries] = []
        for s in self._series.values():
            pos = np.searchsorted(sorted_ts, s.ts, side="right") - 1
            reg = np.where(pos >= 0, sorted_lab[np.maximum(pos, 0)], 0).astype(np.int8)
            out.append(
                SymbolSeries.create(
                    s.symbol, s.ts, s.open, s.high, s.low, s.close, s.volume, reg
                )
            )
        return MarketData(out)

    @classmethod
    def from_bars(cls, bars: Iterable[Bar]) -> MarketData:
        """Build from an iterable of ``Bar`` objects (any order; grouped and sorted here)."""
        grouped: dict[str, list[Bar]] = {}
        for b in bars:
            grouped.setdefault(b.symbol, []).append(b)
        series = []
        for symbol, items in grouped.items():
            items.sort(key=lambda b: b.timestamp_ns)
            series.append(
                SymbolSeries.create(
                    symbol,
                    [b.timestamp_ns for b in items],
                    [b.open for b in items],
                    [b.high for b in items],
                    [b.low for b in items],
                    [b.close for b in items],
                    [b.volume for b in items],
                    [int(b.regime) for b in items],
                )
            )
        return cls(series)

    @classmethod
    def from_frame(cls, frame: pd.DataFrame) -> MarketData:
        """Build from a frame produced by :func:`read_bar_frame`."""
        series = []
        for symbol, grp in frame.groupby("symbol", sort=True):
            grp = grp.sort_values("ts_ns", kind="stable")
            series.append(
                SymbolSeries.create(
                    str(symbol),
                    grp["ts_ns"].to_numpy(dtype=np.int64),
                    grp["open"].to_numpy(dtype=np.float64),
                    grp["high"].to_numpy(dtype=np.float64),
                    grp["low"].to_numpy(dtype=np.float64),
                    grp["close"].to_numpy(dtype=np.float64),
                    grp["volume"].to_numpy(dtype=np.float64),
                    grp["regime"].to_numpy(dtype=np.int8),
                )
            )
        return cls(series)


def _to_ns(frame: pd.DataFrame) -> pd.Series:
    """Convert the timestamp column of a raw frame into int64 UTC nanoseconds."""
    if "timestamp_ns" in frame.columns:
        try:
            return frame["timestamp_ns"].astype("int64")
        except (ValueError, TypeError) as exc:
            raise DataValidationError("timestamp_ns must be integer nanoseconds") from exc
    if "timestamp" not in frame.columns:
        raise DataValidationError("missing 'timestamp' (or 'timestamp_ns') column")
    raw = frame["timestamp"].astype(str)
    parsed = pd.to_datetime(raw, utc=False, errors="coerce", format="ISO8601")
    if parsed.isna().any():
        raise DataValidationError("unparseable timestamp value(s)")
    if parsed.dt.tz is None:
        raise DataValidationError("timestamps must carry a UTC offset (naive times rejected)")
    return parsed.dt.tz_convert("UTC").astype("datetime64[ns, UTC]").astype("int64")


def normalise_frame(raw: pd.DataFrame) -> pd.DataFrame:
    """Validate a raw bar frame and return columns ``ts_ns,symbol,OHLCV,regime``."""
    missing = [c for c in REQUIRED_COLUMNS if c not in raw.columns]
    if missing:
        raise DataValidationError(f"missing required column(s): {', '.join(missing)}")
    out = pd.DataFrame({"ts_ns": _to_ns(raw).to_numpy(dtype=np.int64)})
    out["symbol"] = raw["symbol"].astype(str).to_numpy()
    for col in ("open", "high", "low", "close", "volume"):
        try:
            out[col] = pd.to_numeric(raw[col], errors="raise").to_numpy(dtype=np.float64)
        except (ValueError, TypeError) as exc:
            raise DataValidationError(f"non-numeric values in column {col!r}") from exc
    if "regime" in raw.columns:
        out["regime"] = np.asarray(
            [int(parse_regime(None if pd.isna(v) else v)) for v in raw["regime"]], dtype=np.int8
        )
    else:
        out["regime"] = np.zeros(len(out), dtype=np.int8)
    return out


def _is_synthetic_csv(path: Path) -> bool:
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.startswith("#"):
                return False
            if line.strip().lower().startswith(SYNTHETIC_MARKER):
                return True
    return False


def read_bar_frame(path: str | Path) -> tuple[pd.DataFrame, bool]:
    """Read a CSV/Parquet bar file into a normalised frame.

    Returns ``(frame, is_synthetic)``; ``is_synthetic`` is true when the file carries the
    ``# synthetic-data`` marker written by ``testing.synthetic``.
    """
    p = Path(path)
    if not p.is_file():
        raise DataValidationError(f"dataset not found: {p}")
    suffix = p.suffix.lower()
    if suffix == ".parquet":
        try:
            raw = pd.read_parquet(p)
        except ImportError as exc:
            raise DataValidationError(
                "reading parquet requires the optional dependency 'pyarrow'"
            ) from exc
        return normalise_frame(raw), False
    if suffix not in {".csv", ".txt"}:
        raise DataValidationError(f"unsupported dataset format {suffix!r} (use .csv or .parquet)")
    raw = pd.read_csv(p, comment="#", dtype=str, keep_default_na=False)
    return normalise_frame(raw), _is_synthetic_csv(p)


def load_market_data(path: str | Path) -> MarketData:
    """Load a CSV/Parquet bar file into :class:`MarketData`."""
    frame, _ = read_bar_frame(path)
    return MarketData.from_frame(frame)
