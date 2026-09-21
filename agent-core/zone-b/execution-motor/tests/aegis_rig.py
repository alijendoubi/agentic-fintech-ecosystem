"""Test rig: AegisDecision messages built from the real-Aegis fixtures and a motor wired to the
real dev-Ed25519 verifier. Nothing here ships in the runtime package."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from types import ModuleType
from typing import Any

from execution_motor.broker import Broker
from execution_motor.config import MotorConfig
from execution_motor.halt import KillSwitch
from execution_motor.limits import IdempotencyStore
from execution_motor.motor import ExecutionMotor
from execution_motor.sor import RouterConfig, SmartOrderRouter, UnscoredPolicy
from execution_motor.verifiers import (
    AegisAttestationVerifier,
    RegisteredKey,
    SignatureAlgorithm,
)

from .test_aegis_fixtures import load_fixture, to_messages

FX_NOW_NS: int = load_fixture()["now_ns"]
D = Decimal


def fixture_verifier() -> AegisAttestationVerifier:
    fx = load_fixture()
    key = RegisteredKey(
        fx["key_id"], SignatureAlgorithm.ED25519_DEV, bytes.fromhex(fx["public_key_hex"])
    )
    return AegisAttestationVerifier([key], production=False)


def decision_from_fixture(pb2: dict[str, ModuleType], name: str) -> Any:
    order, att = to_messages(pb2, load_fixture()["cases"][name])
    return pb2["aegis"].AegisDecision(
        signal_id=order.signal_id, decision=1, attestation=att, order=order
    )


def fixture_motor(
    brokers: dict[str, Broker],
    *,
    allow_short: bool = False,
    kill: KillSwitch | None = None,
    clock_ns: Callable[[], int] | None = None,
    idempotency: IdempotencyStore | None = None,
) -> ExecutionMotor:
    return ExecutionMotor(
        config=MotorConfig(
            D(25_000), D(100_000), environment="development", allow_short_selling=allow_short
        ),
        brokers=brokers,
        router=SmartOrderRouter(RouterConfig(unscored_policy=UnscoredPolicy.ALLOW)),
        kill_switch=kill or KillSwitch(start_halted=False),
        verifier=fixture_verifier(),
        idempotency=idempotency,
        clock_ns=clock_ns or (lambda: FX_NOW_NS + 100_000_000),
    )
