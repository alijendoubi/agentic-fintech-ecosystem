"""ALI-162 over a real grpc server on loopback: the generated AegisStub's
WatchKillSwitchState stream drives the motor's kill switch and the cancel sweep.

The Aegis side is a Python fake that serves the real aegis.proto service (the Rust Aegis is
not started here). Plaintext loopback: mTLS on this channel is covered by
test_server_integration.py's pattern and by Aegis's own grpc_mtls tests.
"""

from __future__ import annotations

import importlib
import queue
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from concurrent import futures
from types import ModuleType
from typing import Any

import grpc
import pytest

from execution_motor.aegis_channel import open_aegis_channel
from execution_motor.kill_watch import aegis_state_stream, build_kill_guard

from .conftest import PROTO_DIR
from .test_kill_watch import OpenOrdersBroker

_END = object()


@pytest.fixture(scope="module")
def aegis_grpc(tmp_path_factory: pytest.TempPathFactory) -> dict[str, ModuleType]:
    out = tmp_path_factory.mktemp("aegis_grpc_generated")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{PROTO_DIR}",
            f"--python_out={out}",
            f"--grpc_python_out={out}",
            *(str(f) for f in sorted(PROTO_DIR.glob("*.proto"))),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"protoc failed: {result.stderr}")
    sys.path.insert(0, str(out))
    try:
        return {
            "pb2": importlib.import_module("aegis_pb2"),
            "grpc": importlib.import_module("aegis_pb2_grpc"),
        }
    finally:
        sys.path.remove(str(out))


def wait_for(predicate: Callable[[], bool], timeout_s: float = 2.0) -> float:
    started = time.monotonic()
    while not predicate():
        if time.monotonic() - started > timeout_s:
            raise AssertionError("condition not met in time")
        time.sleep(0.005)
    return time.monotonic() - started


def test_real_stream_halts_cancels_resumes_and_fails_closed(
    aegis_grpc: dict[str, ModuleType],
) -> None:
    pb2, pb2_grpc = aegis_grpc["pb2"], aegis_grpc["grpc"]
    states: queue.Queue[Any] = queue.Queue()

    class FakeAegis(pb2_grpc.AegisServicer):  # type: ignore[misc, name-defined]
        def WatchKillSwitchState(self, request: Any, context: Any) -> Iterator[Any]:  # noqa: N802
            while context.is_active():
                try:
                    item = states.get(timeout=0.05)
                except queue.Empty:
                    continue
                if item is _END:
                    return
                yield item

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    pb2_grpc.add_AegisServicer_to_server(FakeAegis(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()

    broker = OpenOrdersBroker(open_ids=("o1",))
    channel = open_aegis_channel(f"127.0.0.1:{port}", None)
    stub = pb2_grpc.AegisStub(channel)
    kill, watcher = build_kill_guard(
        aegis_state_stream(stub, pb2.Empty),
        {broker.venue: broker},
        resweep_interval_s=30.0,
        reconnect_backoff_s=0.05,
    )
    try:
        assert kill.is_halted()  # no state yet: HARD
        watcher.start()

        states.put(pb2.KillSwitchState(effective_level=pb2.KILL_LEVEL_NORMAL, state_seq=1))
        wait_for(lambda: not kill.is_halted())
        assert broker.cancelled == []

        states.put(pb2.KillSwitchState(effective_level=pb2.KILL_LEVEL_LOGIC, state_seq=2))
        elapsed = wait_for(lambda: broker.cancelled == ["o1"], timeout_s=1.0)
        assert elapsed < 1.0  # spec §5.2: cancel-all issued within 1 s of the latch
        assert kill.is_halted()

        states.put(pb2.KillSwitchState(effective_level=pb2.KILL_LEVEL_NORMAL, state_seq=3))
        wait_for(lambda: not kill.is_halted())  # Aegis reset: the motor follows

        broker.open_ids.append("o2")
        server.stop(grace=None)  # Aegis goes away: the stream breaks
        wait_for(lambda: broker.cancelled == ["o1", "o2"])
        assert kill.is_halted()
    finally:
        watcher.stop()
        channel.close()
        server.stop(grace=None)
