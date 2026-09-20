"""DebateState -> TradeSignal conversion with the fail-closed signal invariants.

A signal is only actionable (SIGNAL_PENDING) when EVERY condition holds; any failure
degrades to SIGNAL_ABSTAIN with side SIDE_UNKNOWN and quantity 0. Explicit checks are
used everywhere (never `assert`, which `python -O` strips).
"""

from __future__ import annotations

import math
import time
import uuid

import structlog

from .config import CognitiveSettings
from .models import (
    DebateState,
    JudgeVerdict,
    SignalSide,
    SignalStatus,
    TradeSignal,
    truncate_summary,
)

log = structlog.get_logger(__name__)

NS_PER_MS = 1_000_000
NO_SUMMARY = "no debate summary available"


def abstain_reasons(
    state: DebateState,
    verdict: JudgeVerdict,
    settings: CognitiveSettings,
    quantity: float,
) -> list[str]:
    """Return every reason this debate must NOT produce an actionable signal."""
    reasons: list[str] = []
    blue, red = state.blue_thesis, state.red_challenge
    if blue is None or red is None:
        reasons.append("missing_upstream_node")
    elif blue.forced_completion or red.forced_completion:
        reasons.append("upstream_forced_completion")
    if verdict.defaulted_abstain:
        reasons.append("judge_defaulted_abstain")
    if verdict.omega < settings.omega_threshold:
        reasons.append("omega_below_threshold")
    if verdict.side == SignalSide.SIDE_UNKNOWN:
        reasons.append("side_unknown")
    if not math.isfinite(quantity) or quantity <= 0.0:
        reasons.append("quantity_not_positive")
    if verdict.expected_value <= 0.0:
        reasons.append("non_positive_expected_value")
    return reasons


def _build(
    state: DebateState,
    verdict: JudgeVerdict,
    settings: CognitiveSettings,
    *,
    actionable: bool,
    quantity: float,
    now_ns: int,
) -> TradeSignal:
    summary = state.debate_summary or NO_SUMMARY
    return TradeSignal(
        signal_id=str(uuid.uuid4()),
        symbol=state.market_context.symbol,
        created_at_ns=now_ns,
        side=verdict.side if actionable else SignalSide.SIDE_UNKNOWN,
        quantity=quantity if actionable else 0.0,
        omega=verdict.omega,
        expected_value=verdict.expected_value,
        p_success=verdict.p_success,
        p_failure=verdict.p_failure,
        reward_estimate=verdict.reward_estimate,
        risk_estimate=verdict.risk_estimate,
        regime=state.regime,
        regime_confidence=state.regime_confidence,
        debate_summary=truncate_summary(summary, settings.compression_max_tokens),
        valid_until_ns=now_ns + settings.signal_ttl_ms * NS_PER_MS,
        status=SignalStatus.SIGNAL_PENDING if actionable else SignalStatus.SIGNAL_ABSTAIN,
    )


def to_trade_signal(
    state: DebateState,
    *,
    settings: CognitiveSettings,
    quantity: float = 0.0,
    now_ns: int | None = None,
) -> TradeSignal:
    """Convert a completed DebateState into the proto-shaped TradeSignal.

    `quantity` is the share quantity chosen by a position sizer. The cognitive core has
    no portfolio or risk state, so it never invents one: the default 0.0 always abstains.
    """
    created = time.time_ns() if now_ns is None else now_ns
    verdict = state.judge_verdict
    if verdict is None:
        log.error("signal_abstain", reasons=["missing_judge_verdict"], symbol=state.market_context.symbol)
        return _build(
            state, JudgeVerdict.default_abstain(), settings, actionable=False, quantity=0.0, now_ns=created
        )
    reasons = abstain_reasons(state, verdict, settings, quantity)
    if reasons:
        log.info("signal_abstain", reasons=reasons, symbol=state.market_context.symbol)
    return _build(
        state, verdict, settings, actionable=not reasons, quantity=quantity, now_ns=created
    )


def abstain_signal(
    state: DebateState,
    *,
    settings: CognitiveSettings,
    summary: str,
    now_ns: int | None = None,
) -> TradeSignal:
    """Explicit fail-closed signal for a debate that could not complete."""
    created = time.time_ns() if now_ns is None else now_ns
    aborted = state.model_copy(update={"debate_summary": summary})
    return _build(
        aborted, JudgeVerdict.default_abstain(), settings, actionable=False, quantity=0.0, now_ns=created
    )
