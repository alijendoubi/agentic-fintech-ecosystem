"""Held-signal context retention (DECISIONS row 7): RetainHeldContext writes one audit record and
executes nothing."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from types import ModuleType
from typing import Any

import pytest

from execution_motor.grpc_service import ExecutionMotorServicer
from execution_motor.halt import KillSwitch
from execution_motor.retention import (
    HELD_CONTEXT_EVENT,
    HELD_CONTEXT_SCHEMA,
    MAX_CONTEXT_BYTES,
    HeldContextRetainer,
)

from .helpers import NOW_NS
from .test_motor import FakeBroker, make_motor


@dataclass(frozen=True)
class _Record:
    seq: int
    hash: str


class _Audit:
    def __init__(self, fail: Exception | None = None) -> None:
        self.records: list[tuple[str, str, dict[str, object]]] = []
        self.fail = fail

    def record(self, event_type: str, actor: str, payload: Mapping[str, object]) -> _Record:
        if self.fail is not None:
            raise self.fail
        self.records.append((event_type, actor, dict(payload)))
        return _Record(seq=len(self.records), hash="ab" * 32)


def held_request(pb2: dict[str, ModuleType], **decision_over: Any) -> Any:
    signal = pb2["signal"].TradeSignal(
        signal_id="sig-held", symbol="AAPL", strategy_id="AFE-STRATEGY-001", quantity_nanos=10**11
    )
    snapshot = pb2["snapshot"].MarketSnapshot(symbol="AAPL", mid_price=190.25, z_score=-1.5)
    context = pb2["motor"].ExecutionContext(
        snapshot=snapshot,
        signal=signal,
        blue_node_thesis="blue thesis",
        red_node_challenge="red challenge",
        judge_synthesis="judge synthesis",
        compression_summaries=["summary 1"],
    )
    context.model_versions["cognitive-core"] = "test"
    decision = pb2["aegis"].AegisDecision(
        signal_id="sig-held",
        decision=3,
        hold_id="hold-1",
        hold_expires_at_ns=NOW_NS + 60 * 10**9,
        decided_at_ns=NOW_NS,
    )
    for key, value in decision_over.items():
        setattr(decision, key, value)
    return pb2["motor"].ExecuteRequest(decision=decision, context=context)


def _servicer(pb2: dict[str, ModuleType], retainer: HeldContextRetainer | None) -> Any:
    broker = FakeBroker()
    servicer = ExecutionMotorServicer(
        make_motor(broker),
        pb2["motor"],
        kill_switch=KillSwitch(start_halted=False),
        retainer=retainer,
        clock_ns=lambda: NOW_NS,
    )
    return servicer, broker


def test_a_held_context_is_retained_byte_exact(pb2: dict[str, ModuleType]) -> None:
    audit = _Audit()
    servicer, broker = _servicer(pb2, HeldContextRetainer(audit, actor="execution-motor"))
    request = held_request(pb2)
    ack = servicer.RetainHeldContext(request, None)
    assert (ack.retained, ack.audit_seq, ack.record_hash, ack.detail) == (True, 1, "ab" * 32, "")
    assert broker.submitted == []  # nothing executed
    [(event, actor, payload)] = audit.records
    assert (event, actor) == (HELD_CONTEXT_EVENT, "execution-motor")
    assert payload["schema"] == HELD_CONTEXT_SCHEMA
    assert (payload["hold_id"], payload["signal_id"], payload["symbol"]) == (
        "hold-1",
        "sig-held",
        "AAPL",
    )
    assert payload["aegis_decided_at_ns"] == NOW_NS
    assert payload["model_versions"] == {"cognitive-core": "test"}
    raw = base64.b64decode(str(payload["context_proto_b64"]))
    assert hashlib.sha256(raw).hexdigest() == payload["context_sha256"]
    restored = pb2["motor"].ExecutionContext.FromString(raw)
    assert restored == request.context
    assert restored.snapshot.mid_price == 190.25
    assert list(restored.compression_summaries) == ["summary 1"]
    decision = pb2["aegis"].AegisDecision.FromString(
        base64.b64decode(str(payload["decision_proto_b64"]))
    )
    assert decision == request.decision
    assert not any(isinstance(v, float) for v in payload.values())  # audit canonical form


@pytest.mark.parametrize(
    ("mutate", "detail"),
    [
        (lambda r: setattr(r.decision, "decision", 1), "not HELD_FOR_HUMAN"),
        (lambda r: setattr(r.decision, "hold_id", ""), "hold_id"),
        (lambda r: setattr(r.decision, "hold_id", "bad id!"), "hold_id"),
        (lambda r: setattr(r.context.signal, "signal_id", "other"), "signal_id"),
        (lambda r: setattr(r.context.snapshot, "symbol", "MSFT"), "symbol"),
        (lambda r: r.context.ClearField("snapshot"), "snapshot"),
        (lambda r: r.ClearField("context"), "no context"),
    ],
)
def test_a_request_that_is_not_a_held_signal_is_refused(
    pb2: dict[str, ModuleType], mutate: Any, detail: str
) -> None:
    audit = _Audit()
    servicer, _ = _servicer(pb2, HeldContextRetainer(audit, actor="execution-motor"))
    request = held_request(pb2)
    mutate(request)
    ack = servicer.RetainHeldContext(request, None)
    assert ack.retained is False
    assert detail in ack.detail
    assert audit.records == []


def test_an_oversized_context_or_audit_failure_is_not_retained(pb2: dict[str, ModuleType]) -> None:
    audit = _Audit()
    servicer, _ = _servicer(pb2, HeldContextRetainer(audit, actor="execution-motor"))
    big = held_request(pb2)
    big.context.judge_synthesis = "x" * (MAX_CONTEXT_BYTES + 1)
    ack = servicer.RetainHeldContext(big, None)
    assert ack.retained is False and "limit" in ack.detail
    assert audit.records == []

    failing, _ = _servicer(
        pb2, HeldContextRetainer(_Audit(fail=RuntimeError("db down")), actor="execution-motor")
    )
    ack = failing.RetainHeldContext(held_request(pb2), None)
    assert ack.retained is False and "db down" in ack.detail


def test_without_a_retainer_the_rpc_says_so(pb2: dict[str, ModuleType]) -> None:
    servicer, _ = _servicer(pb2, None)
    ack = servicer.RetainHeldContext(held_request(pb2), None)
    assert ack.retained is False
    assert "not configured" in ack.detail
