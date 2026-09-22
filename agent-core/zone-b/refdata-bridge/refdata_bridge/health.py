"""Health state + a tiny stdlib HTTP ``/health`` endpoint for Docker HEALTHCHECK.

Reports the last successful push time so a stalled bridge (Redis down, Aegis
down, or the loop wedged) is visibly unhealthy instead of looking fine forever.
Also touches a heartbeat file (same convention as
``agent-core/zone-a/regime-detector/regime_detector/healthcheck.py``) so a
file-based HEALTHCHECK works too if the HTTP port is not exposed.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

#: A push cycle older than this is considered stalled by ``/health``.
DEFAULT_MAX_SILENCE_S = 30.0


class HealthState:
    """Mutable, lock-protected. ``clock`` (wall-clock seconds) is injectable for tests."""

    def __init__(
        self,
        max_silence_s: float = DEFAULT_MAX_SILENCE_S,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.max_silence_s = max_silence_s
        self._clock = clock
        self._lock = threading.Lock()
        self._last_success_ns: int | None = None
        self._last_attempt_ns: int | None = None
        self._last_error: str | None = None

    def record_success(self, now_ns: int) -> None:
        with self._lock:
            self._last_success_ns = now_ns
            self._last_attempt_ns = now_ns
            self._last_error = None

    def record_failure(self, now_ns: int, error: str) -> None:
        with self._lock:
            self._last_attempt_ns = now_ns
            self._last_error = error

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            last_success_ns = self._last_success_ns
            last_attempt_ns = self._last_attempt_ns
            last_error = self._last_error
        now = self._clock()
        age_s = None if last_success_ns is None else now - last_success_ns / 1e9
        healthy = last_success_ns is not None and age_s is not None and age_s <= self.max_silence_s
        return {
            "healthy": healthy,
            "last_success_push_ns": last_success_ns,
            "last_attempt_push_ns": last_attempt_ns,
            "last_error": last_error,
            "age_s": age_s,
        }


def touch_heartbeat(path: Path) -> None:
    try:
        path.touch()
    except OSError as exc:
        log.warning("heartbeat_write_failed", path=str(path), error=str(exc))


def _make_handler(state: HealthState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
            pass  # structured logging elsewhere; keep stdout quiet

        def do_GET(self) -> None:  # noqa: N802 - stdlib method name
            if self.path != "/health":
                self.send_response(404)
                self.end_headers()
                return
            snapshot = state.snapshot()
            body = json.dumps(snapshot).encode("utf-8")
            status = 200 if snapshot["healthy"] else 503
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def start_health_server(host: str, port: int, state: HealthState) -> ThreadingHTTPServer:
    """Starts a background daemon-thread HTTP server serving ``GET /health``."""
    server = ThreadingHTTPServer((host, port), _make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="health-http")
    thread.start()
    log.info("health_server_listening", host=host, port=port)
    return server
