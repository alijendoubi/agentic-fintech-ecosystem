"""Aegis ``HeldSignal`` -> contract ``Hold`` JSON (contract section 5), plus the decision book.

int64 values are decimal strings, enums are names, money/quantity are int64 nanos: exactly what
the terminal's zod schema (hitl-interface/src/lib/signals/schema.ts) accepts.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .attest import Attestation

NANOS = Decimal(10) ** 9
FINAL_STATUSES = frozenset({"APPROVED", "REJECTED", "EXPIRED", "RELEASE_DENIED"})


def _enum_name(enum: Any, value: int) -> str:
    try:
        return str(enum.Name(value))
    except ValueError:
        return str(value)


def required_approvals(
    signal: Any, *, quantity_threshold: Decimal, notional_threshold_usd: Decimal | None
) -> int:
    """Four-eyes (contract 6.5): 2 when quantity >= threshold (0 = every hold) or, for a limit
    order, quantity * limit price >= the notional threshold. Integer/Decimal arithmetic only."""
    quantity = Decimal(signal.quantity_nanos) / NANOS
    if quantity >= quantity_threshold:
        return 2
    if notional_threshold_usd is None or signal.price_limit_nanos <= 0:
        return 1
    notional = quantity * Decimal(signal.price_limit_nanos) / NANOS
    return 2 if notional >= notional_threshold_usd else 1


def hold_json(
    held: Any, pb: Any, *, status: str, required: int, approvals: list[dict[str, Any]]
) -> dict[str, Any]:
    d, s = held.decision, held.signal
    aegis, ts, snap = pb["aegis_pb2"], pb["trade_signal_pb2"], pb["market_snapshot_pb2"]
    return {
        "holdId": d.hold_id,
        "signalId": s.signal_id,
        "symbol": s.symbol,
        "createdAtNs": str(s.created_at_ns),
        "side": _enum_name(ts.SignalSide, s.side),
        "quantityNanos": str(s.quantity_nanos),
        "priceLimitNanos": str(s.price_limit_nanos),
        "estimatedSpreadCostNanos": str(s.estimated_spread_cost_nanos),
        "estimatedMarketImpactNanos": str(s.estimated_market_impact_nanos),
        "estimatedVenueFeesNanos": str(s.estimated_venue_fees_nanos),
        "estimatedTotalCostNanos": str(s.estimated_total_cost_nanos),
        "omega": s.omega,
        "expectedValue": s.expected_value,
        "pSuccess": s.p_success,
        "pFailure": s.p_failure,
        "rewardEstimate": s.reward_estimate,
        "riskEstimate": s.risk_estimate,
        "regime": _enum_name(snap.RegimeLabel, s.regime),
        "regimeConfidence": s.regime_confidence,
        "strategyId": s.strategy_id,
        "debateSummary": s.debate_summary,
        "validUntilNs": str(s.valid_until_ns),
        "holdExpiresAtNs": str(d.hold_expires_at_ns),
        "heldReasons": [_enum_name(aegis.ReasonCode, r) for r in d.reasons],
        "controls": [
            {
                "controlId": c.control_id,
                "isHard": c.is_hard,
                "passed": c.passed,
                "reason": _enum_name(aegis.ReasonCode, c.reason),
                "threshold": c.threshold,
                "observed": c.observed,
                "detail": c.detail,
            }
            for c in d.results
        ],
        "hitlStatus": status,
        "requiredApprovals": required,
        "approvals": approvals,
    }


@dataclass
class FinalRecord:
    hold: dict[str, Any]


@dataclass(frozen=True)
class PendingApproval:
    """The first of two required approvals (owner decision 2026-09-29, DECISIONS row 4). Kept
    until the second distinct approver decides, a rejection, or ``expires_at_ns`` (the hold's
    own expiry). ``attestation`` is the Aegis ``Authorization`` signed when it was given (None
    when no attestor key is configured, dev only)."""

    sub: str
    reason: str
    decided_at_ns: int
    expires_at_ns: int
    attestation: Attestation | None

    def json(self) -> dict[str, Any]:
        return {
            "approverSub": self.sub,
            "decision": "APPROVE",
            "reason": self.reason,
            "decidedAtNs": str(self.decided_at_ns),
        }


@dataclass
class DecisionBook:
    """In-memory, like Aegis's own hold store: a restart forgets finished holds, idempotency
    keys and pending first approvals (Aegis has dropped a resolved hold by then, so a replay
    cannot decide twice; a forgotten first approval must simply be given again, and the
    audit log still records it)."""

    finals: dict[str, FinalRecord] = field(default_factory=dict)
    pending: dict[str, PendingApproval] = field(default_factory=dict)
    replies: dict[str, tuple[int, dict[str, Any]]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
