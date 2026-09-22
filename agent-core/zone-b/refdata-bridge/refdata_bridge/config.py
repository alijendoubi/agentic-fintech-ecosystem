"""Environment-driven configuration for the reference-data bridge.

All values are validated once, at start-up, into an immutable ``Settings``.
Invalid configuration raises ``ConfigError`` and the process refuses to
start: with no bridge running, Aegis's C08/C18 controls keep failing (fail
closed), which is safe -- it is exactly the state before this service
existed.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SNAPSHOT_CHANNEL = "sensory:snapshots"
DEFAULT_REGIME_CHANNEL = "regime:labels"
DEFAULT_HEARTBEAT_PATH = "/tmp/refdata-bridge.heartbeat"  # noqa: S108 - container-local file

ENVIRONMENT_VAR = "ENVIRONMENT"
TLS_CA_VAR = "REFDATA_CLIENT_TLS_CA"
TLS_CERT_VAR = "REFDATA_CLIENT_TLS_CERT"
TLS_KEY_VAR = "REFDATA_CLIENT_TLS_KEY"


class ConfigError(ValueError):
    """Raised when an environment variable is missing or invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable runtime settings."""

    redis_url: str
    snapshot_channel: str
    regime_channel: str
    aegis_target: str
    proto_dir: Path | None
    tls_ca: Path | None
    tls_cert: Path | None
    tls_key: Path | None
    environment: str
    batch_interval_s: float
    max_batch_snapshots: int
    snapshot_stale_after_s: float
    regime_stale_after_s: float
    aegis_rpc_timeout_s: float
    health_host: str
    health_port: int
    heartbeat_path: Path
    log_level: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build settings from ``env`` (defaults to ``os.environ``)."""
        source = os.environ if env is None else env
        tls_ca, tls_cert, tls_key = _tls_paths(source)
        environment = _text(source, ENVIRONMENT_VAR, "development").lower()
        if environment == "production" and tls_ca is None:
            raise ConfigError(
                f"{ENVIRONMENT_VAR}=production refuses an insecure Aegis channel: set "
                f"{TLS_CA_VAR} (and {TLS_CERT_VAR}/{TLS_KEY_VAR} for mutual TLS)"
            )
        return cls(
            redis_url=_text(source, "REDIS_URL", "redis://localhost:6379"),
            snapshot_channel=_text(source, "SNAPSHOT_CHANNEL", DEFAULT_SNAPSHOT_CHANNEL),
            regime_channel=_text(source, "REGIME_CHANNEL", DEFAULT_REGIME_CHANNEL),
            aegis_target=_text(source, "AEGIS_TARGET", "localhost:50051"),
            proto_dir=_optional_path(source, "AFE_PROTO_DIR"),
            tls_ca=tls_ca,
            tls_cert=tls_cert,
            tls_key=tls_key,
            environment=environment,
            batch_interval_s=_float(source, "BATCH_INTERVAL_S", 1.0, 0.05, 300.0),
            max_batch_snapshots=_int(source, "MAX_BATCH_SNAPSHOTS", 64, 1, 512),
            snapshot_stale_after_s=_float(source, "SNAPSHOT_STALE_AFTER_S", 2.0, 0.01, 3600.0),
            regime_stale_after_s=_float(source, "REGIME_STALE_AFTER_S", 30.0, 0.01, 3600.0),
            aegis_rpc_timeout_s=_float(source, "AEGIS_RPC_TIMEOUT_S", 2.0, 0.1, 60.0),
            health_host=_text(source, "HEALTH_HOST", "0.0.0.0"),  # noqa: S104 - container-internal
            health_port=_int(source, "HEALTH_PORT", 8080, 1, 65535),
            heartbeat_path=Path(_text(source, "HEARTBEAT_PATH", DEFAULT_HEARTBEAT_PATH)),
            log_level=_text(source, "LOG_LEVEL", "info").upper(),
        )


def _optional_path(env: Mapping[str, str], name: str) -> Path | None:
    value = env.get(name, "").strip()
    return Path(value) if value else None


def _tls_paths(env: Mapping[str, str]) -> tuple[Path | None, Path | None, Path | None]:
    values = {name: env.get(name, "").strip() for name in (TLS_CA_VAR, TLS_CERT_VAR, TLS_KEY_VAR)}
    ca, cert, key = (Path(v) if v else None for v in values.values())
    if (cert is None) != (key is None):
        raise ConfigError(f"{TLS_CERT_VAR} and {TLS_KEY_VAR} must be set together")
    return ca, cert, key


def _text(env: Mapping[str, str], name: str, default: str) -> str:
    value = env.get(name, default).strip()
    if not value:
        raise ConfigError(f"{name} must not be empty")
    return value


def _int(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = env.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if not low <= value <= high:
        raise ConfigError(f"{name}={value} outside [{low}, {high}]")
    return value


def _float(env: Mapping[str, str], name: str, default: float, low: float, high: float) -> float:
    raw = env.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if not low <= value <= high:  # also rejects NaN
        raise ConfigError(f"{name}={value} outside [{low}, {high}]")
    return value
