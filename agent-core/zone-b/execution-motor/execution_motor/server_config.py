"""Transport / broker / Aegis-client configuration for the gRPC server, from env.

Mirrors ``agent-core/zone-b/aegis/src/config.rs``'s fail-closed rules in Python:

* ``MOTOR_ENV`` unset => production.
* No TLS material => refuse to start unless ``MOTOR_INSECURE_DEV=1`` AND ``MOTOR_ENV`` is
  not production AND ``MOTOR_LISTEN_ADDR`` is explicitly set to a loopback address
  (plaintext is also refused at bind time off-loopback, see ``server.py``).
* No real broker credentials in production => refuse (``MOTOR_USE_MOCK_BROKER=1`` is
  dev-only, the same shape as Aegis's dev signer refusal).
* No Aegis client CA in production => refuse (execution reports must not leave the motor
  over an unauthenticated channel), mirroring ``cognitive-core``'s
  ``sinks.aegis_tls_from_env`` rule for the same ``AEGIS_CLIENT_TLS_*`` variables.

Business-policy config (notional caps, order age/skew windows, short-selling, state dir)
stays in ``config.MotorConfig``; this module is transport/process concerns only.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .errors import ConfigError

_DEFAULT_LISTEN: Final = "0.0.0.0:50061"
_ENVIRONMENTS: Final = frozenset({"production", "staging", "development", "test"})


@dataclass(frozen=True)
class TlsPaths:
    cert: Path
    key: Path
    client_ca: Path


@dataclass(frozen=True)
class AegisClientTls:
    """PEM file paths for the outbound Aegis channel. ``cert``/``key`` are both set (mTLS)
    or both None (server-auth-only TLS, trusting only ``ca``)."""

    ca: Path | None
    cert: Path | None
    key: Path | None


@dataclass(frozen=True)
class ServerConfig:
    environment: str
    listen_host: str
    listen_port: int
    tls: TlsPaths | None  # None => insecure dev (plaintext); never None in production
    aegis_target: str | None  # host:port for ReportExecution; None => reporting disabled
    aegis_tls: AegisClientTls | None
    use_mock_broker: bool
    attestation_keys_file: Path

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def listen_addr(self) -> str:
        return f"{self.listen_host}:{self.listen_port}"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ServerConfig:
        environment = env.get("MOTOR_ENV", "production").strip().lower() or "production"
        if environment not in _ENVIRONMENTS:
            raise ConfigError(f"MOTOR_ENV must be one of {sorted(_ENVIRONMENTS)}")
        raw_listen = env.get("MOTOR_LISTEN_ADDR", "").strip()
        listen_explicit = bool(raw_listen)
        host, port = _split_listen(raw_listen or _DEFAULT_LISTEN)
        tls = _parse_tls(env, environment, host, listen_explicit)
        use_mock = _parse_broker(env, environment)
        aegis_target = env.get("AEGIS_TARGET", "").strip() or None
        if environment == "production" and aegis_target is None:
            raise ConfigError(
                "MOTOR_ENV=production requires AEGIS_TARGET: without it the motor cannot "
                "watch Aegis's kill-switch state and would leave open orders live after a "
                "LOGIC/HARD trip"
            )
        aegis_tls = _parse_aegis_tls(env, environment)
        keys_file = _required_path(env, "MOTOR_ATTESTATION_KEYS_FILE")
        return cls(
            environment=environment,
            listen_host=host,
            listen_port=port,
            tls=tls,
            aegis_target=aegis_target,
            aegis_tls=aegis_tls,
            use_mock_broker=use_mock,
            attestation_keys_file=keys_file,
        )


def _split_listen(raw: str) -> tuple[str, int]:
    body = raw.strip()
    if body.startswith("["):  # IPv6 literal, e.g. [::1]:50061
        host_part, _, port_part = body.rpartition(":")
        host = host_part.strip("[]")
    else:
        host, _, port_part = body.rpartition(":")
    if not host or not port_part.isdigit():
        raise ConfigError(f"MOTOR_LISTEN_ADDR {raw!r} is not host:port")
    return host, int(port_part)


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _required_path(env: Mapping[str, str], name: str) -> Path:
    raw = env.get(name, "").strip()
    if not raw:
        raise ConfigError(f"{name} is required")
    return Path(raw)


def _optional_path(env: Mapping[str, str], name: str) -> Path | None:
    raw = env.get(name, "").strip()
    return Path(raw) if raw else None


def _parse_tls(
    env: Mapping[str, str], environment: str, listen_host: str, listen_explicit: bool
) -> TlsPaths | None:
    insecure = env.get("MOTOR_INSECURE_DEV", "").strip() == "1"
    if insecure:
        if environment == "production":
            raise ConfigError(
                "MOTOR_INSECURE_DEV=1 is refused when MOTOR_ENV is production (or unset)"
            )
        if not listen_explicit or not _is_loopback(listen_host):
            raise ConfigError(
                "MOTOR_INSECURE_DEV=1 (plaintext, no authentication) requires an explicit "
                "loopback MOTOR_LISTEN_ADDR such as 127.0.0.1:50061"
            )
        return None
    return TlsPaths(
        cert=_required_path(env, "MOTOR_TLS_CERT"),
        key=_required_path(env, "MOTOR_TLS_KEY"),
        client_ca=_required_path(env, "MOTOR_TLS_CLIENT_CA"),
    )


def _parse_aegis_tls(env: Mapping[str, str], environment: str) -> AegisClientTls | None:
    ca = _optional_path(env, "AEGIS_CLIENT_TLS_CA")
    cert = _optional_path(env, "AEGIS_CLIENT_TLS_CERT")
    key = _optional_path(env, "AEGIS_CLIENT_TLS_KEY")
    if (cert is None) != (key is None):
        raise ConfigError("AEGIS_CLIENT_TLS_CERT and AEGIS_CLIENT_TLS_KEY must be set together")
    if environment == "production" and ca is None:
        raise ConfigError(
            "MOTOR_ENV=production refuses an insecure Aegis reporting channel: set "
            "AEGIS_CLIENT_TLS_CA (and AEGIS_CLIENT_TLS_CERT/AEGIS_CLIENT_TLS_KEY for mTLS)"
        )
    if ca is None and cert is None:
        return None
    return AegisClientTls(ca=ca, cert=cert, key=key)


def _parse_broker(env: Mapping[str, str], environment: str) -> bool:
    mock = env.get("MOTOR_USE_MOCK_BROKER", "").strip() == "1"
    has_creds = bool(env.get("ALPACA_API_KEY", "").strip()) and bool(
        env.get("ALPACA_SECRET_KEY", "").strip()
    )
    if mock:
        if environment == "production":
            raise ConfigError(
                "MOTOR_USE_MOCK_BROKER=1 is refused when MOTOR_ENV is production (or unset)"
            )
        return True
    if has_creds:
        return False
    if environment == "production":
        raise ConfigError(
            "ALPACA_API_KEY/ALPACA_SECRET_KEY are required in production "
            "(or set MOTOR_USE_MOCK_BROKER=1 outside production)"
        )
    raise ConfigError(
        "no broker configured: set ALPACA_API_KEY/ALPACA_SECRET_KEY, or "
        "MOTOR_USE_MOCK_BROKER=1 outside production"
    )
