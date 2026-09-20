"""Pydantic state models for the Blue/Red/Judge/Reflector debate graph.

`SignalSide`, `SignalStatus` and `RegimeLabel` mirror `shared/proto/trade_signal.proto`
and `shared/proto/market_snapshot.proto` (names AND numeric values are checked
against the compiled protos in tests/test_proto_parity.py). All models are
frozen; LLM-facing models are strict and reject unknown fields, so a malformed
model reply fails validation instead of being coerced.
"""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_SUMMARY_TOKENS = 200
CHARS_PER_TOKEN = 4  # approximation shared with the compression node
PROB_SUM_TOLERANCE = 1e-6

UnitFloat = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
NonNegFloat = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
ShortText = Annotated[str, Field(max_length=4000)]
FactorList = Annotated[tuple[Annotated[str, Field(max_length=500)], ...], Field(max_length=25)]


class SignalSide(StrEnum):
    SIDE_UNKNOWN = "SIDE_UNKNOWN"
    BUY = "BUY"
    SELL = "SELL"
    SELL_SHORT = "SELL_SHORT"


class SignalStatus(StrEnum):
    SIGNAL_PENDING = "SIGNAL_PENDING"
    SIGNAL_APPROVED = "SIGNAL_APPROVED"
    SIGNAL_REJECTED_HARD_BLOCK = "SIGNAL_REJECTED_HARD_BLOCK"
    SIGNAL_SOFT_BLOCK_PENDING = "SIGNAL_SOFT_BLOCK_PENDING"
    SIGNAL_ABSTAIN = "SIGNAL_ABSTAIN"
    SIGNAL_EXPIRED = "SIGNAL_EXPIRED"


class RegimeLabel(StrEnum):
    REGIME_UNKNOWN = "REGIME_UNKNOWN"  # proto default (0); the detector publishes it
    TRENDING_BULL = "TRENDING_BULL"
    TRENDING_BEAR = "TRENDING_BEAR"
    HIGH_VOL_CHOP = "HIGH_VOL_CHOP"
    LOW_VOL_CHOP = "LOW_VOL_CHOP"
    CRISIS = "CRISIS"


# Statuses that may lead to an order: they need a real side and a positive quantity.
ACTIONABLE_STATUSES = frozenset(
    {
        SignalStatus.SIGNAL_PENDING,
        SignalStatus.SIGNAL_APPROVED,
        SignalStatus.SIGNAL_SOFT_BLOCK_PENDING,
    }
)


def truncate_summary(text: str, max_tokens: int = MAX_SUMMARY_TOKENS) -> str:
    """Hard-cap a summary at ~`max_tokens` tokens (4 chars per token)."""
    return text[: max_tokens * CHARS_PER_TOKEN]


class MarketContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(min_length=1, max_length=32)
    mid_price: Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
    z_score: FiniteFloat
    mad_score: FiniteFloat
    ofi: Annotated[float, Field(ge=-1.0, le=1.0, allow_inf_nan=False)] = Field(
        description="Order flow imbalance, range [-1, 1]"
    )
    realized_vol: NonNegFloat
    adv_30d: NonNegFloat


class BlueThesis(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)

    side: SignalSide
    rationale: ShortText
    key_factors: FactorList = ()
    forced_completion: bool = False


class RedChallenge(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)

    counter_factors: FactorList = ()
    failure_patterns: FactorList = ()
    forced_completion: bool = False


class JudgeVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, allow_inf_nan=False)

    side: SignalSide
    omega: UnitFloat = Field(description="Judge confidence score in [0, 1]")
    p_success: UnitFloat
    p_failure: UnitFloat
    reward_estimate: NonNegFloat
    risk_estimate: NonNegFloat
    defaulted_abstain: bool = False

    @model_validator(mode="after")
    def _probabilities_sum_to_one(self) -> Self:
        if abs(self.p_success + self.p_failure - 1.0) > PROB_SUM_TOLERANCE:
            raise ValueError("p_success + p_failure must equal 1 (tolerance 1e-6)")
        return self

    @property
    def expected_value(self) -> float:
        """E[V] = p_success * reward - p_failure * risk (never reward - risk)."""
        return self.p_success * self.reward_estimate - self.p_failure * self.risk_estimate

    @classmethod
    def default_abstain(cls) -> Self:
        """Fail-closed verdict used whenever the Judge cannot be trusted."""
        return cls(
            side=SignalSide.SIDE_UNKNOWN,
            omega=0.0,
            p_success=0.0,
            p_failure=1.0,
            reward_estimate=0.0,
            risk_estimate=0.0,
            defaulted_abstain=True,
        )


class TradeSignal(BaseModel):
    """Mirrors the `TradeSignal` message in `shared/proto/trade_signal.proto`.

    Invariant enforced here so an unusable signal cannot be constructed: any
    actionable status needs a real side, quantity > 0 and a positive validity window.
    Cost fields are 0.0 when Zone A has no cost model (Aegis must not treat 0.0 as a
    quote).
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    signal_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1, max_length=32)
    created_at_ns: int = Field(ge=0)
    side: SignalSide
    quantity: NonNegFloat
    omega: UnitFloat
    expected_value: FiniteFloat
    p_success: UnitFloat
    p_failure: UnitFloat
    reward_estimate: NonNegFloat
    risk_estimate: NonNegFloat
    estimated_spread_cost: NonNegFloat = 0.0
    estimated_market_impact: NonNegFloat = 0.0
    estimated_venue_fees: NonNegFloat = 0.0
    estimated_total_cost: NonNegFloat = 0.0
    regime: RegimeLabel
    regime_confidence: UnitFloat
    debate_summary: str
    price_limit: NonNegFloat = 0.0  # 0 = market order
    valid_until_ns: int = Field(ge=0)
    status: SignalStatus = SignalStatus.SIGNAL_PENDING

    @field_validator("debate_summary")
    @classmethod
    def _summary_token_cap(cls, v: str) -> str:
        return truncate_summary(v)

    @model_validator(mode="after")
    def _signal_invariants(self) -> Self:
        if abs(self.p_success + self.p_failure - 1.0) > PROB_SUM_TOLERANCE:
            raise ValueError("p_success + p_failure must equal 1 (tolerance 1e-6)")
        if self.status in ACTIONABLE_STATUSES:
            if self.side == SignalSide.SIDE_UNKNOWN:
                raise ValueError("actionable signal must not have side SIDE_UNKNOWN")
            if not self.quantity > 0.0 or not math.isfinite(self.quantity):
                raise ValueError("actionable signal must have quantity > 0")
            if self.valid_until_ns <= self.created_at_ns:
                raise ValueError("valid_until_ns must be after created_at_ns")
        if self.status == SignalStatus.SIGNAL_ABSTAIN and (
            self.side != SignalSide.SIDE_UNKNOWN or self.quantity != 0.0
        ):
            raise ValueError("abstain signal must have SIDE_UNKNOWN and quantity 0")
        return self


class DebateState(BaseModel):
    """State threaded through the LangGraph nodes (nodes return updates, never mutate)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    market_context: MarketContext
    regime: RegimeLabel
    regime_confidence: UnitFloat
    # time.monotonic() at debate start; set by the first node, drives the overall deadline.
    debate_started_at: float | None = None
    blue_thesis: BlueThesis | None = None
    red_challenge: RedChallenge | None = None
    judge_verdict: JudgeVerdict | None = None
    debate_summary: str | None = None


class TradeOutcome(BaseModel):
    """Closed-trade facts handed to the Reflector (real inputs, never invented)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    realised_pnl: FiniteFloat
    regime_at_close: RegimeLabel
    description: Annotated[str, Field(max_length=2000)] = ""


class ReflectorDraft(BaseModel):
    """The JSON the Reflector LLM must return (strict; validated before use)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    proposed_change: str = Field(min_length=1, max_length=4000)
    rationale: str = Field(min_length=1, max_length=4000)


class ProposalStage(StrEnum):
    """Promotion gate stages in order (docs/processes/sharp-promotion.md)."""

    COMPLIANCE = "COMPLIANCE"
    LEGAL = "LEGAL"
    BACKTEST = "BACKTEST"
    RISK = "RISK"
    CANARY = "CANARY"


REQUIRED_STAGES: tuple[ProposalStage, ...] = tuple(ProposalStage)


class RubricChangeProposal(BaseModel):
    """Reflector output. A DRAFT only: nothing in this package can apply it.

    Per docs/processes/sharp-promotion.md it must pass Compliance -> Legal ->
    Backtesting -> Risk -> Canary human sign-offs before any live effect.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_id: str = Field(min_length=1)
    trigger_signal_id: str = Field(min_length=1)
    created_at_ns: int = Field(ge=0)
    observed_underperformance: str
    proposed_change: str = Field(min_length=1, max_length=4000)
    rationale: str = Field(min_length=1, max_length=4000)
    realised_pnl: FiniteFloat
    regime_at_close: RegimeLabel
    debate_history: str
    status: Literal["DRAFT"] = "DRAFT"
    requires_human_signoff: Literal[True] = True
    required_stages: tuple[ProposalStage, ...] = REQUIRED_STAGES

    @field_validator("required_stages")
    @classmethod
    def _stages_are_the_full_gate(cls, v: tuple[ProposalStage, ...]) -> tuple[ProposalStage, ...]:
        if v != REQUIRED_STAGES:
            raise ValueError("required_stages must be the full promotion gate, in order")
        return v
