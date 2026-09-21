"""ALI-48: signal -> Aegis-format attestation -> motor verify -> mock paper broker -> report.

The attestation is a REAL Aegis output (fixture). The broker is an in-memory mock and Aegis is
a fixture plus an in-memory ReportExecution transport: no network anywhere.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from types import ModuleType
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from execution_motor.aegis_reporter import AegisReporter, InMemoryReportTransport
from execution_motor.canonical import build_canonical_text
from execution_motor.config import MotorConfig
from execution_motor.errors import ConfigError
from execution_motor.halt import KillStateHaltSource, KillSwitch
from execution_motor.models import ExecutionReport, ExecutionStatus, RejectReason
from execution_motor.motor import ExecutionMotor
from execution_motor.service import handle_decision
from execution_motor.sor import RouterConfig, SmartOrderRouter, UnscoredPolicy
from execution_motor.verifiers import (
    AegisAttestationVerifier,
    RegisteredKey,
    SignatureAlgorithm,
)
from mock_paper_broker import MockPaperBroker

KILL_NORMAL, KILL_SOFT, KILL_HARD, KILL_PHYSICAL = 0, 1, 4, 5
ORDER_FILLED, ORDER_PARTIAL, ORDER_REJECTED = 3, 2, 5
EQUITY = Decimal("100000")


@dataclass
class Rig:
    """One motor wired to the fixture verifier, a mock broker and a recording Aegis."""

    pb2: dict[str, ModuleType]
    fx: dict[str, Any]
    broker: MockPaperBroker = field(default_factory=MockPaperBroker)
    kill_level: Callable[[], int] = lambda: KILL_NORMAL  # noqa: E731
    allow_short: bool = False
    now_offset_ns: int = 100_000_000
    transport: InMemoryReportTransport = field(default_factory=InMemoryReportTransport)

    def __post_init__(self) -> None:
        key = RegisteredKey(
            self.fx["key_id"],
            SignatureAlgorithm.ED25519_DEV,
            bytes.fromhex(self.fx["public_key_hex"]),
        )
        self.reporter = AegisReporter(
            self.transport,
            self.pb2["aegis"].ExecutionReport,
            equity_provider=lambda: self.broker.get_account().equity,
            clock_ns=lambda: self.fx["now_ns"] + self.now_offset_ns,
        )
        self.motor = ExecutionMotor(
            config=MotorConfig(
                Decimal(25_000),
                Decimal(100_000),
                environment="development",
                allow_short_selling=self.allow_short,
            ),
            brokers={self.broker.venue: self.broker},
            router=SmartOrderRouter(RouterConfig(unscored_policy=UnscoredPolicy.ALLOW)),
            kill_switch=KillSwitch(
                start_halted=False, signal_source=KillStateHaltSource(lambda: self.kill_level())
            ),
            verifier=AegisAttestationVerifier([key], production=False),
            clock_ns=lambda: self.fx["now_ns"] + self.now_offset_ns,
        )

    def signal(self, case: str) -> Any:
        """The Zone A signal that Aegis turned into the fixture decision."""
        order = self.fx["cases"][case]["order"]
        return self.pb2["signal"].TradeSignal(
            signal_id=order["signal_id"],
            symbol=order["symbol"],
            side=self.pb2["signal"].SELL_SHORT if case == "sell_short" else order["side"],
            quantity_nanos=order["quantity_nanos"],
            price_limit_nanos=order["limit_price_nanos"],
        )

    def decision(self, case: str) -> Any:
        """AegisDecision (APPROVED) exactly as the real Aegis signed it."""
        data = self.fx["cases"][case]
        o, a = data["order"], data["attestation"]
        order = self.pb2["order"].OrderRequest(
            order_id=o["order_id"],
            signal_id=o["signal_id"],
            symbol=o["symbol"],
            created_at_ns=o["created_at_ns"],
            side=o["side"],
            order_type=o["order_type"],
            quantity_nanos=o["quantity_nanos"],
            limit_price_nanos=o["limit_price_nanos"],
            stop_price_nanos=o["stop_price_nanos"],
            algo=o["algo"],
            status=o["status"],
            hsm_signature=bytes.fromhex(o["hsm_signature_hex"]),
            hsm_key_id=o["hsm_key_id"],
            attestation_expires_at_ns=o["attestation_expires_at_ns"],
        )
        att = self.pb2["aegis"].Attestation(
            canonical_version=a["canonical_version"],
            payload_sha256=bytes.fromhex(a["payload_sha256_hex"]),
            signature=bytes.fromhex(a["signature_hex"]),
            key_id=a["key_id"],
            decided_at_ns=a["decided_at_ns"],
            expires_at_ns=a["expires_at_ns"],
            aegis_state_seq=a["aegis_state_seq"],
            limits_config_sha256=a["limits_config_sha256"],
        )
        return self.pb2["aegis"].AegisDecision(
            signal_id=o["signal_id"], decision=1, attestation=att, order=order
        )

    def run(self, decision: Any) -> ExecutionReport:
        return handle_decision(
            self.motor,
            decision,
            now_ns=self.fx["now_ns"] + self.now_offset_ns,
            reporter=self.reporter,
        )

    def assert_nothing_happened(self) -> None:
        assert self.broker.submitted == [], "an order reached the broker"
        assert self.transport.messages == [], "a rejected order was reported to Aegis"


@pytest.fixture
def rig(pb2: dict[str, ModuleType], aegis_fixture: dict[str, Any]) -> Rig:
    return Rig(pb2, aegis_fixture)


# ---------------------------------------------------------------------- happy path


def test_signal_to_paper_fill_to_aegis_report(rig: Rig) -> None:
    signal = rig.signal("buy")
    decision = rig.decision("buy")
    assert decision.signal_id == signal.signal_id  # Aegis decided exactly this signal

    assert rig.reporter.report_equity()  # session-open equity (starting NAV for C15)
    report = rig.run(decision)

    assert report.status is ExecutionStatus.FILLED and report.reject_reason is None
    assert len(rig.broker.submitted) == 1
    sent = rig.broker.submitted[0]
    assert sent.client_order_id == signal.signal_id  # order_id == signal_id
    assert (sent.symbol, sent.quantity, sent.limit_price) == ("AAPL", Decimal(10), Decimal(150))

    open_equity, exec_report = rig.transport.messages
    assert open_equity.order_id == "" and open_equity.account_equity_nanos == 10**9 * EQUITY
    assert exec_report.order_id == signal.signal_id
    assert exec_report.status == ORDER_FILLED
    assert exec_report.filled_qty_nanos == 10 * 10**9  # cumulative
    assert exec_report.avg_fill_price_nanos == 150 * 10**9
    assert exec_report.account_equity_nanos == 10**9 * EQUITY  # equity on every report


def test_partial_fill_is_reported_with_cumulative_quantity(
    pb2: dict[str, ModuleType], aegis_fixture: dict[str, Any]
) -> None:
    rig = Rig(pb2, aegis_fixture, broker=MockPaperBroker(fill_fraction=Decimal("0.4")))
    report = rig.run(rig.decision("buy"))
    assert report.status is ExecutionStatus.PARTIALLY_FILLED
    (msg,) = rig.transport.messages
    assert msg.status == ORDER_PARTIAL and msg.filled_qty_nanos == 4 * 10**9


def test_sell_executes_and_signed_short_needs_the_explicit_switch(
    pb2: dict[str, ModuleType], aegis_fixture: dict[str, Any]
) -> None:
    rig = Rig(pb2, aegis_fixture)
    assert rig.run(rig.decision("sell")).status is ExecutionStatus.FILLED
    refused = rig.run(rig.decision("sell_short"))  # shorts disabled by default
    assert refused.reject_reason is RejectReason.ATTESTATION_INVALID
    assert len(rig.broker.submitted) == 1
    enabled = Rig(pb2, aegis_fixture, allow_short=True)
    assert enabled.run(enabled.decision("sell_short")).status is ExecutionStatus.FILLED


# ------------------------------------------------------------------- negative: tamper


def _tamper_qty(d: Any) -> None:
    d.order.quantity_nanos += 1


def _tamper_price(d: Any) -> None:
    d.order.limit_price_nanos *= 2


def _tamper_symbol(d: Any) -> None:
    d.order.symbol = "MSFT"


def _tamper_stop(d: Any) -> None:
    d.order.stop_price_nanos = 1


def _tamper_state_seq(d: Any) -> None:
    d.attestation.aegis_state_seq += 1


def _tamper_limits_sha(d: Any) -> None:
    d.attestation.limits_config_sha256 = "cd" * 32


def _tamper_signal_id(d: Any) -> None:
    d.order.signal_id = d.order.order_id = "11111111-1111-4111-8111-111111111111"


def _tamper_signature(d: Any) -> None:
    sig = bytearray(d.attestation.signature)
    sig[0] ^= 0x01
    d.attestation.signature = d.order.hsm_signature = bytes(sig)


@pytest.mark.parametrize(
    "tamper",
    [
        _tamper_qty,
        _tamper_price,
        _tamper_symbol,
        _tamper_stop,
        _tamper_state_seq,
        _tamper_limits_sha,
        _tamper_signal_id,
        _tamper_signature,
    ],
)
def test_tampered_payload_is_denied_and_never_reaches_the_broker(
    rig: Rig, tamper: Callable[[Any], None]
) -> None:
    decision = rig.decision("buy")
    tamper(decision)
    report = rig.run(decision)
    assert report.status is ExecutionStatus.REJECTED
    # A stop price on a LIMIT order is denied even earlier, as a malformed order.
    assert report.reject_reason in (RejectReason.ATTESTATION_INVALID, RejectReason.INVALID_ORDER)
    rig.assert_nothing_happened()


def test_attacker_who_recomputes_the_digest_still_fails_the_signature(rig: Rig) -> None:
    """Tamper the qty and fix payload_sha256 up: the signature no longer matches."""
    decision = rig.decision("buy")
    o, a = decision.order, decision.attestation
    o.quantity_nanos *= 100
    forged_text = build_canonical_text(
        signal_id=o.signal_id,
        symbol=o.symbol,
        side="BUY",
        order_type="LIMIT",
        qty_nanos=o.quantity_nanos,
        limit_price_nanos=o.limit_price_nanos,
        stop_price_nanos=o.stop_price_nanos,
        decided_at_ns=a.decided_at_ns,
        expires_at_ns=a.expires_at_ns,
        aegis_state_seq=a.aegis_state_seq,
        limits_config_sha256=a.limits_config_sha256,
        key_id=a.key_id,
    )
    a.payload_sha256 = hashlib.sha256(forged_text).digest()
    assert rig.run(decision).reject_reason is RejectReason.ATTESTATION_INVALID
    rig.assert_nothing_happened()


def test_attestation_signed_by_an_attacker_key_under_the_real_key_id_is_denied(rig: Rig) -> None:
    decision = rig.decision("buy")
    o, a = decision.order, decision.attestation
    o.quantity_nanos *= 100
    text = build_canonical_text(
        signal_id=o.signal_id,
        symbol=o.symbol,
        side="BUY",
        order_type="LIMIT",
        qty_nanos=o.quantity_nanos,
        limit_price_nanos=o.limit_price_nanos,
        stop_price_nanos=o.stop_price_nanos,
        decided_at_ns=a.decided_at_ns,
        expires_at_ns=a.expires_at_ns,
        aegis_state_seq=a.aegis_state_seq,
        limits_config_sha256=a.limits_config_sha256,
        key_id=a.key_id,
    )
    digest = hashlib.sha256(text).digest()
    a.payload_sha256 = digest
    a.signature = o.hsm_signature = Ed25519PrivateKey.generate().sign(digest)
    assert rig.run(decision).reject_reason is RejectReason.ATTESTATION_INVALID
    rig.assert_nothing_happened()


def test_relabelled_client_order_id_is_refused(rig: Rig) -> None:
    decision = rig.decision("buy")
    decision.order.order_id = "attacker-chosen-client-id"
    assert rig.run(decision).status is ExecutionStatus.REJECTED
    rig.assert_nothing_happened()


# ------------------------------------------------------------------ negative: expiry


def test_expired_attestation_is_denied(rig: Rig) -> None:
    rig.now_offset_ns = 5_000_000_001  # TTL is 5 s
    assert rig.run(rig.decision("buy")).reject_reason is RejectReason.ORDER_EXPIRED
    rig.assert_nothing_happened()


def test_attestation_at_the_exact_expiry_instant_is_denied(rig: Rig) -> None:
    rig.now_offset_ns = 5_000_000_000
    assert rig.run(rig.decision("buy")).reject_reason is RejectReason.ORDER_EXPIRED
    rig.assert_nothing_happened()


def test_attestation_decided_in_the_future_is_denied(rig: Rig) -> None:
    rig.now_offset_ns = -5_000_000_000
    assert rig.run(rig.decision("buy")).reject_reason is RejectReason.ORDER_FROM_FUTURE
    rig.assert_nothing_happened()


# ---------------------------------------------------------------- negative: wrong side


def test_buy_attestation_relabelled_as_sell_is_denied(rig: Rig) -> None:
    decision = rig.decision("buy")
    decision.order.side = rig.pb2["order"].ORDER_SELL
    assert rig.run(decision).reject_reason is RejectReason.ATTESTATION_INVALID
    rig.assert_nothing_happened()


def test_sell_attestation_relabelled_as_buy_is_denied(rig: Rig) -> None:
    decision = rig.decision("sell")
    decision.order.side = rig.pb2["order"].ORDER_BUY
    assert rig.run(decision).reject_reason is RejectReason.ATTESTATION_INVALID
    rig.assert_nothing_happened()


def test_unknown_side_is_refused_before_any_execution(rig: Rig) -> None:
    decision = rig.decision("buy")
    decision.order.side = 0  # ORDER_SIDE_UNSPECIFIED
    assert rig.run(decision).status is ExecutionStatus.REJECTED
    rig.assert_nothing_happened()


# ------------------------------------------------------------------- negative: replay


def test_replayed_signal_id_is_executed_once_only(rig: Rig) -> None:
    first = rig.run(rig.decision("buy"))
    replay = rig.run(rig.decision("buy"))  # byte-identical, still validly signed
    assert first.status is ExecutionStatus.FILLED
    assert replay.reject_reason is RejectReason.DUPLICATE_ORDER
    assert len(rig.broker.submitted) == 1  # the mock broker also asserts client-id uniqueness
    assert len(rig.transport.messages) == 1  # the replay was not reported as an execution


def test_a_rejected_forgery_does_not_burn_the_genuine_signal(rig: Rig) -> None:
    forged = rig.decision("buy")
    _tamper_qty(forged)
    assert rig.run(forged).reject_reason is RejectReason.ATTESTATION_INVALID
    assert rig.run(rig.decision("buy")).status is ExecutionStatus.FILLED


# --------------------------------------------------------------- negative: kill switch


@pytest.mark.parametrize("level", [KILL_SOFT, KILL_HARD, KILL_PHYSICAL])
def test_kill_switch_above_normal_halts_execution(rig: Rig, level: int) -> None:
    rig.kill_level = lambda: level
    report = rig.run(rig.decision("buy"))
    assert report.reject_reason is RejectReason.HALTED
    rig.assert_nothing_happened()


def test_hard_halt_is_sticky_even_after_aegis_reports_normal_again(rig: Rig) -> None:
    level = {"v": KILL_NORMAL}
    rig.kill_level = lambda: level["v"]
    level["v"] = KILL_HARD
    assert rig.run(rig.decision("buy")).reject_reason is RejectReason.HALTED
    level["v"] = KILL_NORMAL
    assert rig.run(rig.decision("buy")).reject_reason is RejectReason.HALTED  # no auto-resume
    rig.assert_nothing_happened()
    rig.motor._kill.resume(operator="ali")  # noqa: SLF001 - explicit human resume
    assert rig.run(rig.decision("buy")).status is ExecutionStatus.FILLED


def test_unreadable_kill_state_fails_closed(rig: Rig) -> None:
    def broken() -> int:
        raise ConnectionError("aegis kill-state stream down")

    rig.kill_level = broken
    assert rig.run(rig.decision("buy")).reject_reason is RejectReason.HALTED
    rig.assert_nothing_happened()


# --------------------------------------------------------- negative: production config


def test_dev_signing_key_is_refused_by_a_production_motor(
    pb2: dict[str, ModuleType], aegis_fixture: dict[str, Any]
) -> None:
    key = RegisteredKey(
        aegis_fixture["key_id"],
        SignatureAlgorithm.ED25519_DEV,
        bytes.fromhex(aegis_fixture["public_key_hex"]),
    )
    with pytest.raises(ConfigError):
        AegisAttestationVerifier([key], production=True)
    broker = MockPaperBroker()
    with pytest.raises(ConfigError):
        ExecutionMotor(
            config=MotorConfig(Decimal(25_000), Decimal(100_000)),  # environment: production
            brokers={broker.venue: broker},
            router=SmartOrderRouter(RouterConfig()),
            kill_switch=KillSwitch(start_halted=False),
            verifier=AegisAttestationVerifier([key], production=False),
        )
