"""Finding 3: production must not run on process-local idempotency state."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from execution_motor.config import MotorConfig
from execution_motor.errors import ConfigError
from execution_motor.halt import KillSwitch
from execution_motor.limits import IDEMPOTENCY_FILENAME, InMemoryIdempotencyStore
from execution_motor.models import ExecutionStatus, RejectReason
from execution_motor.motor import ExecutionMotor
from execution_motor.sor import RouterConfig, SmartOrderRouter, UnscoredPolicy

from .helpers import NOW_NS, HmacTestVerifier, attest, make_order
from .test_motor import FakeBroker

D = Decimal
_ENV = {"MOTOR_MAX_ORDER_NOTIONAL_USD": "1000", "MOTOR_MAX_SESSION_NOTIONAL_USD": "5000"}


def _motor(
    broker: FakeBroker,
    *,
    environment: str = "production",
    state_dir: Path | None = None,
    idempotency: InMemoryIdempotencyStore | None = None,
) -> ExecutionMotor:
    return ExecutionMotor(
        config=MotorConfig(D("25000"), D("100000"), environment=environment, state_dir=state_dir),
        brokers={"alpaca-paper": broker},
        router=SmartOrderRouter(RouterConfig(unscored_policy=UnscoredPolicy.ALLOW)),
        kill_switch=KillSwitch(start_halted=False),
        verifier=HmacTestVerifier(),
        idempotency=idempotency,
        clock_ns=lambda: NOW_NS,
    )


def test_config_reads_state_dir_from_env(tmp_path: Path) -> None:
    assert MotorConfig.from_env(_ENV).state_dir is None
    cfg = MotorConfig.from_env({**_ENV, "MOTOR_STATE_DIR": str(tmp_path)})
    assert cfg.state_dir == tmp_path


def test_production_refuses_to_start_without_a_state_dir() -> None:
    with pytest.raises(ConfigError, match="MOTOR_STATE_DIR"):
        _motor(FakeBroker())


def test_production_refuses_an_injected_in_memory_store(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="in-memory"):
        _motor(FakeBroker(), state_dir=tmp_path, idempotency=InMemoryIdempotencyStore())


def test_non_production_may_run_in_memory() -> None:
    for env in ("development", "staging", "test"):
        motor = _motor(FakeBroker(), environment=env)
        assert motor.execute(attest(make_order())).status is ExecutionStatus.FILLED


def test_production_replay_is_refused_after_restart(tmp_path: Path) -> None:
    broker = FakeBroker()
    first = _motor(broker, state_dir=tmp_path / "state")
    assert first.execute(attest(make_order())).status is ExecutionStatus.FILLED
    restarted = _motor(broker, state_dir=tmp_path / "state")  # captured decision, new process
    again = restarted.execute(attest(make_order()))
    assert again.reject_reason is RejectReason.DUPLICATE_ORDER
    assert len(broker.submitted) == 1


def test_non_empty_state_dir_without_a_store_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "leftover.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError, match="missing"):
        _motor(FakeBroker(), state_dir=tmp_path)


def test_corrupt_store_fails_closed(tmp_path: Path) -> None:
    (tmp_path / IDEMPOTENCY_FILENAME).write_text('{"key": "signal:a"}\n<<garbage\n', "utf-8")
    with pytest.raises(ConfigError, match="corrupt"):
        _motor(FakeBroker(), state_dir=tmp_path)


def test_state_dir_that_is_a_file_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "not-a-dir"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError):
        _motor(FakeBroker(), state_dir=target)
