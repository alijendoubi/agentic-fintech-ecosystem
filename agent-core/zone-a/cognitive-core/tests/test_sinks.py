from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cognitive_core.proto_mapping import from_proto
from cognitive_core.service import CycleOutcome
from cognitive_core.sinks import (
    AegisGrpcSink,
    AegisRelaySink,
    DecisionContext,
    InMemoryMotorSink,
    InMemorySink,
    LogSink,
    MotorGrpcSink,
    SinkError,
    build_execute_request,
    load_generated_motor_protos,
    load_generated_protos,
)
from cognitive_core.tests.runner_fakes import make_harness, pending_signal, snapshot_json


@pytest.mark.asyncio
async def test_in_memory_sink_collects_and_can_fail() -> None:
    sink = InMemorySink()
    receipt = await sink.send(pending_signal())
    assert receipt.accepted and sink.sent == [pending_signal()]
    sink.fail_with = ConnectionError("down")
    with pytest.raises(SinkError, match="down"):
        await sink.send(pending_signal())


@pytest.mark.asyncio
async def test_log_sink_sends_nothing() -> None:
    receipt = await LogSink().send(pending_signal())
    assert receipt.accepted is False and "dry-run" in receipt.detail


def test_load_generated_protos_imports_the_stubs(generated_dir: Path) -> None:
    modules = load_generated_protos(generated_dir)
    assert set(modules) == {"trade_signal_pb2", "aegis_pb2", "aegis_pb2_grpc"}
    assert hasattr(modules["aegis_pb2_grpc"], "AegisStub")


def test_load_generated_protos_fails_when_stubs_are_missing(tmp_path: Path) -> None:
    import importlib
    import sys

    for name in ("trade_signal_pb2", "aegis_pb2", "aegis_pb2_grpc"):
        sys.modules.pop(name, None)
    importlib.invalidate_caches()
    saved = list(sys.path)
    try:
        sys.path[:] = [p for p in sys.path if "generated" not in p]
        with pytest.raises(ImportError):
            load_generated_protos(tmp_path)
    finally:
        sys.path[:] = saved


class _FakeStub:
    def __init__(self, reply: Any = None, error: Exception | None = None) -> None:
        self.reply, self.error = reply, error
        self.calls: list[tuple[Any, float]] = []

    async def SubmitSignal(self, message: Any, *, timeout: float) -> Any:  # noqa: N802
        self.calls.append((message, timeout))
        if self.error:
            raise self.error
        return self.reply


@pytest.mark.asyncio
async def test_grpc_sink_maps_signal_with_nanos_and_strategy_id(generated_dir: Path) -> None:
    protos = load_generated_protos(generated_dir)
    reply = protos["aegis_pb2"].AegisDecision(
        decision=protos["aegis_pb2"].DECISION_APPROVED,
        reasons=[protos["aegis_pb2"].REASON_LOW_OMEGA],
    )
    stub = _FakeStub(reply)
    sink = AegisGrpcSink(
        stub=stub, trade_signal_pb2=protos["trade_signal_pb2"], strategy_id="S-1", timeout_s=0.5
    )
    receipt = await sink.send(pending_signal())
    ((message, timeout),) = stub.calls
    assert timeout == 0.5
    assert message.strategy_id == "S-1"
    assert message.quantity_nanos == 10_000_000_000
    assert from_proto(message, protos["trade_signal_pb2"]) == pending_signal()
    assert receipt.accepted and "DECISION_APPROVED" in receipt.detail
    assert "reasons=[19]" in receipt.detail


@pytest.mark.asyncio
async def test_grpc_sink_wraps_transport_errors_as_sink_error(generated_dir: Path) -> None:
    protos = load_generated_protos(generated_dir)
    sink = AegisGrpcSink(
        stub=_FakeStub(error=TimeoutError("deadline")),
        trade_signal_pb2=protos["trade_signal_pb2"],
        strategy_id="S",
    )
    with pytest.raises(SinkError, match="TimeoutError"):
        await sink.send(pending_signal())


@pytest.mark.asyncio
async def test_grpc_sink_tolerates_an_unreadable_decision(generated_dir: Path) -> None:
    protos = load_generated_protos(generated_dir)
    sink = AegisGrpcSink(
        stub=_FakeStub(SimpleNamespace()),
        trade_signal_pb2=protos["trade_signal_pb2"],
        strategy_id="S",
    )
    assert (await sink.send(pending_signal())).detail == "decision unreadable"


@pytest.mark.parametrize(("strategy", "timeout"), [("", 1.0), ("  ", 1.0), ("S", 0.0)])
def test_grpc_sink_rejects_bad_config(strategy: str, timeout: float) -> None:
    with pytest.raises(ValueError, match=r"strategy_id|timeout_s"):
        AegisGrpcSink(stub=None, trade_signal_pb2=None, strategy_id=strategy, timeout_s=timeout)


@pytest.mark.asyncio
async def test_real_grpc_round_trip_over_loopback(generated_dir: Path) -> None:
    """Full path: runner -> AegisGrpcSink -> real grpc.aio channel -> fake Aegis servicer."""
    import grpc

    protos = load_generated_protos(generated_dir)
    aegis_pb2, aegis_grpc = protos["aegis_pb2"], protos["aegis_pb2_grpc"]
    received: list[Any] = []

    class FakeAegis(aegis_grpc.AegisServicer):
        async def SubmitSignal(self, request: Any, context: Any) -> Any:  # noqa: N802
            received.append(request)
            return aegis_pb2.AegisDecision(
                signal_id=request.signal_id, decision=aegis_pb2.DECISION_HELD_FOR_HUMAN
            )

    server = grpc.aio.server()
    aegis_grpc.add_AegisServicer_to_server(FakeAegis(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            sink = AegisGrpcSink(
                stub=aegis_grpc.AegisStub(channel),
                trade_signal_pb2=protos["trade_signal_pb2"],
                strategy_id="AFE-STRATEGY-001",
                timeout_s=5.0,
            )
            h = make_harness(sink=sink)  # type: ignore[arg-type]
            assert await h.runner.handle_message(snapshot_json()) is CycleOutcome.EMITTED
    finally:
        await server.stop(None)
    (request,) = received
    assert request.symbol == "AAPL" and request.strategy_id == "AFE-STRATEGY-001"
    assert request.quantity_nanos == 10_000_000_000 and request.status == 0  # SIGNAL_PENDING


@pytest.mark.asyncio
async def test_real_grpc_unreachable_server_is_a_sink_error_not_a_hang(
    generated_dir: Path,
) -> None:
    import grpc

    protos = load_generated_protos(generated_dir)
    async with grpc.aio.insecure_channel("127.0.0.1:1") as channel:
        sink = AegisGrpcSink(
            stub=protos["aegis_pb2_grpc"].AegisStub(channel),
            trade_signal_pb2=protos["trade_signal_pb2"],
            strategy_id="S",
            timeout_s=1.0,
        )
        with pytest.raises(SinkError):
            await sink.send(pending_signal())


# -- AegisRelaySink: relay to execution-motor (only APPROVED + attestation forwards) ------


def _decision(
    protos: dict[str, Any],
    *,
    decision: int,
    with_attestation: bool = False,
    signal_id: str = "sig-1",
) -> Any:
    message = protos["aegis_pb2"].AegisDecision(signal_id=signal_id, decision=decision)
    if with_attestation:
        message.attestation.payload_sha256 = b"\x01" * 32
        message.attestation.signature = b"\x02" * 64
        message.attestation.key_id = "hsm-key-1"
    return message


@pytest.mark.asyncio
async def test_relay_forwards_the_exact_decision_when_approved_with_attestation(
    generated_dir: Path,
) -> None:
    protos = load_generated_protos(generated_dir)
    decision = _decision(
        protos, decision=protos["aegis_pb2"].DECISION_APPROVED, with_attestation=True
    )
    motor = InMemoryMotorSink()
    sink = AegisRelaySink(
        aegis_stub=_FakeStub(decision),
        trade_signal_pb2=protos["trade_signal_pb2"],
        strategy_id="S-1",
        motor_sink=motor,
    )
    receipt = await sink.send(pending_signal())
    assert receipt.accepted
    assert motor.sent == [decision]  # the exact AegisDecision object, unmodified


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["DECISION_HELD_FOR_HUMAN", "DECISION_REJECTED"])
async def test_relay_never_forwards_a_held_or_rejected_decision(
    generated_dir: Path, status: str
) -> None:
    protos = load_generated_protos(generated_dir)
    decision = _decision(protos, decision=getattr(protos["aegis_pb2"], status))
    motor = InMemoryMotorSink()
    sink = AegisRelaySink(
        aegis_stub=_FakeStub(decision),
        trade_signal_pb2=protos["trade_signal_pb2"],
        strategy_id="S",
        motor_sink=motor,
    )
    receipt = await sink.send(pending_signal())
    assert receipt.accepted  # the Aegis outcome is still reported to the runner
    assert motor.sent == []  # only Aegis's approval may reach execution


@pytest.mark.asyncio
async def test_relay_never_forwards_an_approval_missing_its_attestation(
    generated_dir: Path,
) -> None:
    protos = load_generated_protos(generated_dir)
    decision = _decision(
        protos, decision=protos["aegis_pb2"].DECISION_APPROVED, with_attestation=False
    )
    motor = InMemoryMotorSink()
    sink = AegisRelaySink(
        aegis_stub=_FakeStub(decision),
        trade_signal_pb2=protos["trade_signal_pb2"],
        strategy_id="S",
        motor_sink=motor,
    )
    await sink.send(pending_signal())
    assert motor.sent == []


@pytest.mark.asyncio
async def test_relay_failure_reaching_motor_is_not_swallowed(generated_dir: Path) -> None:
    protos = load_generated_protos(generated_dir)
    decision = _decision(
        protos, decision=protos["aegis_pb2"].DECISION_APPROVED, with_attestation=True
    )
    motor = InMemoryMotorSink(fail_with=TimeoutError("motor unreachable"))
    sink = AegisRelaySink(
        aegis_stub=_FakeStub(decision),
        trade_signal_pb2=protos["trade_signal_pb2"],
        strategy_id="S",
        motor_sink=motor,
    )
    with pytest.raises(SinkError, match="motor relay failed after aegis approval"):
        await sink.send(pending_signal())


@pytest.mark.asyncio
async def test_relay_propagates_an_unreachable_aegis_as_a_sink_error(generated_dir: Path) -> None:
    protos = load_generated_protos(generated_dir)
    sink = AegisRelaySink(
        aegis_stub=_FakeStub(error=TimeoutError("deadline")),
        trade_signal_pb2=protos["trade_signal_pb2"],
        strategy_id="S",
        motor_sink=InMemoryMotorSink(),
    )
    with pytest.raises(SinkError, match="SubmitSignal failed"):
        await sink.send(pending_signal())


@pytest.mark.parametrize(("strategy", "timeout"), [("", 1.0), ("  ", 1.0), ("S", 0.0)])
def test_relay_rejects_bad_config(strategy: str, timeout: float) -> None:
    with pytest.raises(ValueError, match=r"strategy_id|timeout_s"):
        AegisRelaySink(
            aegis_stub=None,
            trade_signal_pb2=None,
            strategy_id=strategy,
            motor_sink=InMemoryMotorSink(),
            aegis_timeout_s=timeout,
        )


@pytest.mark.asyncio
async def test_runner_delivers_the_signal_and_relays_the_approval_to_the_fake_motor(
    generated_dir: Path,
) -> None:
    """End to end through the runner: an approved decision reaches the fake motor sink with
    the exact AegisDecision payload the fake Aegis returned."""
    protos = load_generated_protos(generated_dir)
    decision = _decision(
        protos, decision=protos["aegis_pb2"].DECISION_APPROVED, with_attestation=True
    )
    motor = InMemoryMotorSink()
    sink = AegisRelaySink(
        aegis_stub=_FakeStub(decision),
        trade_signal_pb2=protos["trade_signal_pb2"],
        strategy_id="S-1",
        motor_sink=motor,
    )
    h = make_harness(sink=sink)  # type: ignore[arg-type]
    assert await h.runner.handle_message(snapshot_json()) is CycleOutcome.EMITTED
    assert motor.sent == [decision]


# -- MotorGrpcSink: mirrors AegisGrpcSink -------------------------------------------------


class _FakeMotorStub:
    def __init__(self, reply: Any = None, error: Exception | None = None) -> None:
        self.reply, self.error = reply, error
        self.calls: list[tuple[Any, float]] = []

    async def Execute(self, decision: Any, *, timeout: float) -> Any:  # noqa: N802
        self.calls.append((decision, timeout))
        if self.error:
            raise self.error
        return self.reply

    async def ExecuteWithContext(self, request: Any, *, timeout: float) -> Any:  # noqa: N802
        raise AssertionError("ExecuteWithContext needs a request builder and a context")


@pytest.mark.asyncio
async def test_motor_grpc_sink_relays_the_decision_and_reports_the_ack(
    generated_dir: Path,
) -> None:
    protos = load_generated_protos(generated_dir)
    decision = _decision(
        protos, decision=protos["aegis_pb2"].DECISION_APPROVED, with_attestation=True
    )
    ack = SimpleNamespace(accepted=True, detail="queued")
    stub = _FakeMotorStub(ack)
    sink = MotorGrpcSink(stub=stub, timeout_s=0.5)
    receipt = await sink.send(decision)
    ((sent, timeout),) = stub.calls
    assert sent is decision and timeout == 0.5
    assert receipt.accepted and "accepted=True" in receipt.detail


@pytest.mark.asyncio
async def test_motor_grpc_sink_wraps_transport_errors_as_sink_error() -> None:
    sink = MotorGrpcSink(stub=_FakeMotorStub(error=ConnectionError("down")))
    with pytest.raises(SinkError, match="Execute failed"):
        await sink.send(SimpleNamespace(signal_id="sig-1"))


@pytest.mark.asyncio
async def test_motor_grpc_sink_tolerates_an_unreadable_ack() -> None:
    sink = MotorGrpcSink(stub=_FakeMotorStub(SimpleNamespace()))
    assert (await sink.send(SimpleNamespace(signal_id="sig-1"))).detail == "ack unreadable"


def test_motor_grpc_sink_rejects_a_non_positive_timeout() -> None:
    with pytest.raises(ValueError, match="timeout_s"):
        MotorGrpcSink(stub=None, timeout_s=0.0)


@pytest.mark.asyncio
async def test_in_memory_motor_sink_collects_and_can_fail() -> None:
    sink = InMemoryMotorSink()
    decision = SimpleNamespace(signal_id="sig-1")
    receipt = await sink.send(decision)
    assert receipt.accepted and sink.sent == [decision]
    sink.fail_with = ConnectionError("down")
    with pytest.raises(SinkError, match="down"):
        await sink.send(decision)


# -- ALI-161: decision context for the Compliance Manifest --------------------------------


def _context(**over: Any) -> DecisionContext:
    fields: dict[str, Any] = {
        "symbol": "AAPL",
        "ingestion_ts_ns": 1_700_000_000_000_000_000,
        "mid_price": 190.5,
        "z_score": 1.2,
        "mad_score": 0.9,
        "order_flow_imbalance": 0.1,
        "realized_volatility": 0.2,
        "adv_30d": 5e7,
        "regime": "TRENDING_BULL",
        "blue_node_thesis": "BUY: momentum",
        "red_node_challenge": "counter: earnings",
        "judge_synthesis": "judge says go",
        "model_versions": {"cognitive-core/blue": "m-blue"},
    }
    fields.update(over)
    return DecisionContext(**fields)


class _FakeContextStub(_FakeMotorStub):
    def __init__(self, reply: Any = None) -> None:
        super().__init__(reply)
        self.context_calls: list[tuple[Any, float]] = []

    async def ExecuteWithContext(self, request: Any, *, timeout: float) -> Any:  # noqa: N802
        # Overrides the parent's refusal: this fake accepts context calls.
        self.context_calls.append((request, timeout))
        return self.reply


@pytest.mark.asyncio
async def test_runner_relays_the_signal_proto_and_the_debate_context(generated_dir: Path) -> None:
    protos = load_generated_protos(generated_dir)
    decision = _decision(
        protos, decision=protos["aegis_pb2"].DECISION_APPROVED, with_attestation=True
    )
    motor = InMemoryMotorSink()
    sink = AegisRelaySink(
        aegis_stub=_FakeStub(decision),
        trade_signal_pb2=protos["trade_signal_pb2"],
        strategy_id="S-1",
        motor_sink=motor,
    )
    h = make_harness(sink=sink)  # type: ignore[arg-type]
    assert await h.runner.handle_message(snapshot_json()) is CycleOutcome.EMITTED
    (signal_message,) = motor.signals
    (context,) = motor.contexts
    assert signal_message.strategy_id == "S-1"  # the exact proto that went to Aegis
    assert context is not None and context.symbol == signal_message.symbol
    assert context.regime and context.mid_price > 0
    assert set(context.model_versions) >= {"cognitive-core/blue", "cognitive-core/judge"}


@pytest.mark.asyncio
async def test_motor_grpc_sink_uses_execute_with_context_when_it_can(generated_dir: Path) -> None:
    protos = load_generated_protos(generated_dir)
    motor_protos = load_generated_motor_protos(generated_dir)
    decision = _decision(
        protos, decision=protos["aegis_pb2"].DECISION_APPROVED, with_attestation=True
    )
    signal_message = protos["trade_signal_pb2"].TradeSignal(signal_id="sig-1", symbol="AAPL")
    stub = _FakeContextStub(SimpleNamespace(accepted=True, detail="ok"))
    builder = build_execute_request(
        motor_protos["execution_motor_pb2"], motor_protos["market_snapshot_pb2"]
    )
    sink = MotorGrpcSink(stub=stub, timeout_s=0.5, request_builder=builder)
    await sink.send(decision, signal_message=signal_message, context=_context())
    ((request, timeout),) = stub.context_calls
    assert stub.calls == [] and timeout == 0.5
    assert request.decision == decision
    assert request.context.signal == signal_message
    snap = request.context.snapshot
    assert (snap.symbol, snap.mid_price, snap.is_stale) == ("AAPL", 190.5, False)
    assert snap.regime == motor_protos["market_snapshot_pb2"].TRENDING_BULL
    assert dict(request.context.model_versions) == {"cognitive-core/blue": "m-blue"}
    assert request.context.judge_synthesis == "judge says go"


@pytest.mark.asyncio
async def test_motor_grpc_sink_falls_back_to_plain_execute_without_context(
    generated_dir: Path,
) -> None:
    protos = load_generated_protos(generated_dir)
    decision = _decision(
        protos, decision=protos["aegis_pb2"].DECISION_APPROVED, with_attestation=True
    )
    stub = _FakeContextStub(SimpleNamespace(accepted=False, detail="context_missing"))
    sink = MotorGrpcSink(stub=stub, request_builder=lambda d, s, c: None)
    await sink.send(decision)  # no context: a compliance-enforcing motor will refuse this
    assert stub.context_calls == [] and len(stub.calls) == 1
