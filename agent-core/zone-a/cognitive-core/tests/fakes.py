"""Test doubles and builders. No network, no real sleeping."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from cognitive_core.config import CognitiveSettings, load_settings
from cognitive_core.models import DebateState, MarketContext, RegimeLabel

BLUE_JSON = '{"side": "BUY", "rationale": "strong momentum", "key_factors": ["ofi_positive"]}'
BLUE_UNKNOWN_JSON = '{"side": "SIDE_UNKNOWN", "rationale": "no view", "key_factors": []}'
RED_JSON = '{"counter_factors": ["overbought"], "failure_patterns": ["bull_trap_2023"]}'
JUDGE_JSON = (
    '{"side": "BUY", "omega": 0.72, "p_success": 0.65, "p_failure": 0.35, '
    '"reward_estimate": 120.0, "risk_estimate": 40.0}'
)
SUMMARY = "Blue bought on momentum; Red flagged overbought; Judge sided BUY at 0.72."


def make_settings(**env: str) -> CognitiveSettings:
    """Hermetic settings: only the given COGNITIVE_* variables, everything else default."""
    return load_settings(env)


def tiny_budget_settings(**env: str) -> CognitiveSettings:
    """Millisecond budgets so timeout paths finish instantly (no real sleeps)."""
    base: Mapping[str, str] = {
        "COGNITIVE_BLUE_LATENCY_BUDGET_S": "0.01",
        "COGNITIVE_RED_LATENCY_BUDGET_S": "0.01",
        "COGNITIVE_JUDGE_LATENCY_BUDGET_S": "0.01",
        "COGNITIVE_COMPRESSION_LATENCY_BUDGET_S": "0.01",
    }
    return load_settings({**base, **env})


def initial_state(regime: RegimeLabel = RegimeLabel.TRENDING_BULL) -> DebateState:
    return DebateState(
        market_context=MarketContext(
            symbol="AAPL",
            mid_price=190.0,
            z_score=1.2,
            mad_score=0.9,
            ofi=0.1,
            realized_vol=0.22,
            adv_30d=5_000_000,
        ),
        regime=regime,
        regime_confidence=0.85,
    )


class ScriptedClient:
    """Returns a fixed reply (or raises it if it is an exception); records call order."""

    def __init__(
        self, reply: object, name: str = "client", calls: list[str] | None = None
    ) -> None:
        self._reply = reply
        self._name = name
        self.calls: list[str] = calls if calls is not None else []
        self.prompts: list[str] = []

    async def ainvoke(self, prompt: str, *, max_tokens: int) -> str:
        self.calls.append(self._name)
        self.prompts.append(prompt)
        if isinstance(self._reply, BaseException):
            raise self._reply
        return self._reply  # type: ignore[return-value]  # tests may return non-str on purpose


class HangingClient:
    """Never answers; only a timeout can end the call."""

    def __init__(self, name: str = "hang", calls: list[str] | None = None) -> None:
        self._name = name
        self.calls: list[str] = calls if calls is not None else []

    async def ainvoke(self, prompt: str, *, max_tokens: int) -> str:
        self.calls.append(self._name)
        await asyncio.Event().wait()
        return "unreachable"
