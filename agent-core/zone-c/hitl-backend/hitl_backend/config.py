"""Environment configuration, validated once at startup (fail closed on anything missing/bad)."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

MIN_SECRET_LENGTH = 32
#: Same fragments as the terminal (hitl-interface/src/lib/config.ts) and ALI-21.
PLACEHOLDER_FRAGMENTS = (
    "change-me",
    "change_me",
    "changeme",
    "replace-me",
    "replaceme",
    "placeholder",
    "your-secret",
    "yoursecret",
    "example",
    "default",
    "password",
)


class ConfigError(ValueError):
    """Invalid or missing configuration; the service refuses to start."""


@dataclass(frozen=True)
class TlsFiles:
    ca: Path
    cert: Path
    key: Path


@dataclass(frozen=True)
class Settings:
    production: bool
    listen_host: str
    listen_port: int
    jwt_secret: str
    jwt_issuer: str | None
    jwt_audience: str | None
    service_token: str | None
    aegis_target: str
    aegis_identity: str
    aegis_tls: TlsFiles | None
    aegis_timeout_s: float
    motor_target: str | None
    motor_tls: TlsFiles | None
    motor_timeout_s: float
    four_eyes_quantity_threshold: Decimal
    four_eyes_notional_threshold_usd: Decimal | None
    cooling_period_s: float
    audit_actor: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        production = env.get("HITL_ENV", "production").strip().lower() in ("", "production")
        secret = env.get("HITL_JWT_SECRET", "")
        _check_secret(secret)
        issuer = env.get("HITL_JWT_ISSUER", "").strip() or None
        audience = env.get("HITL_JWT_AUDIENCE", "").strip() or None
        if production and (issuer is None or audience is None):
            raise ConfigError("HITL_JWT_ISSUER and HITL_JWT_AUDIENCE are required in production")
        host, port = _listen(env.get("HITL_BACKEND_LISTEN", "127.0.0.1:8090"))
        aegis_target = env.get("AEGIS_TARGET", "").strip()
        if not aegis_target:
            raise ConfigError("AEGIS_TARGET (host:port) is required")
        aegis_identity = env.get("HITL_AEGIS_IDENTITY", "").strip()
        if not aegis_identity:
            raise ConfigError(
                "HITL_AEGIS_IDENTITY is required: this service's certificate CN, which Aegis "
                "requires as ResolveHold.operator_id (role hold-resolver in identities.json)"
            )
        aegis_tls = _tls(env, "HITL_AEGIS_TLS")
        motor_target = env.get("MOTOR_TARGET", "").strip() or None
        motor_tls = _tls(env, "HITL_MOTOR_TLS")
        if production and (aegis_tls is None or (motor_target and motor_tls is None)):
            raise ConfigError("production requires mTLS material for Aegis (and the motor)")
        if production and motor_target is None:
            raise ConfigError(
                "MOTOR_TARGET is required in production (released holds must execute)"
            )
        token = env.get("HITL_API_TOKEN", "").strip() or None
        if token is not None and len(token) < 16:
            raise ConfigError("HITL_API_TOKEN must be at least 16 characters when set")
        return cls(
            production=production,
            listen_host=host,
            listen_port=port,
            jwt_secret=secret,
            jwt_issuer=issuer,
            jwt_audience=audience,
            service_token=token,
            aegis_target=aegis_target,
            aegis_identity=aegis_identity,
            aegis_tls=aegis_tls,
            aegis_timeout_s=_positive(env, "HITL_AEGIS_TIMEOUT_S", "2"),
            motor_target=motor_target,
            motor_tls=motor_tls,
            motor_timeout_s=_positive(env, "HITL_MOTOR_TIMEOUT_S", "2"),
            four_eyes_quantity_threshold=_decimal(env, "HITL_FOUR_EYES_QUANTITY_THRESHOLD", "0"),
            four_eyes_notional_threshold_usd=_optional_decimal(
                env, "HITL_FOUR_EYES_NOTIONAL_THRESHOLD_USD"
            ),
            cooling_period_s=_non_negative(env, "HITL_COOLING_PERIOD_S", "0"),
            audit_actor=env.get("HITL_AUDIT_ACTOR", "hitl-backend").strip() or "hitl-backend",
        )


def _check_secret(secret: str) -> None:
    lowered = secret.lower()
    if len(secret) < MIN_SECRET_LENGTH:
        raise ConfigError(f"HITL_JWT_SECRET must be at least {MIN_SECRET_LENGTH} characters")
    if any(fragment in lowered for fragment in PLACEHOLDER_FRAGMENTS):
        raise ConfigError("HITL_JWT_SECRET looks like a placeholder")


def _listen(raw: str) -> tuple[str, int]:
    host, _, port = raw.strip().rpartition(":")
    try:
        number = int(port)
    except ValueError as exc:
        raise ConfigError("HITL_BACKEND_LISTEN must be host:port") from exc
    if not host or not 0 < number < 65536:
        raise ConfigError("HITL_BACKEND_LISTEN must be host:port")
    return host.strip("[]"), number


def _tls(env: Mapping[str, str], prefix: str) -> TlsFiles | None:
    names = [f"{prefix}_CA", f"{prefix}_CERT", f"{prefix}_KEY"]
    values = [env.get(n, "").strip() for n in names]
    if not any(values):
        return None
    if not all(values):
        raise ConfigError(f"set all of {', '.join(names)} or none")
    return TlsFiles(Path(values[0]), Path(values[1]), Path(values[2]))


def _float(env: Mapping[str, str], name: str, default: str) -> float:
    raw = env.get(name, default).strip() or default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if not math.isfinite(value):
        raise ConfigError(f"{name} must be finite")
    return value


def _positive(env: Mapping[str, str], name: str, default: str) -> float:
    value = _float(env, name, default)
    if value <= 0:
        raise ConfigError(f"{name} must be positive")
    return value


def _non_negative(env: Mapping[str, str], name: str, default: str) -> float:
    value = _float(env, name, default)
    if value < 0:
        raise ConfigError(f"{name} must be >= 0")
    return value


def _decimal(env: Mapping[str, str], name: str, default: str) -> Decimal:
    raw = env.get(name, default).strip() or default
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ConfigError(f"{name} must be a decimal number") from exc
    if not value.is_finite() or value < 0:
        raise ConfigError(f"{name} must be a finite number >= 0")
    return value


def _optional_decimal(env: Mapping[str, str], name: str) -> Decimal | None:
    return _decimal(env, name, "0") if env.get(name, "").strip() else None
