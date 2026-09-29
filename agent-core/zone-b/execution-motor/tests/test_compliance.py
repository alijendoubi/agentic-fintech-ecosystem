"""ALI-161: ExecuteWithContext, context binding, the SHARP gate and the Compliance Manifest.

Fakes stand in for the gates in most tests; ``test_real_manifest_*`` runs the real
``afe_manifest`` library (sibling package, test-time only) against a temporary directory.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from execution_motor.compliance import (
    AfeManifestWriter,
    ComplianceConfig,
    ComplianceGates,
    SharpStrategyGate,
    load_strategy_map,
    ptc_checks_from_decision,
)
from execution_motor.errors import ConfigError
from execution_motor.grpc_service import ExecutionMotorServicer
from execution_motor.halt import KillSwitch
from execution_motor.models import RejectReason

from .helpers import NOW_NS
from .test_motor import FakeBroker, make_motor
from .test_service import decision

SIGNAL_ID = "0b9c7f3e-6d0e-4a3a-9c53-0d9d5b8e1a11"  # the order's signal_id in test_proto_adapter
STRATEGY = "AFE-STRATEGY-001"


class _Ctx:
    """Stand-in for grpc.ServicerContext."""


class FakeGate:
    def __init__(self, refusal: str | None = None) -> None:
        self.refusal = refusal
        self.seen: list[str] = []

    def check(self, strategy_id: str) -> str | None:
        self.seen.append(strategy_id)
        return self.refusal


class FakeWriter:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[Any, Any]] = []

    def write(self, decision: Any, context: Any) -> str:
        self.calls.append((decision, context))
        if self.error is not None:
            raise self.error
        return "d" * 64


def approved(pb2: dict[str, ModuleType]) -> Any:
    """An APPROVED decision as Aegis sends it: signal_id == order.signal_id, with results."""
    msg = decision(pb2, signal_id=SIGNAL_ID, aegis_version="aegis 0.1.0")
    msg.results.add(
        control_id="C01", is_hard=True, passed=True, threshold="190.50", observed="190.45"
    )
    msg.results.add(
        control_id="C12",
        is_hard=False,
        passed=True,
        threshold="0.25",
        observed="0.1",
        detail="soft limit",
    )
    return msg


def context(pb2: dict[str, ModuleType], **signal_over: Any) -> Any:
    snap = pb2["snapshot"].MarketSnapshot(
        symbol="AAPL",
        ingestion_timestamp_ns=NOW_NS - 1_000,
        mid_price=190.5,
        regime=pb2["snapshot"].TRENDING_BULL,
    )
    fields: dict[str, Any] = {
        "signal_id": SIGNAL_ID,
        "symbol": "AAPL",
        "strategy_id": STRATEGY,
        "omega": 0.8,
        "status": pb2["signal"].SIGNAL_APPROVED,
    }
    fields.update(signal_over)
    sig = pb2["signal"].TradeSignal(**fields)
    ctx = pb2["motor"].ExecutionContext(
        snapshot=snap,
        signal=sig,
        blue_node_thesis="blue",
        red_node_challenge="red",
        judge_synthesis="judge",
    )
    ctx.model_versions["cognitive-core"] = "test-1"
    return ctx


def request(pb2: dict[str, ModuleType], ctx: Any | None = None, dec: Any | None = None) -> Any:
    msg = pb2["motor"].ExecuteRequest(decision=dec if dec is not None else approved(pb2))
    msg.context.CopyFrom(ctx if ctx is not None else context(pb2))
    return msg


def servicer(
    pb2: dict[str, ModuleType], broker: FakeBroker, gates: ComplianceGates
) -> ExecutionMotorServicer:
    return ExecutionMotorServicer(
        make_motor(broker),
        pb2["motor"],
        kill_switch=KillSwitch(start_halted=False),
        compliance=gates,
        clock_ns=lambda: NOW_NS,
    )


# --------------------------------------------------------------------------- the RPC path


def test_context_request_with_both_gates_passing_reaches_broker(
    pb2: dict[str, ModuleType],
) -> None:
    broker, gate, writer = FakeBroker(), FakeGate(), FakeWriter()
    ack = servicer(pb2, broker, ComplianceGates(writer, gate)).ExecuteWithContext(
        request(pb2), _Ctx()
    )
    assert ack.accepted is True, ack.detail
    assert gate.seen == [STRATEGY]
    assert len(writer.calls) == 1
    assert len(broker.submitted) == 1


def test_plain_execute_is_refused_when_compliance_is_enforced(pb2: dict[str, ModuleType]) -> None:
    broker = FakeBroker()
    ack = servicer(pb2, broker, ComplianceGates(FakeWriter(), None)).Execute(approved(pb2), _Ctx())
    assert ack.accepted is False
    assert ack.reject_reason == RejectReason.CONTEXT_MISSING.value
    assert broker.submitted == []


def test_plain_execute_still_works_with_compliance_disabled(pb2: dict[str, ModuleType]) -> None:
    broker = FakeBroker()
    ack = servicer(pb2, broker, ComplianceGates(None, None)).Execute(approved(pb2), _Ctx())
    assert ack.accepted is True


@pytest.mark.parametrize(
    ("signal_over", "reason"),
    [
        ({"signal_id": "someone-else"}, RejectReason.CONTEXT_MISMATCH),
        ({"symbol": "MSFT"}, RejectReason.CONTEXT_MISMATCH),
        ({"strategy_id": "  "}, RejectReason.CONTEXT_MISMATCH),
    ],
)
def test_context_not_matching_the_signed_order_is_refused(
    pb2: dict[str, ModuleType], signal_over: dict[str, Any], reason: RejectReason
) -> None:
    broker, writer = FakeBroker(), FakeWriter()
    ack = servicer(pb2, broker, ComplianceGates(writer, FakeGate())).ExecuteWithContext(
        request(pb2, context(pb2, **signal_over)), _Ctx()
    )
    assert ack.reject_reason == reason.value
    assert writer.calls == [] and broker.submitted == []


def test_request_without_context_is_refused(pb2: dict[str, ModuleType]) -> None:
    broker = FakeBroker()
    msg = pb2["motor"].ExecuteRequest(decision=approved(pb2))
    ack = servicer(pb2, broker, ComplianceGates(FakeWriter(), None)).ExecuteWithContext(msg, _Ctx())
    assert ack.reject_reason == RejectReason.CONTEXT_MISSING.value
    assert broker.submitted == []


def test_unpromoted_strategy_is_refused_without_burning_the_signal(
    pb2: dict[str, ModuleType],
) -> None:
    broker, gate = FakeBroker(), FakeGate("SHARP proposal 'p1' is RISK")
    svc = servicer(pb2, broker, ComplianceGates(None, gate))
    refused = svc.ExecuteWithContext(request(pb2), _Ctx())
    assert refused.reject_reason == RejectReason.STRATEGY_NOT_PROMOTED.value
    assert "RISK" in refused.detail
    assert broker.submitted == []
    gate.refusal = None  # promoted afterwards: the same (still valid) decision may now trade
    assert svc.ExecuteWithContext(request(pb2), _Ctx()).accepted is True


def test_manifest_failure_refuses_the_order_and_consumes_the_signal(
    pb2: dict[str, ModuleType],
) -> None:
    broker, writer = FakeBroker(), FakeWriter(OSError("disk full"))
    svc = servicer(pb2, broker, ComplianceGates(writer, None))
    ack = svc.ExecuteWithContext(request(pb2), _Ctx())
    assert ack.reject_reason == RejectReason.MANIFEST_FAILED.value
    assert "disk full" in ack.detail
    assert broker.submitted == []
    writer.error = None  # fail closed: the claim is not released, so no silent retry
    again = svc.ExecuteWithContext(request(pb2), _Ctx())
    assert again.reject_reason == RejectReason.DUPLICATE_ORDER.value
    assert broker.submitted == []


def test_manifest_is_not_written_when_the_motor_rejects_first(pb2: dict[str, ModuleType]) -> None:
    broker, writer = FakeBroker(), FakeWriter()
    dec = approved(pb2)
    dec.attestation.signature = b"\x00" * len(dec.attestation.signature)
    dec.order.hsm_signature = dec.attestation.signature
    ack = servicer(pb2, broker, ComplianceGates(writer, None)).ExecuteWithContext(
        request(pb2, dec=dec), _Ctx()
    )
    assert ack.reject_reason == RejectReason.ATTESTATION_INVALID.value
    assert writer.calls == []


# ------------------------------------------------------------------------- Aegis -> PTC map


def test_every_control_becomes_a_ptc_check_with_exact_decimals_kept(
    pb2: dict[str, ModuleType],
) -> None:
    dec = approved(pb2)
    dec.results.add(
        control_id="C09",
        is_hard=True,
        passed=False,
        threshold="n/a",
        observed="1e999",
        reason=pb2["aegis"].ReasonCode.Value("REASON_HOLD_EXPIRED"),
    )
    checks = ptc_checks_from_decision(dec, pb2["compliance"], pb2["aegis"])
    assert [c.check_name for c in checks] == ["C01", "C12", "C09"]
    hard, soft = pb2["compliance"].HARD_BLOCK, pb2["compliance"].SOFT_BLOCK
    assert [c.ptc_type for c in checks] == [hard, soft, hard]
    assert checks[0].threshold_value == pytest.approx(190.5)
    assert "threshold='190.50'" in checks[0].reason
    assert checks[1].reason.endswith("soft limit")
    assert (checks[2].threshold_value, checks[2].actual_value) == (0.0, 0.0)  # unparsable/inf
    assert checks[2].reason.startswith("REASON_HOLD_EXPIRED")
    assert checks[2].passed is False


# ------------------------------------------------------------------------------ SHARP gate


class _Record:
    def __init__(self, state: str) -> None:
        self.state = state


class _Store:
    def __init__(self, history: dict[str, list[str]]) -> None:
        self.history = history

    def load(self, proposal_id: str) -> list[str]:
        return self.history.get(proposal_id, [])


def _gate(state: str | Exception, *, canary: bool = False) -> SharpStrategyGate:
    def fold(history: Any, lookup: Any) -> _Record:
        if isinstance(state, Exception):
            raise state
        return _Record(state)

    allowed = frozenset({"PROMOTED", "CANARY"} if canary else {"PROMOTED"})
    return SharpStrategyGate(
        {STRATEGY: "p1"},
        store=_Store({"p1": ["e1"]}),
        audit_lookup=None,
        fold=fold,
        allowed_states=allowed,
    )


def test_sharp_gate_allows_only_promoted() -> None:
    assert _gate("PROMOTED").check(STRATEGY) is None
    assert "RISK" in (_gate("RISK").check(STRATEGY) or "")
    assert "CANARY" in (_gate("CANARY").check(STRATEGY) or "")
    assert _gate("CANARY", canary=True).check(STRATEGY) is None


def test_sharp_gate_refuses_unmapped_unknown_and_unverifiable() -> None:
    assert "no SHARP proposal mapping" in (_gate("PROMOTED").check("OTHER") or "")
    unknown = SharpStrategyGate(
        {STRATEGY: "p2"},
        store=_Store({}),
        audit_lookup=None,
        fold=lambda h, a: _Record("PROMOTED"),
        allowed_states=frozenset({"PROMOTED"}),
    )
    assert "does not exist" in (unknown.check(STRATEGY) or "")
    assert "unverifiable" in (_gate(RuntimeError("audit mismatch")).check(STRATEGY) or "")


def test_strategy_map_file_is_validated(tmp_path: Path) -> None:
    good = tmp_path / "map.json"
    good.write_text(json.dumps({STRATEGY: "p1"}), encoding="utf-8")
    assert load_strategy_map(good) == {STRATEGY: "p1"}
    for bad in ("[]", "{}", '{"a": ""}', "not json"):
        path = tmp_path / "bad.json"
        path.write_text(bad, encoding="utf-8")
        with pytest.raises(ConfigError):
            load_strategy_map(path)
    with pytest.raises(ConfigError):
        load_strategy_map(tmp_path / "missing.json")


# ---------------------------------------------------------------------------------- config


def test_config_requires_both_gates_by_default() -> None:
    with pytest.raises(ConfigError, match="MOTOR_MANIFEST_DIR"):
        ComplianceConfig.from_env({"MOTOR_SHARP_STRATEGY_MAP": "/cfg/m.json"}, production=True)
    with pytest.raises(ConfigError, match="MOTOR_SHARP_STRATEGY_MAP"):
        ComplianceConfig.from_env({"MOTOR_MANIFEST_DIR": "/m"}, production=True)
    cfg = ComplianceConfig.from_env(
        {"MOTOR_MANIFEST_DIR": "/m", "MOTOR_SHARP_STRATEGY_MAP": "/cfg/m.json"}, production=True
    )
    assert cfg.needs_audit_db and not cfg.allow_canary


@pytest.mark.parametrize("flag", ["MOTOR_MANIFEST_DISABLED", "MOTOR_SHARP_DISABLED"])
def test_disabling_a_gate_is_refused_in_production(flag: str) -> None:
    env = {flag: "1", "MOTOR_MANIFEST_DIR": "/m", "MOTOR_SHARP_STRATEGY_MAP": "/cfg/m.json"}
    with pytest.raises(ConfigError, match="refused in production"):
        ComplianceConfig.from_env(env, production=True)


def test_dev_may_disable_the_sharp_gate_only() -> None:
    cfg = ComplianceConfig.from_env(
        {"MOTOR_SHARP_DISABLED": "1", "MOTOR_MANIFEST_DIR": "/m"}, production=False
    )
    assert cfg.strategy_map_file is None and cfg.manifest_dir == Path("/m")


# ------------------------------------------------------------------- real afe_manifest store

_MANIFEST_PKG = Path(__file__).resolve().parents[3] / "zone-c" / "compliance-manifest"


@pytest.fixture
def afe_manifest() -> ModuleType:
    if str(_MANIFEST_PKG) not in sys.path:
        sys.path.insert(0, str(_MANIFEST_PKG))
    module: ModuleType = pytest.importorskip("afe_manifest")
    return module


class _Audit:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def record(self, event_type: str, actor: str, payload: Any) -> None:
        self.events.append((event_type, actor, dict(payload)))


def _writer(afe: ModuleType, root: Path, pb2: dict[str, ModuleType]) -> tuple[Any, _Audit]:
    audit = _Audit()
    store = afe.FilesystemManifestStore(root, audit, actor="execution-motor")
    writer = AfeManifestWriter(
        store,
        build=afe.build_manifest,
        inputs_cls=afe.ManifestInputs,
        cognitive_cls=afe.CognitiveOutputs,
        cm_pb2=pb2["compliance"],
        aegis_pb2=pb2["aegis"],
    )
    return (store, writer), audit


def test_real_manifest_is_stored_chained_and_audited_before_the_broker(
    pb2: dict[str, ModuleType], afe_manifest: ModuleType, tmp_path: Path
) -> None:
    (store, writer), audit = _writer(afe_manifest, tmp_path / "manifests", pb2)
    broker = FakeBroker()
    ack = servicer(pb2, broker, ComplianceGates(writer, FakeGate())).ExecuteWithContext(
        request(pb2), _Ctx()
    )
    assert ack.accepted is True, ack.detail
    assert [e[0] for e in audit.events] == ["manifest.stored"]
    files = sorted((tmp_path / "manifests").glob("*.json"))
    assert len(files) == 1
    doc = json.loads(files[0].read_text(encoding="utf-8"))
    assert doc["model_versions"] == {
        "aegis": "aegis 0.1.0",
        "aegis-limits-sha256": "ab" * 32,
        "cognitive-core": "test-1",
    }
    assert [c["check_name"] for c in doc["manifest"]["ptc_checks"]] == ["C01", "C12"]
    assert doc["manifest"]["order"]["signal_id"] == SIGNAL_ID
    assert store.verify_store().ok


def test_real_manifest_refuses_a_soft_block_without_hitl_record(
    pb2: dict[str, ModuleType], afe_manifest: ModuleType, tmp_path: Path
) -> None:
    (_store, writer), audit = _writer(afe_manifest, tmp_path / "manifests", pb2)
    dec = approved(pb2)
    dec.results[1].passed = False  # soft control failed; no HITL approval in the context
    broker = FakeBroker()
    ack = servicer(pb2, broker, ComplianceGates(writer, None)).ExecuteWithContext(
        request(pb2, dec=dec), _Ctx()
    )
    assert ack.reject_reason == RejectReason.MANIFEST_FAILED.value
    assert "HITL" in ack.detail
    assert broker.submitted == [] and audit.events == []
