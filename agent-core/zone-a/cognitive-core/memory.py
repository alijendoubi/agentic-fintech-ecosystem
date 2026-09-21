"""Optional memory hooks for the runner (ALI-39 wires a Chroma store in behind them).

cognitive-core defines the contracts as structural Protocols in plain types, so it has no
import-time or runtime dependency on `afe_vector_memory` or chromadb; any object with these
methods works (`afe_vector_memory.AsyncMemoryAdapter` does).

Contract, fail closed:

* `PrecedentProvider.recall` returns a sequence. Empty means "the store answered and found
  nothing similar". ANY exception means "unknown"; it is never treated as "no precedent".
* `ReflectionWriter` calls are best effort and happen AFTER the signal decision; a failure is
  logged and counted but can never change or block a signal.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import structlog

from .models import (
    MAX_PRECEDENT_CHARS,
    MAX_PRECEDENTS,
    DebateState,
    MarketContext,
    PrecedentStatus,
    RegimeLabel,
    TradeSignal,
)

log = structlog.get_logger(__name__)

NS_PER_MS = 1_000_000
FLOW_THRESHOLD = 0.2
STRETCH_Z = 1.5
_LOG_ERROR_CHARS = 200


class PrecedentLike(Protocol):
    """One recalled record (read-only attributes)."""

    @property
    def text(self) -> str: ...
    @property
    def outcome(self) -> str: ...
    @property
    def regime(self) -> str: ...
    @property
    def symbol(self) -> str: ...
    @property
    def distance(self) -> float: ...


class PrecedentProvider(Protocol):
    async def recall(
        self, *, symbol: str, regime: str, query_text: str, limit: int | None = None
    ) -> Sequence[PrecedentLike]: ...


class ReflectionWriter(Protocol):
    async def write_debate(
        self, *, signal_id: str, symbol: str, regime: str, ts_ms: int, outcome: str, text: str
    ) -> None: ...

    async def write_reflection(
        self, *, signal_id: str, symbol: str, regime: str, ts_ms: int, outcome: str, text: str
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class RecallResult:
    status: PrecedentStatus
    precedents: tuple[str, ...] = ()


def build_recall_query(context: MarketContext, regime: RegimeLabel) -> str:
    """Deterministic text describing the current situation, for similarity search."""
    if context.ofi > FLOW_THRESHOLD:
        flow = "buy_pressure"
    elif context.ofi < -FLOW_THRESHOLD:
        flow = "sell_pressure"
    else:
        flow = "flat_flow"
    if context.z_score > STRETCH_Z:
        stretch = "stretched_up"
    elif context.z_score < -STRETCH_Z:
        stretch = "stretched_down"
    else:
        stretch = "in_range"
    return f"{context.symbol} {regime.value} {flow} {stretch}"


def _one_line(precedent: PrecedentLike) -> str:
    text = " ".join(str(precedent.text).split())
    line = (
        f"[{precedent.outcome}] regime={precedent.regime} symbol={precedent.symbol} "
        f"distance={float(precedent.distance):.2f}: {text}"
    )
    return line[:MAX_PRECEDENT_CHARS]


async def recall_precedents(
    provider: PrecedentProvider | None,
    context: MarketContext,
    regime: RegimeLabel,
    *,
    top_k: int,
    timeout_s: float,
) -> RecallResult:
    """Ask the provider for precedents; map any failure to status "unavailable"."""
    if provider is None:
        return RecallResult("none_configured")
    try:
        found = await asyncio.wait_for(
            provider.recall(
                symbol=context.symbol,
                regime=regime.value,
                query_text=build_recall_query(context, regime),
                limit=min(top_k, MAX_PRECEDENTS),
            ),
            timeout=timeout_s,
        )
        lines = tuple(_one_line(p) for p in list(found)[:MAX_PRECEDENTS])
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - provider is foreign code: any failure = unknown
        log.warning(
            "precedent_recall_failed",
            error_type=type(exc).__name__,
            error=str(exc)[:_LOG_ERROR_CHARS],
            symbol=context.symbol,
            outcome="unavailable",
        )
        return RecallResult("unavailable")
    return RecallResult("ok", lines)


def apply_recall(state: DebateState, recall: RecallResult) -> DebateState:
    return state.model_copy(
        update={"precedents": recall.precedents, "precedents_status": recall.status}
    )


def debate_record_args(signal: TradeSignal) -> dict[str, str | int]:
    """Keyword arguments for `ReflectionWriter.write_debate` from a finished signal."""
    outcome = "abstain" if signal.status.value == "SIGNAL_ABSTAIN" else "pending"
    return {
        "signal_id": signal.signal_id,
        "symbol": signal.symbol,
        "regime": signal.regime.value,
        "ts_ms": signal.created_at_ns // NS_PER_MS,
        "outcome": outcome,
        "text": f"{signal.side.value} omega={signal.omega:.2f}: {signal.debate_summary}",
    }


async def write_debate_record(writer: ReflectionWriter | None, signal: TradeSignal) -> bool:
    """Best-effort persistence of a debate outcome. True only if it was written."""
    if writer is None:
        return False
    try:
        await writer.write_debate(**debate_record_args(signal))  # type: ignore[arg-type]
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - must never affect the signal path
        log.warning(
            "debate_record_failed",
            error_type=type(exc).__name__,
            error=str(exc)[:_LOG_ERROR_CHARS],
            signal_id=signal.signal_id,
        )
        return False
    return True


async def write_reflection_record(
    writer: ReflectionWriter | None,
    signal: TradeSignal,
    *,
    outcome: str,
    text: str,
    ts_ms: int,
) -> bool:
    """Best-effort persistence of a post-trade reflection (called by the offline path)."""
    if writer is None:
        return False
    try:
        await writer.write_reflection(
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            regime=signal.regime.value,
            ts_ms=ts_ms,
            outcome=outcome,
            text=text,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - offline path never raises into callers
        log.warning(
            "reflection_record_failed",
            error_type=type(exc).__name__,
            error=str(exc)[:_LOG_ERROR_CHARS],
            signal_id=signal.signal_id,
        )
        return False
    return True
