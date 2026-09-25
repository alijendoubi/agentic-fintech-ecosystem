"""Live integration tests against a RUNNING dev compose stack (ALI-166).

Every test uses the stack's real network paths and real mTLS (dev-tls certificates):
no fakes, no mocks, no in-process servers. What they prove, hop by hop:

* Aegis's mTLS listener refuses a client without a certificate, and its identity
  roles deny a known client calling an RPC outside its role;
* the kill-switch state is readable over mTLS and NORMAL on a fresh stack;
* Redis -> refdata-bridge -> Aegis.PushReferenceData works end to end (a snapshot
  published to Redis makes Aegis's reference data fresh);
* a TradeSignal submitted with cognitive-core's identity gets a real, complete
  decision from the real Aegis engine, and every failed control carries a reason;
* execution-motor, over its own mTLS listener, refuses a decision Aegis did not
  approve (and an approved one whose attestation was tampered with);
* the HITL operator terminal answers its health endpoint.

A real broker, real LLMs and the real Polygon feed are NOT exercised (owner-supplied
credentials, ALI-154 / ALI-165). See README.md.
"""

from __future__ import annotations

import json
import time
import urllib.request
import uuid
from collections.abc import Callable
from types import ModuleType
from typing import Any

import grpc
import pytest

from conftest import AEGIS_ADDR, HITL_URL, ca_only_channel

SHARE = 1_000_000_000
SYMBOL = "AAPL"  # on the dev limits allowlist (Aegis testkit limits)


def publish_snapshot(redis_client: Any, now_ns: int, symbol: str = SYMBOL) -> int:
    payload = {
        "symbol": symbol,
        "ingestion_ts_ns": now_ns,
        "mid_price": 150.0,
        "adv_30d": 50_000_000.0,
        "is_stale": False,
        # sensory-array sets warmup=false once its normalizer window is full; the bridge
        # treats a missing flag as still warming up and marks the snapshot stale.
        "warmup": False,
    }
    return int(redis_client.publish("sensory:snapshots", json.dumps(payload)))


def publish_regime(redis_client: Any, now_ns: int, symbol: str = SYMBOL) -> int:
    payload = {"symbol": symbol, "label": "TRENDING_BULL", "confidence": 0.9, "ts_ns": now_ns}
    return int(redis_client.publish("regime:labels", json.dumps(payload)))


def wait_until(predicate: Callable[[], bool], timeout_s: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def feed_until_fresh(
    redis_client: Any, reader: Any, empty: Any, stack_now_ns: int, timeout_s: float = 8.0
) -> bool:
    """Publish a fresh snapshot every 200 ms (like a live feed) until Aegis reports fresh
    reference data. A single snapshot is not enough: Aegis's freshness window
    (max_ref_age_ms, 1000 ms in the dev limits) is about one refdata-bridge batch interval
    (1.0 s), so one snapshot is often already stale by the time it is read back."""
    offset = stack_now_ns - time.time_ns()  # stack clock minus host clock
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        publish_snapshot(redis_client, time.time_ns() + offset)
        time.sleep(0.2)
        if reader.GetAegisState(empty, timeout=5).reference_data_fresh:
            return True
    return False


def trade_signal(pb: dict[str, ModuleType], now_ns: int) -> Any:
    ts = pb["trade_signal_pb2"]
    snap = pb["market_snapshot_pb2"]
    return ts.TradeSignal(
        signal_id=str(uuid.uuid4()),
        symbol=SYMBOL,
        created_at_ns=now_ns - 100_000_000,
        side=ts.SignalSide.Value("BUY"),
        omega=0.8,
        regime=snap.RegimeLabel.Value("TRENDING_BULL"),
        regime_confidence=0.9,
        valid_until_ns=now_ns + 4_000_000_000,
        quantity_nanos=1 * SHARE,
        price_limit_nanos=150 * SHARE,
        strategy_id="AFE-STRATEGY-001",
    )


# ------------------------------------------------------------------ mTLS and roles


def test_aegis_refuses_a_client_without_a_certificate(pb: dict[str, ModuleType]) -> None:
    channel = ca_only_channel(AEGIS_ADDR, "aegis")
    try:
        stub = pb["aegis_pb2_grpc"].AegisStub(channel)
        with pytest.raises(grpc.RpcError):
            stub.GetKillSwitchState(pb["aegis_pb2"].Empty(), timeout=5)
    finally:
        channel.close()


def test_aegis_denies_an_rpc_outside_the_callers_role(
    aegis_as: Any, pb: dict[str, ModuleType]
) -> None:
    # cognitive-core holds only signal-submitter; reading kill-switch state needs state-reader.
    with pytest.raises(grpc.RpcError) as err:
        aegis_as("cognitive-core").GetKillSwitchState(pb["aegis_pb2"].Empty(), timeout=5)
    assert err.value.code() == grpc.StatusCode.PERMISSION_DENIED


def test_kill_switch_state_is_readable_and_normal(aegis_as: Any, pb: dict[str, ModuleType]) -> None:
    state = aegis_as("aegis-supervisor").GetKillSwitchState(pb["aegis_pb2"].Empty(), timeout=5)
    assert state.effective_level == pb["aegis_pb2"].KILL_LEVEL_NORMAL, state


# ------------------------------------------------------------------ data path


def test_redis_to_bridge_to_aegis_reference_data(
    aegis_as: Any, pb: dict[str, ModuleType], redis_client: Any, stack_now_ns: int
) -> None:
    assert publish_snapshot(redis_client, stack_now_ns) >= 1, "no subscriber: is the bridge up?"
    reader = aegis_as("aegis-supervisor")
    empty = pb["aegis_pb2"].Empty()
    assert feed_until_fresh(redis_client, reader, empty, stack_now_ns), (
        "Aegis never saw fresh reference data from the bridge"
    )


# ------------------------------------------------------------------ decision path

_decision: dict[str, Any] = {}


def test_signal_gets_a_complete_decision_from_the_real_engine(
    aegis_as: Any, pb: dict[str, ModuleType], redis_client: Any, stack_now_ns: int
) -> None:
    publish_regime(redis_client, stack_now_ns)
    reader = aegis_as("aegis-supervisor")
    empty = pb["aegis_pb2"].Empty()
    feed_until_fresh(redis_client, reader, empty, stack_now_ns)

    decision = aegis_as("cognitive-core").SubmitSignal(trade_signal(pb, stack_now_ns), timeout=10)
    a = pb["aegis_pb2"]
    assert decision.decision in (
        a.DECISION_APPROVED,
        a.DECISION_REJECTED,
        a.DECISION_HELD_FOR_HUMAN,
    ), decision
    assert decision.results, "every evaluated control must be reported"
    for r in decision.results:
        if not r.passed:
            assert r.reason != a.REASON_UNSPECIFIED, r
    if decision.decision == a.DECISION_APPROVED:
        assert decision.attestation.signature, "an approval must carry a signed attestation"
    _decision["value"] = decision


def test_motor_refuses_what_aegis_did_not_approve(motor: Any, pb: dict[str, ModuleType]) -> None:
    decision = _decision.get("value")
    if decision is None:
        pytest.skip("depends on test_signal_gets_a_complete_decision_from_the_real_engine")
    a = pb["aegis_pb2"]
    candidate = a.AegisDecision()
    candidate.CopyFrom(decision)
    if candidate.decision == a.DECISION_APPROVED:
        sig = bytearray(candidate.attestation.signature)
        sig[0] ^= 0xFF  # tamper: the motor must refuse a bad signature
        candidate.attestation.signature = bytes(sig)
    ack = motor.Execute(candidate, timeout=10)
    assert not ack.accepted, ack
    assert ack.status in ("rejected", "halted"), ack


def test_motor_health_over_mtls(motor: Any, pb: dict[str, ModuleType]) -> None:
    health = motor.Health(pb["aegis_pb2"].Empty(), timeout=5)
    assert health.ok == (not health.halted), health


# ------------------------------------------------------------------ operator terminal


def test_hitl_terminal_health() -> None:
    with urllib.request.urlopen(f"{HITL_URL}/api/health", timeout=5) as resp:  # noqa: S310
        assert resp.status == 200
