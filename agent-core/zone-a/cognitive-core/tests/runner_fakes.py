"""Builders for runner tests. No network, no real LLM, no real sleeping."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from cognitive_core.config import CognitiveSettings
from cognitive_core.graph import build_debate_graph
from cognitive_core.halt import HaltGate
from cognitive_core.health import HealthState
from cognitive_core.models import RegimeLabel, SignalSide, SignalStatus, TradeSignal
from cognitive_core.runner_config import RunnerSettings
from cognitive_core.service import CognitiveRunner
from cognitive_core.sinks import InMemorySink
from cognitive_core.tests.fakes import (
    BLUE_JSON,
    JUDGE_JSON,
    RED_JSON,
    SUMMARY,
    ScriptedClient,
    make_settings,
)

NOW_NS = 1_800_000_000_000_000_000


def snapshot(**overrides: Any) -> dict[str, Any]:
    """A valid sensory-array snapshot (fields the runner reads, plus some it ignores)."""
    base: dict[str, Any] = {
        "symbol": "AAPL",
        "ingestion_ts_ns": NOW_NS - 100_000_000,
        "exchange_ts_ns": NOW_NS - 200_000_000,
        "mid_price": 190.0,
        "bid_price": 189.99,
        "ask_price": 190.01,
        "z_score": 1.2,
        "mad_score": 0.9,
        "order_flow_imbalance": 0.1,
        "realized_volatility": 0.22,
        "adv_30d": 5_000_000.0,
        "warmup": False,
        "regime_label": "TRENDING_BULL",
        "regime_confidence": 0.85,
        "is_stale": False,
        "stale_reason": "",
    }
    base.update(overrides)
    return base


def snapshot_json(**overrides: Any) -> str:
    return json.dumps(snapshot(**overrides))


def runner_settings(**env: str) -> RunnerSettings:
    """Hermetic settings. Defaults: quantity 10 (actionable), no cooldown, fast heartbeat."""
    base = {
        "COGNITIVE_ORDER_QUANTITY": "10",
        "COGNITIVE_MIN_DEBATE_INTERVAL_S": "0",
        "COGNITIVE_HEARTBEAT_INTERVAL_S": "0.05",
    }
    return RunnerSettings.from_env({**base, **env})


@dataclass
class Harness:
    runner: CognitiveRunner
    sink: InMemorySink
    health: HealthState
    calls: list[str]
    clients: dict[str, ScriptedClient]
    monotonic: list[float] = field(default_factory=lambda: [0.0])


class FakeProvider:
    """A PrecedentProvider that returns canned precedents or raises."""

    def __init__(self, result: Sequence[Any] = (), error: Exception | None = None) -> None:
        self._result, self._error = result, error
        self.requests: list[dict[str, Any]] = []

    async def recall(self, **kwargs: Any) -> Sequence[Any]:
        self.requests.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._result


class FakeWriter:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.debates: list[dict[str, Any]] = []
        self.reflections: list[dict[str, Any]] = []

    async def write_debate(self, **kwargs: Any) -> None:
        if self.error is not None:
            raise self.error
        self.debates.append(kwargs)

    async def write_reflection(self, **kwargs: Any) -> None:
        if self.error is not None:
            raise self.error
        self.reflections.append(kwargs)


def pending_signal() -> TradeSignal:
    return TradeSignal(
        signal_id="sig-1",
        symbol="AAPL",
        created_at_ns=NOW_NS,
        side=SignalSide.BUY,
        quantity=10.0,
        omega=0.72,
        expected_value=64.0,
        p_success=0.65,
        p_failure=0.35,
        reward_estimate=120.0,
        risk_estimate=40.0,
        regime=RegimeLabel.TRENDING_BULL,
        regime_confidence=0.85,
        debate_summary="summary",
        valid_until_ns=NOW_NS + 2_000_000_000,
        status=SignalStatus.SIGNAL_PENDING,
    )


def make_harness(
    settings: RunnerSettings | None = None,
    *,
    env: dict[str, str] | None = None,
    cognitive: CognitiveSettings | None = None,
    judge_reply: object = JUDGE_JSON,
    sink: InMemorySink | None = None,
    precedents: Any = None,
    reflections: Any = None,
    client_overrides: dict[str, Any] | None = None,
) -> Harness:
    calls: list[str] = []
    cognitive = cognitive or make_settings()
    clients = {
        "blue": ScriptedClient(BLUE_JSON, "blue", calls),
        "red": ScriptedClient(RED_JSON, "red", calls),
        "judge": ScriptedClient(judge_reply, "judge", calls),
        "compression": ScriptedClient(SUMMARY, "compression", calls),
    }
    clients.update(client_overrides or {})
    graph = build_debate_graph(
        cognitive,
        blue_client=clients["blue"],
        red_client=clients["red"],
        judge_client=clients["judge"],
        compression_client=clients["compression"],
    )
    sink = sink if sink is not None else InMemorySink()
    health = HealthState(30.0)
    clock = [0.0]
    runner = CognitiveRunner(
        settings=settings or runner_settings(),
        cognitive=cognitive,
        graph=graph,
        sink=sink,
        halt=HaltGate(env or {}),
        health=health,
        precedents=precedents,
        reflections=reflections,
        clock_ns=lambda: NOW_NS,
        monotonic=lambda: clock[0],
    )
    return Harness(runner, sink, health, calls, clients, clock)
