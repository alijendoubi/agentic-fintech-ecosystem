"""Liveness state plus a stdlib HTTP server exposing `GET /health` (no extra dependencies).

`/health` is 200 while the runner loop keeps beating and 503 once it has stalled or stopped.
A deliberate halt is NOT unhealthy (the process is doing what the operator asked); it is
reported in the body so dashboards can show it.
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

HEALTH_PATH = "/health"


class HealthState:
    """Thread-safe counters and heartbeat, written by the runner, read by the HTTP thread."""

    def __init__(
        self, max_beat_age_s: float, monotonic: Callable[[], float] = time.monotonic
    ) -> None:
        if not max_beat_age_s > 0:
            raise ValueError("max_beat_age_s must be positive")
        self._max_age = max_beat_age_s
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._last_beat: float | None = None
        self._stopped = False
        self._halted = False
        self._counters: Counter[str] = Counter()

    def beat(self) -> None:
        with self._lock:
            self._last_beat = self._monotonic()

    def count(self, name: str) -> None:
        with self._lock:
            self._counters[name] += 1

    def set_halted(self, halted: bool) -> None:
        with self._lock:
            self._halted = halted

    def stop(self) -> None:
        with self._lock:
            self._stopped = True

    def snapshot(self) -> tuple[bool, dict[str, Any]]:
        """Return (healthy, body)."""
        with self._lock:
            age = None if self._last_beat is None else self._monotonic() - self._last_beat
            healthy = not self._stopped and age is not None and age <= self._max_age
            body: dict[str, Any] = {
                "status": "ok" if healthy else "unhealthy",
                "halted": self._halted,
                "last_beat_age_s": None if age is None else round(age, 3),
                "counters": dict(self._counters),
            }
        return healthy, body


def _handler_for(state: HealthState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self.path.split("?", 1)[0] != HEALTH_PATH:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            healthy, body = state.snapshot()
            payload = json.dumps(body).encode()
            self.send_response(HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return  # keep stdout for structured logs only

    return Handler


class HealthServer:
    """Runs `ThreadingHTTPServer` in a daemon thread. `port=0` picks a free port (tests)."""

    def __init__(self, state: HealthState, host: str = "127.0.0.1", port: int = 8080) -> None:
        self._server = ThreadingHTTPServer((host, port), _handler_for(state))
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="health-http", daemon=True
        )

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
