"""``batch.py``: latest-per-symbol accumulation and single-global-regime semantics."""

from __future__ import annotations

from refdata_bridge.batch import BatchState
from refdata_bridge.mapping import ReferenceSnapshotData, RegimeLabelData


def snap(symbol: str, as_of_ns: int, *, stale: bool = False) -> ReferenceSnapshotData:
    return ReferenceSnapshotData(
        symbol=symbol,
        mid_price_nanos=100_000_000_000,
        adv_30d_nanos=1_000_000_000_000,
        as_of_ns=as_of_ns,
        is_stale=stale,
    )


def regime(ts_ns: int, label: str = "TRENDING_BULL") -> RegimeLabelData:
    return RegimeLabelData(label=label, confidence=0.9, timestamp_ns=ts_ns)


def test_build_batch_includes_new_symbol() -> None:
    state = BatchState()
    state.record_snapshot(snap("AAPL", 100))
    batch = state.build_batch(max_snapshots=10)
    assert [s.symbol for s in batch.snapshots] == ["AAPL"]


def test_out_of_order_snapshot_does_not_overwrite_newer() -> None:
    state = BatchState()
    state.record_snapshot(snap("AAPL", 200))
    state.record_snapshot(snap("AAPL", 100))  # older, arrives late
    batch = state.build_batch(max_snapshots=10)
    assert batch.snapshots[0].as_of_ns == 200


def test_unchanged_symbol_is_not_resent_after_being_applied() -> None:
    state = BatchState()
    state.record_snapshot(snap("AAPL", 100))
    state.mark_applied(["AAPL"], regime_applied=False, regime_ts=None)
    batch = state.build_batch(max_snapshots=10)
    assert batch.snapshots == []


def test_rejected_symbol_is_resent_next_cycle() -> None:
    state = BatchState()
    state.record_snapshot(snap("AAPL", 100))
    state.mark_applied([], regime_applied=False, regime_ts=None)  # Aegis rejected it
    batch = state.build_batch(max_snapshots=10)
    assert [s.symbol for s in batch.snapshots] == ["AAPL"]


def test_newer_snapshot_after_applied_is_sent_again() -> None:
    state = BatchState()
    state.record_snapshot(snap("AAPL", 100))
    state.mark_applied(["AAPL"], regime_applied=False, regime_ts=None)
    state.record_snapshot(snap("AAPL", 200))
    batch = state.build_batch(max_snapshots=10)
    assert [s.as_of_ns for s in batch.snapshots] == [200]


def test_batch_respects_max_snapshots_oldest_first() -> None:
    state = BatchState()
    for i, sym in enumerate(["A", "B", "C"]):
        state.record_snapshot(snap(sym, 100 + i))
    batch = state.build_batch(max_snapshots=2)
    assert [s.symbol for s in batch.snapshots] == ["A", "B"]


def test_regime_is_included_until_applied() -> None:
    state = BatchState()
    state.record_regime(regime(100))
    batch = state.build_batch(max_snapshots=10)
    assert batch.regime is not None
    assert batch.regime.timestamp_ns == 100

    state.mark_applied([], regime_applied=True, regime_ts=100)
    batch = state.build_batch(max_snapshots=10)
    assert batch.regime is None


def test_older_regime_never_overwrites_newer_across_symbols() -> None:
    """RegimeLabelPacket has no symbol field (market_snapshot.proto): only ONE
    global regime is tracked. A late/older message from any symbol must not
    clobber the most recently timestamped one already recorded."""
    state = BatchState()
    state.record_regime(regime(200, label="CRISIS"))
    state.record_regime(regime(100, label="TRENDING_BULL"))  # older, different "symbol" origin
    batch = state.build_batch(max_snapshots=10)
    assert batch.regime is not None
    assert batch.regime.label == "CRISIS"


def test_regime_rejected_by_aegis_is_resent() -> None:
    state = BatchState()
    state.record_regime(regime(100))
    state.mark_applied([], regime_applied=False, regime_ts=None)  # rejected
    batch = state.build_batch(max_snapshots=10)
    assert batch.regime is not None


def test_known_symbols_count() -> None:
    state = BatchState()
    state.record_snapshot(snap("AAPL", 100))
    state.record_snapshot(snap("MSFT", 100))
    assert state.known_symbols() == 2
