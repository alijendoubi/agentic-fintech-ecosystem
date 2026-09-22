"""Environment settings for the runner service (separate from the debate `CognitiveSettings`).

Variable names follow agent-core/infrastructure/docker-compose.yml where it defines them
(`ZONE_B_GRPC_HOST/PORT`, `REDIS_HOST`, `CHROMA_HOST/PORT`). Every invalid value raises
`ConfigError`, so a bad deployment refuses to start instead of running with a guess.

    COGNITIVE_HEALTH_HOST / _PORT      127.0.0.1 / 8080
    COGNITIVE_ORDER_QUANTITY           0 outside production (every debate abstains, see below);
                                       REQUIRED and must be > 0 when ENVIRONMENT=production
    COGNITIVE_STRATEGY_ID              AFE-STRATEGY-001 (TODO(owner): confirm)
    COGNITIVE_MIN_DEBATE_INTERVAL_S    60   per-symbol cooldown (LLM cost / rate control)
    COGNITIVE_MAX_CONTEXT_AGE_S        5    drop snapshots older than this
    COGNITIVE_SYMBOLS                  comma list; empty = all (Aegis still enforces its own)
    COGNITIVE_EMIT_ABSTAIN             false: only actionable signals go to the sink
    COGNITIVE_SINK                     grpc | log
    COGNITIVE_MEMORY_ENABLED           false; when true the composition root builds the
                                       vector-memory adapter (needs afe_vector_memory)
    COGNITIVE_MEMORY_REQUIRED          true: memory outage skips the debate (fail closed)
    COGNITIVE_MEMORY_TOP_K / _TIMEOUT_S  3 / 0.3
    COGNITIVE_HEARTBEAT_INTERVAL_S     1.0
    REDIS_URL | REDIS_HOST + REDIS_PORT   redis://localhost:6379
    SNAPSHOT_CHANNEL                   sensory:snapshots
    ZONE_B_GRPC_HOST / _PORT           aegis / 50051
    COGNITIVE_AEGIS_TIMEOUT_S          1.0
    MOTOR_TARGET                       execution-motor:50052 (host:port for the execution-motor
                                       relay; see sinks.py "Known limitations")
    COGNITIVE_MOTOR_TIMEOUT_S          1.0
    AFE_PROTO_DIR                      directory holding the generated *_pb2 modules

Position sizing: the cognitive core has no portfolio or risk state, so it never invents a
real quantity — building a real risk-based position sizer is out of scope here (a
strategy/risk decision, not something an LLM debate should decide unilaterally).
`COGNITIVE_ORDER_QUANTITY` is a fixed placeholder (TODO(owner): replace with a real position
sizer). Outside production it defaults to 0, so every signal abstains unless a caller opts in
(e.g. `tests/runner_fakes.py` sets a TEST-ONLY fixed quantity of 10 for tests that expect an
actionable signal — that value is not a sizing policy, just a fixture constant). In
production (`ENVIRONMENT=production`) `COGNITIVE_ORDER_QUANTITY` is REQUIRED: unset or <= 0
raises `ConfigError` at startup rather than silently trading nothing (or, if this default were
ever changed, silently trading an arbitrary fixed size).
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from .config import ConfigError
from .sinks import DEFAULT_MOTOR_TARGET

_SYMBOL = re.compile(r"[A-Z0-9.\-]{1,32}")
_HOST = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?")
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
MAX_PORT = 65_535
ENVIRONMENT_VAR = "ENVIRONMENT"

SinkKind = Literal["grpc", "log"]


def _read[T](source: Mapping[str, str], name: str, default: str, parse: Callable[[str], T]) -> T:
    raw = source.get(name, default).strip()
    try:
        return parse(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is invalid: {exc}") from exc


def _bool(raw: str) -> bool:
    lowered = raw.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ValueError("must be one of true/false/1/0/yes/no/on/off")


def _int(low: int, high: int) -> Callable[[str], int]:
    def parse(raw: str) -> int:
        value = int(raw)
        if not low <= value <= high:
            raise ValueError(f"must be in [{low}, {high}]")
        return value

    return parse


def _float(low: float, high: float) -> Callable[[str], float]:
    def parse(raw: str) -> float:
        value = float(raw)
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"must be a finite number in [{low}, {high}]")
        return value

    return parse


def _host(raw: str) -> str:
    if _HOST.fullmatch(raw) is None:
        raise ValueError("must be a bare hostname or IPv4 address")
    return raw


def _text(raw: str) -> str:
    if not raw:
        raise ValueError("must not be empty")
    return raw


def _symbols(raw: str) -> frozenset[str]:
    items = [part.strip() for part in raw.split(",") if part.strip()]
    for item in items:
        if _SYMBOL.fullmatch(item) is None:
            raise ValueError(f"{item!r} is not a valid symbol")
    return frozenset(items)


def _sink(raw: str) -> SinkKind:
    if raw == "grpc":
        return "grpc"
    if raw == "log":
        return "log"
    raise ValueError("must be 'grpc' or 'log'")


def _redis_url(source: Mapping[str, str]) -> str:
    url = source.get("REDIS_URL", "").strip()
    if url:
        if not url.startswith(("redis://", "rediss://")):
            raise ConfigError("REDIS_URL must start with redis:// or rediss://")
        return url
    host = _read(source, "REDIS_HOST", "localhost", _host)
    port = _read(source, "REDIS_PORT", "6379", _int(1, MAX_PORT))
    return f"redis://{host}:{port}"


@dataclass(frozen=True, slots=True)
class RunnerSettings:
    health_host: str
    health_port: int
    order_quantity: float
    strategy_id: str
    min_debate_interval_s: float
    max_context_age_s: float
    symbols: frozenset[str]
    emit_abstain: bool
    sink: SinkKind
    memory_enabled: bool
    memory_required: bool
    memory_top_k: int
    memory_timeout_s: float
    heartbeat_interval_s: float
    redis_url: str
    snapshot_channel: str
    aegis_host: str
    aegis_port: int
    aegis_timeout_s: float
    motor_target: str
    motor_timeout_s: float
    proto_dir: str | None

    @property
    def max_beat_age_s(self) -> float:
        """Health goes red if the loop has not ticked for this long."""
        return max(30.0, 10.0 * self.heartbeat_interval_s)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RunnerSettings:
        source = os.environ if env is None else env
        proto_dir = source.get("AFE_PROTO_DIR", "").strip() or None
        order_quantity = _read(source, "COGNITIVE_ORDER_QUANTITY", "0", _float(0.0, 1e9))
        production = source.get(ENVIRONMENT_VAR, "").strip().lower() == "production"
        if production and order_quantity <= 0.0:
            raise ConfigError(
                "ENVIRONMENT=production requires COGNITIVE_ORDER_QUANTITY to be set to a "
                "positive value (unset or 0 abstains every signal). This is a fixed "
                "placeholder quantity, not a real position sizer "
                "(TODO(owner): replace with one) — production must not start without an "
                "explicit choice here."
            )
        return cls(
            health_host=_read(source, "COGNITIVE_HEALTH_HOST", "127.0.0.1", _host),
            health_port=_read(source, "COGNITIVE_HEALTH_PORT", "8080", _int(0, MAX_PORT)),
            order_quantity=order_quantity,
            strategy_id=_read(source, "COGNITIVE_STRATEGY_ID", "AFE-STRATEGY-001", _text),
            min_debate_interval_s=_read(
                source, "COGNITIVE_MIN_DEBATE_INTERVAL_S", "60", _float(0.0, 86_400.0)
            ),
            max_context_age_s=_read(
                source, "COGNITIVE_MAX_CONTEXT_AGE_S", "5", _float(0.001, 3_600.0)
            ),
            symbols=_read(source, "COGNITIVE_SYMBOLS", "", _symbols),
            emit_abstain=_read(source, "COGNITIVE_EMIT_ABSTAIN", "false", _bool),
            sink=_read(source, "COGNITIVE_SINK", "grpc", _sink),
            memory_enabled=_read(source, "COGNITIVE_MEMORY_ENABLED", "false", _bool),
            memory_required=_read(source, "COGNITIVE_MEMORY_REQUIRED", "true", _bool),
            memory_top_k=_read(source, "COGNITIVE_MEMORY_TOP_K", "3", _int(1, 5)),
            memory_timeout_s=_read(source, "COGNITIVE_MEMORY_TIMEOUT_S", "0.3", _float(0.01, 2.0)),
            heartbeat_interval_s=_read(
                source, "COGNITIVE_HEARTBEAT_INTERVAL_S", "1.0", _float(0.05, 60.0)
            ),
            redis_url=_redis_url(source),
            snapshot_channel=_read(source, "SNAPSHOT_CHANNEL", "sensory:snapshots", _text),
            aegis_host=_read(source, "ZONE_B_GRPC_HOST", "aegis", _host),
            aegis_port=_read(source, "ZONE_B_GRPC_PORT", "50051", _int(1, MAX_PORT)),
            aegis_timeout_s=_read(source, "COGNITIVE_AEGIS_TIMEOUT_S", "1.0", _float(0.05, 30.0)),
            motor_target=_read(source, "MOTOR_TARGET", DEFAULT_MOTOR_TARGET, _text),
            motor_timeout_s=_read(
                source, "COGNITIVE_MOTOR_TIMEOUT_S", "1.0", _float(0.05, 30.0)
            ),
            proto_dir=proto_dir,
        )
