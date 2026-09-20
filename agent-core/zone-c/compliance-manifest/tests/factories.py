"""Test data built from the REAL compiled protos. Values are synthetic test fixtures, not market
data."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from afe_manifest import CognitiveOutputs, ManifestInputs
from afe_manifest.model import GENESIS_HASH

MODELS = {"blue_node": "test-model-a@1", "judge": "test-model-b@2"}


def make_snapshot(pb: SimpleNamespace, **kw: Any) -> Any:
    fields: dict[str, Any] = {
        "symbol": "TEST",
        "ingestion_timestamp_ns": 1_000,
        "exchange_timestamp_ns": 900,
        "mid_price": 100.25,
        "bid_price": 100.0,
        "ask_price": 100.5,
        "regime": pb.market_snapshot.TRENDING_BULL,
    }
    return pb.market_snapshot.MarketSnapshot(**{**fields, **kw})


def make_signal(pb: SimpleNamespace, **kw: Any) -> Any:
    fields: dict[str, Any] = {
        "signal_id": "sig-1",
        "symbol": "TEST",
        "created_at_ns": 2_000,
        "side": pb.trade_signal.BUY,
        "quantity": 10.0,
        "omega": 0.8,
        "expected_value": 1.5,
        "debate_summary": "judge summary",
        "status": pb.trade_signal.SIGNAL_APPROVED,
    }
    return pb.trade_signal.TradeSignal(**{**fields, **kw})


def make_order(pb: SimpleNamespace, **kw: Any) -> Any:
    fields: dict[str, Any] = {
        "order_id": "ord-1",
        "signal_id": "sig-1",
        "symbol": "TEST",
        "created_at_ns": 3_000,
        "side": pb.order_request.ORDER_BUY,
        "order_type": pb.order_request.LIMIT,
        "quantity": 10.0,
        "limit_price": 100.5,
        "hsm_signature": b"\x01\x02",
        "hsm_key_id": "key-test",
    }
    return pb.order_request.OrderRequest(**{**fields, **kw})


def make_ptc(
    pb: SimpleNamespace, name: str = "check", *, hard: bool = True, passed: bool = True
) -> Any:
    kind = pb.compliance_manifest.HARD_BLOCK if hard else pb.compliance_manifest.SOFT_BLOCK
    return pb.compliance_manifest.PTCCheckResult(
        check_name=name,
        ptc_type=kind,
        passed=passed,
        reason="" if passed else f"{name} failed",
        threshold_value=1.0,
        actual_value=0.5,
    )


def make_hitl(pb: SimpleNamespace, decision: str = "approved", operator: str = "op-test") -> Any:
    return pb.compliance_manifest.HITLOverrideRecord(
        operator_id=operator,
        override_timestamp_ns=2_500,
        override_text="test",
        decision=decision,
    )


def make_inputs(pb: SimpleNamespace, prev: str = GENESIS_HASH, **overrides: Any) -> ManifestInputs:
    base: dict[str, Any] = {
        "snapshot": make_snapshot(pb),
        "signal": make_signal(pb),
        "order": make_order(pb),
        "model_versions": dict(MODELS),
        "ptc_checks": [make_ptc(pb, "hard_1"), make_ptc(pb, "soft_1", hard=False)],
        "prev_manifest_hash": prev,
        "hitl_override": None,
        "cognitive": CognitiveOutputs("blue", "red", "judge", ("s1", "s2")),
    }
    return ManifestInputs(**{**base, **overrides})


@dataclass
class FakeAudit:
    """In-memory AuditSink test double; can be told to fail."""

    fail: bool = False
    events: list[tuple[str, str, Mapping[str, object]]] = field(default_factory=list)

    def record(self, event_type: str, actor: str, payload: Mapping[str, object]) -> None:
        if self.fail:
            raise RuntimeError("audit unavailable")
        self.events.append((event_type, actor, dict(payload)))
