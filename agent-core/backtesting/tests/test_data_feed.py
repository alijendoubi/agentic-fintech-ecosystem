"""Data loading, validation and point-in-time (no look-ahead) tests. Data is SYNTHETIC."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from backtesting.data import MarketData, SymbolSeries, load_market_data, read_bar_frame
from backtesting.feed import PointInTimeFeed
from backtesting.models import (
    Bar,
    ConfigError,
    DataValidationError,
    LookaheadError,
    OrderIntent,
    RegimeLabel,
    Side,
    parse_regime,
)
from backtesting.testing.synthetic import generate_market_data, write_synthetic_csv


def _data(n: int = 10) -> MarketData:
    return generate_market_data(["AAA", "BBB"], n, seed=1)


def test_regime_names_match_proto() -> None:
    proto = Path(__file__).resolve().parents[2] / "shared" / "proto" / "market_snapshot.proto"
    text = proto.read_text(encoding="utf-8")
    for label in RegimeLabel:
        assert f"{label.name} = {label.value};" in text


def test_parse_regime_variants() -> None:
    assert parse_regime("crisis") is RegimeLabel.CRISIS
    assert parse_regime(3) is RegimeLabel.HIGH_VOL_CHOP
    assert parse_regime("") is RegimeLabel.REGIME_UNKNOWN
    assert parse_regime(None) is RegimeLabel.REGIME_UNKNOWN
    assert parse_regime("2") is RegimeLabel.TRENDING_BEAR
    for bad in ("BOGUS", 99, True):
        with pytest.raises(DataValidationError):
            parse_regime(bad)


def test_order_intent_validation() -> None:
    assert OrderIntent.of("A", Side.BUY, 5).quantity == Decimal("5")
    with pytest.raises(ConfigError):
        OrderIntent.of("A", Side.BUY, 0)
    with pytest.raises(ConfigError):
        OrderIntent.of("A", Side.BUY, 1, omega=1.5)
    with pytest.raises(ConfigError):
        OrderIntent.of("A", Side.BUY, 1, price_limit=-1)
    with pytest.raises(TypeError):
        OrderIntent("A", Side.BUY, 1)  # type: ignore[arg-type]


def test_series_validation_rejects_bad_data() -> None:
    ts = [1, 2, 3]
    ok = [10.0, 10.0, 10.0]
    with pytest.raises(DataValidationError):  # duplicate timestamps
        SymbolSeries.create("X", [1, 1, 2], ok, ok, ok, ok, ok)
    with pytest.raises(DataValidationError):  # NaN price
        SymbolSeries.create("X", ts, [1.0, float("nan"), 1.0], ok, ok, ok, ok)
    with pytest.raises(DataValidationError):  # high below close
        SymbolSeries.create("X", ts, ok, [9.0] * 3, [9.0] * 3, ok, ok)
    with pytest.raises(DataValidationError):  # negative volume
        SymbolSeries.create("X", ts, ok, ok, ok, ok, [-1.0, 1.0, 1.0])


def test_arrays_are_read_only() -> None:
    s = _data().series("AAA")
    with pytest.raises(ValueError, match="read-only"):
        s.close[0] = 1.0


def test_csv_roundtrip_and_synthetic_marker(tmp_path: Path) -> None:
    data = _data()
    path = tmp_path / "bars.csv"
    write_synthetic_csv(path, data)
    frame, is_synth = read_bar_frame(path)
    assert is_synth is True
    loaded = load_market_data(path)
    assert loaded.symbols == data.symbols
    np.testing.assert_allclose(loaded.series("AAA").close, data.series("AAA").close)


def test_csv_iso_timestamps_and_naive_rejected(tmp_path: Path) -> None:
    good = tmp_path / "good.csv"
    good.write_text(
        "timestamp,symbol,open,high,low,close,volume,regime\n"
        "2024-03-01T14:31:00Z,ZZZ,10,11,9,10.5,100,TRENDING_BULL\n"
        "2024-03-01T14:32:00+00:00,ZZZ,10.5,11,10,10.6,120,\n",
        encoding="utf-8",
    )
    data = load_market_data(good)
    s = data.series("ZZZ")
    assert len(s) == 2 and int(s.regime[0]) == RegimeLabel.TRENDING_BULL
    assert int(s.ts[1] - s.ts[0]) == 60 * 10**9
    naive = tmp_path / "naive.csv"
    naive.write_text(
        "timestamp,symbol,open,high,low,close,volume\n2024-03-01T14:31:00,Z,1,1,1,1,1\n",
        encoding="utf-8",
    )
    with pytest.raises(DataValidationError, match="UTC offset"):
        load_market_data(naive)


def test_csv_missing_column_and_missing_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("timestamp_ns,symbol,close\n1,A,1\n", encoding="utf-8")
    with pytest.raises(DataValidationError, match="missing required"):
        load_market_data(bad)
    with pytest.raises(DataValidationError, match="not found"):
        load_market_data(tmp_path / "nope.csv")


def test_slice_and_regime_asof_join() -> None:
    data = _data(10)
    tl = data.timeline
    sliced = data.slice_time(int(tl[2]), int(tl[5]))
    assert len(sliced.timeline) == 4
    labelled = data.with_regimes([int(tl[3]), int(tl[7])], ["CRISIS", RegimeLabel.LOW_VOL_CHOP])
    reg = labelled.series("AAA").regime
    assert list(reg[:3]) == [0, 0, 0]  # before first label: unknown, not back-filled
    assert list(reg[3:7]) == [5, 5, 5, 5]
    assert list(reg[7:]) == [4, 4, 4]
    with pytest.raises(DataValidationError):
        data.slice_time(0, 1)


def test_feed_only_exposes_past_and_present() -> None:
    data = _data(8)
    seen = 0
    for ev in PointInTimeFeed(data).events():
        seen += 1
        hist = ev.view.history("AAA")
        assert len(hist) == seen
        assert hist.closes().shape[0] == seen
        assert hist.latest is not None and hist.latest.timestamp_ns == ev.ts_ns
        assert int(hist.timestamps()[-1]) == ev.ts_ns
    assert seen == 8


def test_peeking_into_the_future_raises() -> None:
    data = _data(8)
    events = PointInTimeFeed(data).events()
    ev = next(events)  # only one bar visible
    hist = ev.view.history("AAA")
    with pytest.raises(LookaheadError):
        hist[1]
    with pytest.raises(LookaheadError):
        hist[0:5]
    with pytest.raises(LookaheadError):
        hist.at(int(data.timeline[3]))
    with pytest.raises(LookaheadError):
        ev.view.at("AAA", int(data.timeline[-1]))
    assert hist[0] is not None and hist[-1] == hist[0]


def test_returned_arrays_cannot_reach_future_data() -> None:
    data = _data(8)
    ev = next(PointInTimeFeed(data).events())
    closes = ev.view.history("AAA").closes()
    assert closes.base is None  # a copy: no route back to the full array
    closes[0] = -1.0
    assert data.series("AAA").close[0] > 0  # mutation does not leak into the store


def test_symbol_not_started_has_no_latest() -> None:
    one = [1.0] * 3
    a = SymbolSeries.create("A", [1, 2, 3], one, one, one, one, one)
    b = SymbolSeries.create("B", [3], [1.0], [1.0], [1.0], [1.0], [1.0])
    events = list(PointInTimeFeed(MarketData([a, b])).events())
    assert events[0].view.latest("B") is None
    assert events[2].view.latest("B") == Bar("B", 3, 1.0, 1.0, 1.0, 1.0, 1.0)
    with pytest.raises(DataValidationError):
        events[0].view.history("NOPE")
