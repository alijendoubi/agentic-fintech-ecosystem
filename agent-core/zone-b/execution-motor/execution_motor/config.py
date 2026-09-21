"""Motor configuration. Caps have NO defaults: a missing cap is a startup error (fail closed)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final

from .errors import ConfigError

_NS_PER_MS: Final = 1_000_000
# 5 s covers the Aegis -> motor gRPC hop with room to spare (end-to-end budget is 2.6 s).
_DEFAULT_MAX_AGE_MS: Final = 5_000
_DEFAULT_SKEW_MS: Final = 1_000


def _positive_decimal(env: Mapping[str, str], name: str) -> Decimal:
    raw = env.get(name)
    if raw is None or not raw.strip():
        raise ConfigError(f"{name} is required")
    try:
        value = Decimal(raw.strip())
    except InvalidOperation as exc:
        raise ConfigError(f"{name} is not a valid decimal") from exc
    if not value.is_finite() or value <= 0:
        raise ConfigError(f"{name} must be a finite number > 0")
    return value


def _positive_ms(env: Mapping[str, str], name: str, default_ms: int) -> int:
    raw = env.get(name)
    if raw is None:
        return default_ms * _NS_PER_MS
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer number of milliseconds") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be > 0")
    return value * _NS_PER_MS


_ENVIRONMENTS: Final = frozenset({"production", "staging", "development", "test"})


def _flag(env: Mapping[str, str], name: str) -> bool:
    """Explicit boolean, default False. Anything but true/false is a startup error."""
    raw = env.get(name)
    if raw is None or not raw.strip():
        return False
    value = raw.strip().lower()
    if value not in ("true", "false"):
        raise ConfigError(f"{name} must be 'true' or 'false'")
    return value == "true"


def _optional_path(env: Mapping[str, str], name: str) -> Path | None:
    raw = env.get(name)
    return Path(raw.strip()) if raw is not None and raw.strip() else None


@dataclass(frozen=True)
class MotorConfig:
    max_order_notional: Decimal
    max_session_notional: Decimal
    max_order_age_ns: int = _DEFAULT_MAX_AGE_MS * _NS_PER_MS
    max_clock_skew_ns: int = _DEFAULT_SKEW_MS * _NS_PER_MS
    # Fail closed: an unset environment is production (dev signing keys are then refused).
    environment: str = "production"
    # Aegis may sign SELL_SHORT; the motor only accepts it when this is explicitly enabled.
    allow_short_selling: bool = False
    # Directory for durable motor state (idempotency claims). Required in production.
    state_dir: Path | None = None

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def __post_init__(self) -> None:
        if self.environment not in _ENVIRONMENTS:
            raise ConfigError(f"environment must be one of {sorted(_ENVIRONMENTS)}")
        if self.max_order_notional <= 0 or self.max_session_notional <= 0:
            raise ConfigError("notional caps must be > 0")
        if self.max_session_notional < self.max_order_notional:
            raise ConfigError("session notional cap must be >= per-order notional cap")
        if self.max_order_age_ns <= 0 or self.max_clock_skew_ns < 0:
            raise ConfigError("age/skew windows are invalid")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> MotorConfig:
        return cls(
            max_order_notional=_positive_decimal(env, "MOTOR_MAX_ORDER_NOTIONAL_USD"),
            max_session_notional=_positive_decimal(env, "MOTOR_MAX_SESSION_NOTIONAL_USD"),
            max_order_age_ns=_positive_ms(env, "MOTOR_MAX_ORDER_AGE_MS", _DEFAULT_MAX_AGE_MS),
            max_clock_skew_ns=_positive_ms(env, "MOTOR_MAX_CLOCK_SKEW_MS", _DEFAULT_SKEW_MS),
            environment=env.get("MOTOR_ENV", "production").strip().lower() or "production",
            allow_short_selling=_flag(env, "MOTOR_SHORT_SELLING_ENABLED"),
            state_dir=_optional_path(env, "MOTOR_STATE_DIR"),
        )
