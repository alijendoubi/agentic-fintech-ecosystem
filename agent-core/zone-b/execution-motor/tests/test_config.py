from __future__ import annotations

from decimal import Decimal

import pytest

from execution_motor.config import MotorConfig
from execution_motor.errors import ConfigError

_BASE = {
    "MOTOR_MAX_ORDER_NOTIONAL_USD": "25000",
    "MOTOR_MAX_SESSION_NOTIONAL_USD": "100000",
}


def test_from_env_reads_required_caps_as_decimal() -> None:
    cfg = MotorConfig.from_env(_BASE)
    assert cfg.max_order_notional == Decimal("25000")
    assert cfg.max_session_notional == Decimal("100000")
    assert cfg.max_order_age_ns == 5_000_000_000
    assert cfg.max_clock_skew_ns == 1_000_000_000


def test_from_env_overrides_optional_values() -> None:
    cfg = MotorConfig.from_env({**_BASE, "MOTOR_MAX_ORDER_AGE_MS": "250", "MOTOR_MAX_CLOCK_SKEW_MS": "10"})
    assert cfg.max_order_age_ns == 250_000_000
    assert cfg.max_clock_skew_ns == 10_000_000


@pytest.mark.parametrize("missing", list(_BASE))
def test_missing_cap_is_a_config_error(missing: str) -> None:
    env = {k: v for k, v in _BASE.items() if k != missing}
    with pytest.raises(ConfigError):
        MotorConfig.from_env(env)


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "NaN", "Infinity", ""])
def test_bad_cap_values_refused(bad: str) -> None:
    with pytest.raises(ConfigError):
        MotorConfig.from_env({**_BASE, "MOTOR_MAX_ORDER_NOTIONAL_USD": bad})


def test_session_cap_must_cover_order_cap() -> None:
    with pytest.raises(ConfigError):
        MotorConfig.from_env({**_BASE, "MOTOR_MAX_SESSION_NOTIONAL_USD": "100"})


@pytest.mark.parametrize("bad", ["0", "-5", "x"])
def test_bad_age_refused(bad: str) -> None:
    with pytest.raises(ConfigError):
        MotorConfig.from_env({**_BASE, "MOTOR_MAX_ORDER_AGE_MS": bad})


def test_config_is_frozen() -> None:
    cfg = MotorConfig.from_env(_BASE)
    with pytest.raises(AttributeError):
        cfg.max_order_notional = Decimal("1")  # type: ignore[misc]
