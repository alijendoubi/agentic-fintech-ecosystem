"""Pydantic state models for the Blue/Red/Judge/Reflector debate graph.

`SignalSide` / `SignalStatus` mirror `shared/proto/trade_signal.proto` exactly —
keep both in sync when either changes.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator

MAX_SUMMARY_TOKENS = 200


class SignalSide(str, Enum):
    SIDE_UNKNOWN = "SIDE_UNKNOWN"
    BUY = "BUY"
    SELL = "SELL"
    SELL_SHORT = "SELL_SHORT"


class SignalStatus(str, Enum):
    SIGNAL_PENDING = "SIGNAL_PENDING"
    SIGNAL_APPROVED = "SIGNAL_APPROVED"
    SIGNAL_REJECTED_HARD_BLOCK = "SIGNAL_REJECTED_HARD_BLOCK"
    SIGNAL_SOFT_BLOCK_PENDING = "SIGNAL_SOFT_BLOCK_PENDING"
    SIGNAL_ABSTAIN = "SIGNAL_ABSTAIN"
    SIGNAL_EXPIRED = "SIGNAL_EXPIRED"


class RegimeLabel(str, Enum):
    TRENDING_BULL = "TRENDING_BULL"
    TRENDING_BEAR = "TRENDING_BEAR"
    HIGH_VOL_CHOP = "HIGH_VOL_CHOP"
    LOW_VOL_CHOP = "LOW_VOL_CHOP"
    CRISIS = "CRISIS"


class MarketContext(BaseModel):
    symbol: str
    mid_price: float
    z_score: float
    mad_score: float
    ofi: float = Field(description="Order flow imbalance, range [-1, 1]")
    realized_vol: float
    adv_30d: float

    @field_validator("ofi")
    @classmethod
    def _ofi_bounded(cls, v: float) -> float:
        if not -1.0 <= v <= 1.0:
            raise ValueError("ofi must be in [-1, 1]")
        return v


class BlueThesis(BaseModel):
    side: SignalSide
    rationale: str
    key_factors: list[str] = Field(default_factory=list)
    forced_completion: bool = False


class RedChallenge(BaseModel):
    counter_factors: list[str] = Field(default_factory=list)
    failure_patterns: list[str] = Field(default_factory=list)
    forced_completion: bool = False


class JudgeVerdict(BaseModel):
    side: SignalSide
    omega: float = Field(description="Judge confidence score")
    p_success: float
    p_failure: float
    reward_estimate: float
    risk_estimate: float
    defaulted_abstain: bool = False

    @field_validator("omega", "p_success", "p_failure")
    @classmethod
    def _unit_interval(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("value must be in [0, 1]")
        return v


class TradeSignal(BaseModel):
    """Mirrors `shared/proto/trade_signal.proto` TradeSignal message."""

    signal_id: str
    symbol: str
    side: SignalSide
    quantity: float
    omega: float
    expected_value: float
    p_success: float
    p_failure: float
    reward_estimate: float
    risk_estimate: float
    regime: RegimeLabel
    regime_confidence: float
    debate_summary: str
    price_limit: float = 0.0
    status: SignalStatus = SignalStatus.SIGNAL_PENDING

    @field_validator("debate_summary")
    @classmethod
    def _summary_token_cap(cls, v: str) -> str:
        # Approximation: ~1 token per 4 chars, matching the 200-token compression cap.
        if len(v) > MAX_SUMMARY_TOKENS * 4:
            return v[: MAX_SUMMARY_TOKENS * 4]
        return v


class DebateState(BaseModel):
    """Mutable state threaded through the LangGraph nodes."""

    market_context: MarketContext
    regime: RegimeLabel
    regime_confidence: float
    blue_thesis: BlueThesis | None = None
    red_challenge: RedChallenge | None = None
    judge_verdict: JudgeVerdict | None = None
    debate_summary: str | None = None


class RubricChangeProposal(BaseModel):
    """Reflector output. Draft only — has no field that can cause live effect.

    Per docs/processes/sharp-promotion.md this must pass Compliance -> Legal ->
    Backtesting -> Risk -> Canary before any deploy. Nothing in this module
    writes to live config.
    """

    proposal_id: str
    trigger_signal_id: str
    observed_underperformance: str
    proposed_change: str
    rationale: str
