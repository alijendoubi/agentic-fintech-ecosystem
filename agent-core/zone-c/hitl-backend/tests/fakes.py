"""Fakes for Aegis holds, the audit log and the motor relay, plus JWT/held-signal builders."""

from __future__ import annotations

import time
from typing import Any

import grpc
import jwt

SECRET = "q7Xv2LmR9sTz4WbN8cKp1HdF6gJy3AeU-test-only"
NOW_NS = 1_800_000_000_000_000_000
HOLD_ID = "3f2a1c9e-8b7d-4e6f-a5c4-1b2d3e4f5a6b"
SIGNAL_ID = "0b9c7f3e-6d0e-4a3a-9c53-0d9d5b8e1a11"
NANO = 1_000_000_000


class FakeRpcError(grpc.RpcError):
    def __init__(self, code: grpc.StatusCode, details: str = "") -> None:
        super().__init__(details)
        self._code = code
        self._details = details

    def code(self) -> grpc.StatusCode:
        return self._code

    def details(self) -> str:
        return self._details


def held_signal(
    pb: dict[str, Any], *, quantity_shares: int = 10, expires_ns: int = NOW_NS + 30 * NANO
) -> Any:
    aegis, ts, snap = pb["aegis_pb2"], pb["trade_signal_pb2"], pb["market_snapshot_pb2"]
    signal = ts.TradeSignal(
        signal_id=SIGNAL_ID,
        symbol="AAPL",
        created_at_ns=NOW_NS - NANO,
        side=ts.BUY,
        omega=0.72,
        expected_value=64.0,
        p_success=0.65,
        p_failure=0.35,
        reward_estimate=120.0,
        risk_estimate=40.0,
        regime=snap.TRENDING_BULL,
        regime_confidence=0.85,
        debate_summary="Judge sided BUY",
        valid_until_ns=NOW_NS + 60 * NANO,
        quantity_nanos=quantity_shares * NANO,
        price_limit_nanos=190_500_000_000,
        strategy_id="AFE-STRATEGY-001",
    )
    decision = aegis.AegisDecision(
        signal_id=SIGNAL_ID,
        decision=aegis.DECISION_HELD_FOR_HUMAN,
        hold_id=HOLD_ID,
        hold_expires_at_ns=expires_ns,
        decided_at_ns=NOW_NS - NANO,
        reasons=[aegis.REASON_UNUSUAL_ORDER_SIZE],
    )
    decision.results.add(
        control_id="C05",
        is_hard=False,
        passed=False,
        threshold="5",
        observed="10",
        reason=aegis.REASON_UNUSUAL_ORDER_SIZE,
    )
    decision.results.add(
        control_id="C01", is_hard=True, passed=True, threshold="190.50", observed="190.45"
    )
    return aegis.HeldSignal(decision=decision, signal=signal)


class FakeHolds:
    identity = "hitl-backend"

    def __init__(self, pb: dict[str, Any], held: Any | None = None) -> None:
        self.pb = pb
        self.held = {h.decision.hold_id: h for h in ([held] if held is not None else [])}
        self.resolved: list[dict[str, Any]] = []
        self.resolve_error: grpc.RpcError | None = None
        self.release_outcome = pb["aegis_pb2"].DECISION_APPROVED

    def list(self) -> list[Any]:
        return list(self.held.values())

    def get(self, hold_id: str) -> Any:
        if hold_id not in self.held:
            raise FakeRpcError(grpc.StatusCode.NOT_FOUND)
        return self.held[hold_id]

    def resolve(self, **fields: Any) -> Any:
        self.resolved.append(fields)
        if self.resolve_error is not None:
            raise self.resolve_error
        aegis = self.pb["aegis_pb2"]
        held = self.held.pop(fields["hold_id"])
        outcome = self.release_outcome if fields["approve"] else aegis.DECISION_REJECTED
        reasons = (
            [] if outcome == aegis.DECISION_APPROVED else [aegis.REASON_HOLD_REJECTED_BY_OPERATOR]
        )
        return aegis.AegisDecision(
            signal_id=held.signal.signal_id, decision=outcome, reasons=reasons
        )


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []
        self.fail = False

    def record(self, event_type: str, actor: str, payload: dict[str, object]) -> None:
        if self.fail:
            raise OSError("audit down")
        self.events.append((event_type, actor, payload))


class FakeRelay:
    def __init__(self, pb: dict[str, Any]) -> None:
        self.pb = pb
        self.requests: list[Any] = []

    def execute(self, request: Any) -> Any:
        self.requests.append(request)
        return self.pb["execution_motor_pb2"].ExecuteAck(
            accepted=True, status="filled", order_id="o-1"
        )


def token(
    *,
    sub: str = "alice",
    role: str = "approver",
    amr: list[str] | None = None,
    exp_in: int = 600,
    secret: str = SECRET,
    **extra: Any,
) -> str:
    claims = {
        "sub": sub,
        "role": role,
        "amr": ["pwd", "mfa"] if amr is None else amr,
        "exp": int(time.time()) + exp_in,
        **extra,
    }
    return jwt.encode(claims, secret, algorithm="HS256")
