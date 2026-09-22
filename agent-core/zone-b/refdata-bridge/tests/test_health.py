"""``health.py``: last-push tracking and the ``/health`` HTTP endpoint."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from refdata_bridge.health import HealthState, start_health_server

_NS_PER_S = 1_000_000_000


def test_unhealthy_before_any_push() -> None:
    state = HealthState(clock=lambda: 1_000.0)
    snap = state.snapshot()
    assert snap["healthy"] is False
    assert snap["last_success_push_ns"] is None


def test_healthy_shortly_after_success() -> None:
    clock_value = [1_000.0]
    state = HealthState(max_silence_s=30.0, clock=lambda: clock_value[0])
    state.record_success(int(clock_value[0] * _NS_PER_S))
    clock_value[0] += 5.0
    snap = state.snapshot()
    assert snap["healthy"] is True
    assert snap["age_s"] is not None
    assert abs(snap["age_s"] - 5.0) < 1e-6


def test_unhealthy_once_silence_exceeds_threshold() -> None:
    clock_value = [1_000.0]
    state = HealthState(max_silence_s=10.0, clock=lambda: clock_value[0])
    state.record_success(int(clock_value[0] * _NS_PER_S))
    clock_value[0] += 20.0
    assert state.snapshot()["healthy"] is False


def test_failure_records_error_without_clearing_prior_success() -> None:
    clock_value = [1_000.0]
    state = HealthState(max_silence_s=30.0, clock=lambda: clock_value[0])
    state.record_success(int(clock_value[0] * _NS_PER_S))
    clock_value[0] += 1.0
    state.record_failure(int(clock_value[0] * _NS_PER_S), "boom")
    snap = state.snapshot()
    assert snap["last_error"] == "boom"
    assert snap["last_success_push_ns"] is not None  # not wiped by a later failure


def test_http_health_endpoint_reports_status_and_last_push() -> None:
    state = HealthState(max_silence_s=30.0, clock=lambda: 1_700_000_000.0 + 5.0)
    state.record_success(1_700_000_000_000_000_000)
    server = start_health_server("127.0.0.1", 0, state)
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
            assert resp.status == 200
            body = json.loads(resp.read())
        assert body["healthy"] is True
        assert body["last_success_push_ns"] == 1_700_000_000_000_000_000
    finally:
        server.shutdown()
        server.server_close()


def test_http_health_endpoint_404_for_other_paths() -> None:
    state = HealthState()
    server = start_health_server("127.0.0.1", 0, state)
    try:
        port = server.server_address[1]
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/other", timeout=5)
            raised = False
        except urllib.error.HTTPError as exc:
            raised = True
            assert exc.code == 404
        assert raised
    finally:
        server.shutdown()
        server.server_close()
