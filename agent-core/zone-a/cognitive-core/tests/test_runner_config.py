from __future__ import annotations

import pytest
from cognitive_core.config import ConfigError
from cognitive_core.runner_config import RunnerSettings


def test_defaults_are_fail_closed() -> None:
    s = RunnerSettings.from_env({})
    assert s.order_quantity == 0.0  # every debate abstains until a sizer exists
    assert s.emit_abstain is False and s.memory_enabled is False and s.memory_required is True
    assert s.sink == "grpc" and s.symbols == frozenset()
    assert (s.health_host, s.health_port) == ("127.0.0.1", 8080)
    assert (s.aegis_host, s.aegis_port) == ("aegis", 50051)
    assert s.redis_url == "redis://localhost:6379"
    assert s.snapshot_channel == "sensory:snapshots"
    assert s.max_beat_age_s == 30.0


def test_compose_style_environment() -> None:
    s = RunnerSettings.from_env(
        {
            "REDIS_HOST": "redis",
            "ZONE_B_GRPC_HOST": "aegis",
            "ZONE_B_GRPC_PORT": "50051",
            "COGNITIVE_SYMBOLS": "AAPL, MSFT ,BRK.B",
            "COGNITIVE_SINK": "log",
            "COGNITIVE_MEMORY_ENABLED": "true",
            "AFE_PROTO_DIR": "/app/generated",
            "COGNITIVE_HEARTBEAT_INTERVAL_S": "5",
        }
    )
    assert s.redis_url == "redis://redis:6379"
    assert s.symbols == frozenset({"AAPL", "MSFT", "BRK.B"})
    assert s.sink == "log" and s.memory_enabled is True
    assert s.proto_dir == "/app/generated"
    assert s.max_beat_age_s == 50.0


def test_redis_url_wins_over_host_and_port() -> None:
    s = RunnerSettings.from_env({"REDIS_URL": "rediss://cache:6380/1", "REDIS_HOST": "ignored"})
    assert s.redis_url == "rediss://cache:6380/1"


def test_reads_process_environment_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COGNITIVE_ORDER_QUANTITY", "3")
    assert RunnerSettings.from_env().order_quantity == 3.0


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("COGNITIVE_HEALTH_HOST", "http://x"),
        ("COGNITIVE_HEALTH_PORT", "70000"),
        ("COGNITIVE_ORDER_QUANTITY", "-1"),
        ("COGNITIVE_ORDER_QUANTITY", "nan"),
        ("COGNITIVE_ORDER_QUANTITY", "inf"),
        ("COGNITIVE_STRATEGY_ID", ""),
        ("COGNITIVE_MIN_DEBATE_INTERVAL_S", "-1"),
        ("COGNITIVE_MAX_CONTEXT_AGE_S", "0"),
        ("COGNITIVE_SYMBOLS", "aapl"),
        ("COGNITIVE_SYMBOLS", "AAPL;MSFT"),
        ("COGNITIVE_EMIT_ABSTAIN", "sometimes"),
        ("COGNITIVE_SINK", "kafka"),
        ("COGNITIVE_MEMORY_ENABLED", "2"),
        ("COGNITIVE_MEMORY_TOP_K", "0"),
        ("COGNITIVE_MEMORY_TOP_K", "6"),
        ("COGNITIVE_MEMORY_TIMEOUT_S", "0"),
        ("COGNITIVE_HEARTBEAT_INTERVAL_S", "0"),
        ("REDIS_URL", "http://cache"),
        ("REDIS_HOST", "bad host"),
        ("REDIS_PORT", "x"),
        ("SNAPSHOT_CHANNEL", ""),
        ("ZONE_B_GRPC_HOST", "aegis:50051"),
        ("ZONE_B_GRPC_PORT", "0"),
        ("COGNITIVE_AEGIS_TIMEOUT_S", "0"),
    ],
)
def test_invalid_values_fail_closed(name: str, value: str) -> None:
    with pytest.raises(ConfigError):
        RunnerSettings.from_env({name: value})
