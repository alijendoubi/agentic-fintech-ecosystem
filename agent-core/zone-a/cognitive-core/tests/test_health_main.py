from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from cognitive_core import __main__ as entrypoint
from cognitive_core.health import HealthServer, HealthState
from cognitive_core.healthcheck import probe
from cognitive_core.service import CycleOutcome
from cognitive_core.sinks import LogSink
from cognitive_core.sources import RedisSnapshotSource, StaticSource
from cognitive_core.tests.fakes import BLUE_JSON, JUDGE_JSON, RED_JSON, SUMMARY, ScriptedClient
from cognitive_core.tests.runner_fakes import NOW_NS, make_harness, snapshot_json

# -- health state and server --------------------------------------------------------


def test_health_state_needs_a_recent_beat() -> None:
    now = [100.0]
    state = HealthState(10.0, monotonic=lambda: now[0])
    assert state.snapshot()[0] is False  # never beat
    state.beat()
    now[0] = 105.0
    assert state.snapshot()[0] is True
    now[0] = 111.0
    healthy, body = state.snapshot()
    assert healthy is False and body["last_beat_age_s"] == 11.0


def test_health_state_stop_and_halt_and_counters() -> None:
    state = HealthState(10.0)
    state.beat()
    state.count("emitted")
    state.count("emitted")
    state.set_halted(True)
    healthy, body = state.snapshot()
    assert healthy is True and body["halted"] is True  # a deliberate halt is not unhealthy
    assert body["counters"] == {"emitted": 2}
    state.stop()
    assert state.snapshot()[0] is False


def test_health_state_rejects_non_positive_age() -> None:
    with pytest.raises(ValueError, match="max_beat_age_s"):
        HealthState(0)


def _get(port: int, path: str = "/health") -> tuple[int, dict[str, Any] | None]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        body = err.read()
        return err.code, json.loads(body) if body.startswith(b"{") else None


def test_health_endpoint_reports_200_then_503_and_404_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = HealthState(10.0)
    server = HealthServer(state, "127.0.0.1", 0)
    server.start()
    try:
        assert _get(server.port)[0] == 503  # no beat yet
        state.beat()
        status, body = _get(server.port)
        assert status == 200 and body is not None and body["status"] == "ok"
        assert _get(server.port, "/health?x=1")[0] == 200
        assert _get(server.port, "/other")[0] == 404
        monkeypatch.setenv("COGNITIVE_HEALTH_PORT", str(server.port))
        assert probe(str(server.port)) is True
        state.stop()
        assert probe(str(server.port)) is False  # 503 fails the container check
    finally:
        server.stop()
    assert probe(str(server.port)) is False  # connection refused


def test_healthcheck_main_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    from cognitive_core import healthcheck

    monkeypatch.setenv("COGNITIVE_HEALTH_PORT", "1")
    assert healthcheck.main() == 1
    monkeypatch.setattr(healthcheck, "probe", lambda port: True)
    assert healthcheck.main() == 0


# -- sources ------------------------------------------------------------------------


class _FakePubSub:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self._messages = messages
        self.subscribed: list[str] = []
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.subscribed.append(channel)

    async def listen(self) -> Any:
        for message in self._messages:
            yield message

    async def aclose(self) -> None:
        self.closed = True


class _FakeRedis:
    def __init__(self, pubsub: _FakePubSub) -> None:
        self._pubsub = pubsub
        self.closed = False

    def pubsub(self) -> _FakePubSub:
        return self._pubsub

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_redis_source_yields_only_data_messages_and_cleans_up() -> None:
    pubsub = _FakePubSub(
        [{"type": "subscribe", "data": 1}, {"type": "message", "data": b"payload"}]
    )
    client = _FakeRedis(pubsub)
    seen: list[str] = []

    def factory(url: str) -> _FakeRedis:
        seen.append(url)
        return client

    source = RedisSnapshotSource("redis://r:6379", "sensory:snapshots", factory)
    assert [m async for m in source.messages()] == [b"payload"]
    assert seen == ["redis://r:6379"] and pubsub.subscribed == ["sensory:snapshots"]
    assert pubsub.closed and client.closed


@pytest.mark.asyncio
async def test_static_source_replays_messages() -> None:
    assert [m async for m in StaticSource(["a", b"b"]).messages()] == ["a", b"b"]


# -- composition root ---------------------------------------------------------------

VECTOR_DB_DIR = Path(__file__).resolve().parents[2] / "vector-db"
_ENV = {
    "COGNITIVE_SINK": "log",
    "COGNITIVE_ORDER_QUANTITY": "10",
    "COGNITIVE_MIN_DEBATE_INTERVAL_S": "0",
    "COGNITIVE_HEALTH_PORT": "0",
    "COGNITIVE_HEARTBEAT_INTERVAL_S": "0.05",
}


def _graph_factory(_settings: Any) -> Any:
    from cognitive_core.graph import build_debate_graph
    from cognitive_core.tests.fakes import make_settings

    return build_debate_graph(
        make_settings(),
        blue_client=ScriptedClient(BLUE_JSON),
        red_client=ScriptedClient(RED_JSON),
        judge_client=ScriptedClient(JUDGE_JSON),
        compression_client=ScriptedClient(SUMMARY),
    )


@pytest.mark.asyncio
async def test_amain_exits_1_when_the_source_ends_without_a_stop() -> None:
    code = await entrypoint.amain(
        _ENV, graph_factory=_graph_factory, source=StaticSource([snapshot_json()])
    )
    assert code == entrypoint.EXIT_SOURCE


@pytest.mark.asyncio
async def test_amain_exits_1_when_the_source_fails() -> None:
    class Broken:
        async def messages(self) -> Any:
            raise ConnectionError("redis gone")
            yield  # pragma: no cover

    code = await entrypoint.amain(_ENV, graph_factory=_graph_factory, source=Broken())
    assert code == entrypoint.EXIT_SOURCE


@pytest.mark.asyncio
async def test_amain_exits_0_on_stop() -> None:
    stop = asyncio.Event()

    class Idle:
        async def messages(self) -> Any:
            await asyncio.Event().wait()
            yield ""  # pragma: no cover

    task = asyncio.create_task(
        entrypoint.amain(_ENV, graph_factory=_graph_factory, source=Idle(), stop=stop)
    )
    await asyncio.sleep(0.2)
    stop.set()
    assert await asyncio.wait_for(task, timeout=5) == entrypoint.EXIT_OK


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "env",
    [
        {"COGNITIVE_SINK": "kafka"},  # invalid runner config
        {**_ENV, "COGNITIVE_OMEGA_THRESHOLD": "0.1"},  # invalid debate config
        {**_ENV, "COGNITIVE_SINK": "grpc", "AFE_PROTO_DIR": "/nonexistent/protos"},  # no stubs
    ],
)
async def test_amain_refuses_to_start_on_bad_configuration(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_stubs(_directory: object) -> dict[str, Any]:
        raise ImportError("generated stubs not found")

    monkeypatch.setattr(entrypoint, "load_generated_protos", no_stubs)
    assert await entrypoint.amain(env, graph_factory=_graph_factory) == entrypoint.EXIT_CONFIG


@pytest.mark.asyncio
async def test_amain_fails_startup_when_memory_is_enabled_but_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(VECTOR_DB_DIR))
    env = {
        **_ENV,
        "COGNITIVE_MEMORY_ENABLED": "true",
        "CHROMA_HOST": "127.0.0.1",
        "CHROMA_PORT": "1",
        "CHROMA_CONNECT_TIMEOUT_S": "5",
    }
    assert await entrypoint.amain(env, graph_factory=_graph_factory) == entrypoint.EXIT_CONFIG


def test_build_sink_log_and_memory_disabled_paths() -> None:
    from cognitive_core.runner_config import RunnerSettings

    settings = RunnerSettings.from_env({"COGNITIVE_SINK": "log"})
    assert isinstance(entrypoint.build_sink(settings), LogSink)
    assert entrypoint.build_memory({}, settings) == (None, None)


def test_build_sink_grpc_uses_generated_stubs(
    generated_dir: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cognitive_core.runner_config import RunnerSettings
    from cognitive_core.sinks import AegisGrpcSink

    monkeypatch.setattr(entrypoint, "open_aegis_channel", lambda host, port: object())
    settings = RunnerSettings.from_env({"AFE_PROTO_DIR": str(generated_dir)})
    monkeypatch.setattr(entrypoint, "load_generated_protos", lambda _d: _stub_protos(generated_dir))
    assert isinstance(entrypoint.build_sink(settings), AegisGrpcSink)


def _stub_protos(generated_dir: Any) -> dict[str, Any]:
    from cognitive_core.sinks import load_generated_protos

    real = load_generated_protos(generated_dir)

    class _Grpc:
        AegisStub = staticmethod(lambda channel: object())

    return {**real, "aegis_pb2_grpc": _Grpc}


def test_main_runs_asyncio_and_returns_the_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_amain(env: Any, **_: Any) -> int:
        return 7

    monkeypatch.setattr(entrypoint, "amain", fake_amain)
    assert entrypoint.main(env={}) == 7


@pytest.mark.asyncio
async def test_halted_runner_keeps_beating_and_reports_halted() -> None:
    h = make_harness(env={"COGNITIVE_HALT": "1"})
    assert await h.runner.handle_message(snapshot_json()) is CycleOutcome.HALTED
    h.health.beat()
    healthy, body = h.health.snapshot()
    assert healthy and body["halted"] and NOW_NS
