"""Gateway configuration from env. Fail closed: anything missing or odd refuses to start.

* ``GATEWAY_ENV`` unset => production (same convention as ``MOTOR_ENV``/``AEGIS_ENV``).
* mTLS is mandatory in every environment: there is no plaintext mode.
* The client allow-list, the attestation key registry and the broker credentials are
  required; nothing has a default.
* The broker endpoint is the pinned Alpaca paper host unless BOTH live opt-ins are set
  (``alpaca.resolve_base_url``, same rule the motor used before ADR-004).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from execution_motor.errors import ConfigError

from .alpaca import AlpacaCredentials, resolve_base_url

_NS_PER_MS: Final = 1_000_000
_DEFAULT_LISTEN: Final = "0.0.0.0:50071"
_ENVIRONMENTS: Final = frozenset({"production", "staging", "development", "test"})
# Spec section 7: expires_at_ns PROPOSED = decided_at_ns + 5 s. An attestation that claims a
# longer life is refused, which also bounds the in-memory replay window (see authorize.py).
_DEFAULT_MAX_TTL_MS: Final = 5_000
# Same default as the motor's MOTOR_MAX_CLOCK_SKEW_MS (execution_motor/config.py).
_DEFAULT_SKEW_MS: Final = 1_000
# Same per-request timeout the motor's Alpaca client used before ADR-004 (5 s).
_DEFAULT_BROKER_TIMEOUT_MS: Final = 5_000


@dataclass(frozen=True)
class TlsPaths:
    cert: Path
    key: Path
    client_ca: Path


@dataclass(frozen=True)
class GatewayConfig:
    environment: str
    listen_addr: str
    tls: TlsPaths
    allowed_client_cns: frozenset[str]
    attestation_keys_file: Path
    max_ttl_ns: int
    max_clock_skew_ns: int
    broker_timeout_s: float
    base_url: str
    credentials: AlpacaCredentials = field(repr=False)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> GatewayConfig:
        environment = env.get("GATEWAY_ENV", "production").strip().lower() or "production"
        if environment not in _ENVIRONMENTS:
            raise ConfigError(f"GATEWAY_ENV must be one of {sorted(_ENVIRONMENTS)}")
        listen = env.get("GATEWAY_LISTEN_ADDR", "").strip() or _DEFAULT_LISTEN
        host, _, port = listen.rpartition(":")
        if not host or not port.isdigit():
            raise ConfigError(f"GATEWAY_LISTEN_ADDR {listen!r} is not host:port")
        cns = frozenset(
            cn.strip() for cn in env.get("GATEWAY_ALLOWED_CLIENT_CNS", "").split(",") if cn.strip()
        )
        if not cns:
            raise ConfigError(
                "GATEWAY_ALLOWED_CLIENT_CNS is required (comma-separated client certificate "
                "CNs, normally just execution-motor)"
            )
        return cls(
            environment=environment,
            listen_addr=listen,
            tls=TlsPaths(
                cert=_required_path(env, "GATEWAY_TLS_CERT"),
                key=_required_path(env, "GATEWAY_TLS_KEY"),
                client_ca=_required_path(env, "GATEWAY_TLS_CLIENT_CA"),
            ),
            allowed_client_cns=cns,
            attestation_keys_file=_required_path(env, "GATEWAY_ATTESTATION_KEYS_FILE"),
            max_ttl_ns=_positive_ms(env, "GATEWAY_MAX_ATTESTATION_TTL_MS", _DEFAULT_MAX_TTL_MS),
            max_clock_skew_ns=_positive_ms(env, "GATEWAY_MAX_CLOCK_SKEW_MS", _DEFAULT_SKEW_MS),
            broker_timeout_s=_positive_ms(
                env, "GATEWAY_BROKER_TIMEOUT_MS", _DEFAULT_BROKER_TIMEOUT_MS
            )
            / 1e9,
            base_url=resolve_base_url(env.get("ALPACA_BASE_URL"), env),
            credentials=AlpacaCredentials.from_env(env),
        )


def _required_path(env: Mapping[str, str], name: str) -> Path:
    raw = env.get(name, "").strip()
    if not raw:
        raise ConfigError(f"{name} is required")
    return Path(raw)


def _positive_ms(env: Mapping[str, str], name: str, default_ms: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default_ms * _NS_PER_MS
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer number of milliseconds") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be > 0")
    return value * _NS_PER_MS
