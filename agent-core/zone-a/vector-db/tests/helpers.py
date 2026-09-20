from __future__ import annotations

from afe_vector_memory import MemoryKind, MemoryRecord, Outcome

NOW_MS = 1_800_000_000_000
DAY_MS = 86_400_000


class Clock:
    def __init__(self, now_ms: int = NOW_MS) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


def make_record(
    record_id: str = "r1",
    *,
    text: str = "breakout above resistance failed, reversed on volume",
    kind: MemoryKind = MemoryKind.DEBATE_OUTCOME,
    symbol: str = "AAPL",
    regime: str = "TRENDING_BULL",
    ts_ms: int = NOW_MS,
    outcome: Outcome = Outcome.LOSS,
    signal_id: str = "sig-1",
) -> MemoryRecord:
    return MemoryRecord(record_id, kind, text, symbol, regime, ts_ms, outcome, signal_id)
