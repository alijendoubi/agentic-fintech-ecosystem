"""Kill-switch drill against the RUNNING dev compose stack (ALI-170).

Follows agent-core/docs/runbooks/kill-switch-drill.md as far as the dev stack allows and
prints every observation as JSON evidence (also written to --out). Nothing here is
simulated: triggers, resets and signed approvals go to the real Aegis over mTLS, the
motor's reaction is read from the real execution-motor, and the freeze step pauses the
real Aegis container.

Needs: dev stack up with the dev-tls bootstrap (operator identity + reset approvers,
ALI-170) and, for the motor steps, the execution-motor from PR #9 (kill-switch watch).

    AFE_LIVE_STACK=1 python drill_kill_switch.py --out drill-evidence.json [--skip-freeze]
"""

from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import conftest as c

APPROVERS = c.DEV_TLS / "approvers"
SHARE = 1_000_000_000


def compile_protos() -> dict[str, ModuleType]:
    out = tempfile.mkdtemp(prefix="drill_pb_")
    protos = [str(p) for p in sorted(c.PROTO_DIR.glob("*.proto"))]
    subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{c.PROTO_DIR}",
            f"--python_out={out}",
            f"--grpc_python_out={out}",
            *protos,
        ],
        check=True,
    )
    sys.path.insert(0, out)
    names = (
        "aegis_pb2",
        "aegis_pb2_grpc",
        "trade_signal_pb2",
        "market_snapshot_pb2",
        "execution_motor_pb2",
        "execution_motor_pb2_grpc",
    )
    return {n: importlib.import_module(n) for n in names}


class Drill:
    def __init__(self, pb: dict[str, ModuleType]) -> None:
        self.pb = pb
        self.a = pb["aegis_pb2"]
        grpc_mod = pb["aegis_pb2_grpc"]
        self.op = grpc_mod.AegisStub(c.mtls_channel(c.AEGIS_ADDR, "operator", "aegis"))
        self.core = grpc_mod.AegisStub(c.mtls_channel(c.AEGIS_ADDR, "cognitive-core", "aegis"))
        self.motor = pb["execution_motor_pb2_grpc"].ExecutionMotorStub(
            c.mtls_channel(c.MOTOR_ADDR, "cognitive-core", "execution-motor")
        )
        self.clock = c.redis_as("healthcheck")
        self.evidence: dict[str, Any] = {}

    # ---------------------------------------------------------------- primitives

    def stack_ns(self) -> int:
        seconds, micros = self.clock.time()  # type: ignore[attr-defined]
        return int(seconds) * 1_000_000_000 + int(micros) * 1_000

    def state(self) -> Any:
        return self.op.GetKillSwitchState(self.a.Empty(), timeout=5)

    def level_name(self, level: int) -> str:
        return self.a.KillSwitchLevel.Name(level)

    def trigger(self, level: int, reason: str) -> Any:
        request = self.a.TriggerKillSwitchRequest(
            level=level,
            reason=reason,
            actor_id="drill/dev-operator-a",
            evidence_ref=f"ALI-170 drill: {reason}",
        )
        return self.op.TriggerKillSwitch(request, timeout=5)

    def latch_id(self, level: int) -> str:
        latches = [x for x in self.state().latches if x.level == level]
        if not latches:
            raise RuntimeError(f"no {self.level_name(level)} latch to reset")
        return max(latches, key=lambda x: x.latched_at_ns).trigger_id

    def approval(self, trigger_id: str, approver_id: str, role: str) -> Any:
        seed = bytes.fromhex((APPROVERS / f"{approver_id}.seed").read_text().strip())
        ns = self.stack_ns()
        text = (
            f"afe-reset-v1\ntrigger_id={trigger_id}\napprover_id={approver_id}\n"
            f"role={role}\napproved_at_ns={ns}\n"
        )
        sig = Ed25519PrivateKey.from_private_bytes(seed).sign(text.encode())
        return self.a.Authorization(
            approver_id=approver_id, role=role, approved_at_ns=ns, credential_ref=sig.hex()
        )

    def reset(self, trigger_id: str, approvers: list[tuple[str, str]]) -> Any:
        request = self.a.ResetKillSwitchRequest(
            trigger_id=trigger_id,
            approvals=[self.approval(trigger_id, i, r) for i, r in approvers],
            root_cause_ref="ALI-170 dev drill",
            note="dev drill reset",
        )
        return self.op.ResetKillSwitch(request, timeout=5)

    def motor_halted(self) -> bool:
        return bool(self.motor.Health(self.a.Empty(), timeout=5).halted)

    def wait(self, predicate: Any, timeout_s: float) -> float | None:
        start = time.monotonic()
        while time.monotonic() - start < timeout_s:
            try:
                if predicate():
                    return round(time.monotonic() - start, 3)
            except Exception:  # noqa: BLE001 - a transient RPC error just means "not yet"
                pass
            time.sleep(0.02)
        return None

    def motor_log_count(self, marker: str) -> int:
        out = subprocess.run(
            ["docker", "logs", "afe-execution-motor"], capture_output=True, text=True, check=False
        )
        return (out.stdout + out.stderr).count(marker)

    def signal(self) -> Any:
        ts, snap = self.pb["trade_signal_pb2"], self.pb["market_snapshot_pb2"]
        now = self.stack_ns()
        return ts.TradeSignal(
            signal_id=str(uuid.uuid4()),
            symbol="AAPL",
            created_at_ns=now - 100_000_000,
            side=ts.SignalSide.Value("BUY"),
            omega=0.8,
            regime=snap.RegimeLabel.Value("TRENDING_BULL"),
            regime_confidence=0.9,
            valid_until_ns=now + 4_000_000_000,
            quantity_nanos=SHARE,
            price_limit_nanos=150 * SHARE,
            strategy_id="AFE-STRATEGY-001",
        )

    def reasons(self, decision: Any) -> list[str]:
        return sorted({self.a.ReasonCode.Name(r.reason) for r in decision.results if not r.passed})

    # ---------------------------------------------------------------- steps

    def baseline(self) -> None:
        st = self.state()
        self.evidence["baseline"] = {
            "level": self.level_name(st.effective_level),
            "motor_halted": self.motor_halted(),
        }
        if st.effective_level != self.a.KILL_LEVEL_NORMAL:
            raise RuntimeError(f"baseline not NORMAL: {self.evidence['baseline']}")

    def soft(self) -> None:
        t0 = time.monotonic()
        st = self.trigger(self.a.KILL_LEVEL_SOFT, "drill step 1 SOFT")
        trigger_s = round(time.monotonic() - t0, 3)
        motor_s = self.wait(self.motor_halted, 5)
        decision = self.core.SubmitSignal(self.signal(), timeout=10)
        tid = self.latch_id(self.a.KILL_LEVEL_SOFT)
        resp = self.reset(tid, [("dev-operator-a", "operator")])
        resumed_s = self.wait(lambda: not self.motor_halted(), 5)
        self.evidence["step1_soft"] = {
            "level_after_trigger": self.level_name(st.effective_level),
            "trigger_rpc_s": trigger_s,
            "motor_halted_after_s": motor_s,
            "signal_decision": self.a.DecisionStatus.Name(decision.decision),
            "signal_failed_reasons": self.reasons(decision),
            "reset_single_operator_accepted": resp.accepted,
            "reset_refusal": resp.refusal_reason,
            "level_after_reset": self.level_name(self.state().effective_level),
            "motor_resumed_after_s": resumed_s,
        }

    def logic(self) -> None:
        sweeps_before = self.motor_log_count("kill_switch_cancel_sweep")
        self.trigger(self.a.KILL_LEVEL_LOGIC, "drill step 2 LOGIC")
        sweep_s = self.wait(
            lambda: self.motor_log_count("kill_switch_cancel_sweep") > sweeps_before, 5
        )
        tid = self.latch_id(self.a.KILL_LEVEL_LOGIC)
        one = self.reset(tid, [("dev-operator-a", "operator")])
        two = self.reset(tid, [("dev-operator-a", "operator"), ("dev-operator-b", "operator")])
        self.evidence["step2_logic"] = {
            "cancel_sweep_logged_within_s": sweep_s,
            "reset_one_operator_accepted": one.accepted,
            "reset_one_operator_refusal": one.refusal_reason,
            "reset_two_operators_accepted": two.accepted,
            "reset_two_operators_refusal": two.refusal_reason,
            "level_after_reset": self.level_name(self.state().effective_level),
        }

    def persistence(self) -> None:
        self.trigger(self.a.KILL_LEVEL_SOFT, "drill step 6 persistence")
        subprocess.run(["docker", "restart", "afe-aegis"], check=True, capture_output=True)
        up_s = self.wait(lambda: self.state() is not None, 60)
        after = self.level_name(self.state().effective_level)
        tid = self.latch_id(self.a.KILL_LEVEL_SOFT)
        resp = self.reset(tid, [("dev-operator-a", "operator")])
        self.evidence["step6_persistence"] = {
            "aegis_back_after_s": up_s,
            "level_after_restart": after,
            "reset_accepted": resp.accepted,
            "level_after_reset": self.level_name(self.state().effective_level),
        }

    def freeze(self, seconds: int) -> None:
        """Freeze Aegis (docker pause) and record what notices it, and when."""
        before = self.motor_log_count("kill_state_stream_down")
        subprocess.run(["docker", "pause", "afe-aegis"], check=True, capture_output=True)
        t0 = time.monotonic()
        stream_down_s = self.wait(
            lambda: self.motor_log_count("kill_state_stream_down") > before, seconds
        )
        motor_halted = self.motor_halted()
        remaining = seconds - (time.monotonic() - t0)
        if remaining > 0:
            time.sleep(remaining)
        subprocess.run(["docker", "unpause", "afe-aegis"], check=True, capture_output=True)
        recovered_s = self.wait(lambda: not self.motor_halted(), 90)
        sup = subprocess.run(
            ["docker", "logs", "--since", f"{seconds + 120}s", "afe-aegis-supervisor"],
            capture_output=True,
            text=True,
            check=False,
        )
        log = sup.stdout + sup.stderr
        self.evidence["step4_freeze"] = {
            "frozen_for_s": seconds,
            "motor_logged_stream_down_after_s": stream_down_s,
            "motor_halted_while_frozen": motor_halted,
            "motor_recovered_after_unfreeze_s": recovered_s,
            "supervisor_probe_failures_logged": log.count("aegis liveness probe failed"),
            "supervisor_trip_attempts_failed": log.count("cannot deliver HARD"),
            "supervisor_latched_hard": "HARD kill switch latched by the supervisor" in log,
            "level_after": self.level_name(self.state().effective_level),
        }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("drill-evidence.json"))
    ap.add_argument("--freeze-seconds", type=int, default=75)
    ap.add_argument("--skip-freeze", action="store_true")
    args = ap.parse_args()
    drill = Drill(compile_protos())
    drill.evidence["started_stack_ns"] = drill.stack_ns()
    drill.baseline()
    drill.soft()
    drill.logic()
    drill.persistence()
    if not args.skip_freeze:
        drill.freeze(args.freeze_seconds)
    drill.evidence["finished_stack_ns"] = drill.stack_ns()
    text = json.dumps(drill.evidence, indent=2)
    args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
