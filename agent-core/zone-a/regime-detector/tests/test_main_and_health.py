"""Process wiring, entrypoint shim and container health probe."""

from __future__ import annotations

import os
import runpy
import time
from pathlib import Path

import pytest
from regime_detector import healthcheck, main
from regime_detector.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_healthy_when_heartbeat_fresh(tmp_path: Path) -> None:
    beat = tmp_path / "hb"
    beat.touch()
    assert healthcheck.is_healthy(beat, time.time(), 30.0)


def test_unhealthy_when_missing_or_old(tmp_path: Path) -> None:
    beat = tmp_path / "hb"
    assert not healthcheck.is_healthy(beat, time.time(), 30.0)
    beat.touch()
    os.utime(beat, (time.time() - 120, time.time() - 120))
    assert not healthcheck.is_healthy(beat, time.time(), 30.0)


def test_healthcheck_main_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    beat = tmp_path / "hb"
    monkeypatch.setenv("HEARTBEAT_PATH", str(beat))
    assert healthcheck.main() == 1
    beat.touch()
    assert healthcheck.main() == 0
    monkeypatch.setenv("INFERENCE_INTERVAL_S", "abc")
    assert healthcheck.main() == 1


def test_invalid_config_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUESTDB_PORT", "not-a-port")
    with pytest.raises(SystemExit) as exc:
        main.run()
    assert exc.value.code == main.EXIT_CONFIG_ERROR


def test_build_service_wires_without_network(tmp_path: Path) -> None:
    settings = Settings.from_env({"MODEL_DIR": str(tmp_path)})  # no HMAC key: persistence off
    service = main.build_service(settings)
    assert service is not None


def test_entrypoint_shim_invokes_run(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(main, "run", lambda: calls.append("run"))
    runpy.run_path(str(ROOT / "hmm.py"), run_name="__main__")
    assert calls == ["run"]
