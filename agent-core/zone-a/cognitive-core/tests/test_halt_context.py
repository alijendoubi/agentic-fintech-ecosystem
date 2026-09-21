from __future__ import annotations

import json
from pathlib import Path

import pytest
from cognitive_core.context import (
    MAX_MESSAGE_BYTES,
    NS_PER_S,
    ContextError,
    parse_snapshot,
)
from cognitive_core.halt import HALT_ENV_VAR, HALT_FILE_ENV_VAR, HaltGate
from cognitive_core.models import RegimeLabel
from cognitive_core.tests.runner_fakes import NOW_NS, snapshot, snapshot_json

# -- halt gate ----------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "on"])
def test_truthy_env_halts(value: str) -> None:
    status = HaltGate({HALT_ENV_VAR: value}).check()
    assert status.halted and HALT_ENV_VAR in status.reason


@pytest.mark.parametrize(
    "env", [{}, {HALT_ENV_VAR: ""}, {HALT_ENV_VAR: "0"}, {HALT_ENV_VAR: "False"}]
)
def test_absent_or_falsy_env_runs(env: dict[str, str]) -> None:
    assert HaltGate(env).check().halted is False


@pytest.mark.parametrize("value", ["maybe", "2", "halt", "nope!"])
def test_unparsable_env_halts_fail_closed(value: str) -> None:
    status = HaltGate({HALT_ENV_VAR: value}).check()
    assert status.halted and "fail closed" in status.reason


def test_halt_file_is_checked_on_every_call(tmp_path: Path) -> None:
    halt_file = tmp_path / "halt"
    gate = HaltGate({HALT_FILE_ENV_VAR: str(halt_file)})
    assert gate.check().halted is False
    halt_file.write_text("")  # existence is the signal; content is irrelevant
    assert gate.check().halted is True
    halt_file.unlink()
    assert gate.check().halted is False


def test_explicit_halt_file_argument_wins_over_env(tmp_path: Path) -> None:
    halt_file = tmp_path / "halt"
    halt_file.touch()
    assert HaltGate({HALT_FILE_ENV_VAR: "/nonexistent/x"}, halt_file=halt_file).check().halted


def test_unreadable_halt_file_halts_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(self: Path) -> bool:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "exists", deny)
    status = HaltGate({}, halt_file="/some/halt").check()
    assert status.halted and "PermissionError" in status.reason


def test_default_gate_reads_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(HALT_ENV_VAR, "1")
    assert HaltGate().check().halted


# -- snapshot parsing ---------------------------------------------------------------


def _parse(raw: str | bytes, *, now_ns: int = NOW_NS, max_age_s: float = 5.0):  # type: ignore[no-untyped-def]
    return parse_snapshot(raw, now_ns=now_ns, max_age_s=max_age_s)


def test_valid_snapshot_maps_field_names_and_ignores_extras() -> None:
    parsed = _parse(snapshot_json(unknown_future_field=[1, 2]))
    ctx = parsed.market_context
    assert (ctx.symbol, ctx.mid_price, ctx.ofi, ctx.realized_vol) == ("AAPL", 190.0, 0.1, 0.22)
    assert parsed.regime is RegimeLabel.TRENDING_BULL
    assert parsed.regime_confidence == 0.85
    assert parsed.ingestion_ts_ns == snapshot()["ingestion_ts_ns"]


def test_bytes_payload_is_decoded() -> None:
    assert _parse(snapshot_json().encode()).market_context.symbol == "AAPL"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "{",
        "null",
        "[1]",
        '"text"',
        "NaN",
        b"\xff\xfe\xfd",
        pytest.param("x" * (MAX_MESSAGE_BYTES + 1), id="oversized-str"),
        pytest.param(b"x" * (MAX_MESSAGE_BYTES + 1), id="oversized-bytes"),
        123,
        pytest.param("[" * 60_000, id="deeply-nested"),
    ],
)
def test_undecodable_messages_are_rejected(raw: object) -> None:
    with pytest.raises(ContextError):
        _parse(raw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        {"is_stale": True},
        {"is_stale": "no"},
        {"warmup": True},
        {"warmup": None},
        {"ingestion_ts_ns": 0},
        {"ingestion_ts_ns": "1"},
        {"ingestion_ts_ns": True},
        {"ingestion_ts_ns": NOW_NS - 10 * NS_PER_S},  # too old
        {"ingestion_ts_ns": NOW_NS + 5 * NS_PER_S},  # from the future
        {"regime_label": "SIDEWAYS"},
        {"regime_label": None},
        {"regime_label": "REGIME_UNKNOWN"},
        {"regime_confidence": 1.5},
        {"regime_confidence": -0.1},
        {"regime_confidence": "high"},
        {"regime_confidence": True},
        {"symbol": ""},
        {"symbol": None},
        {"symbol": "X" * 40},
        {"mid_price": 0},
        {"mid_price": -5},
        {"mid_price": None},
        {"z_score": "1.0"},
        {"order_flow_imbalance": 1.5},
        {"realized_volatility": -0.1},
        {"adv_30d": None},
    ],
)
def test_untrustworthy_snapshots_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ContextError):
        _parse(json.dumps(snapshot(**overrides)))


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_json_constants_are_rejected(token: str) -> None:
    raw = snapshot_json().replace('"z_score": 1.2', f'"z_score": {token}')
    with pytest.raises(ContextError, match="non-finite"):
        _parse(raw)


def test_missing_required_key_is_rejected() -> None:
    payload = snapshot()
    del payload["order_flow_imbalance"]
    with pytest.raises(ContextError, match="order_flow_imbalance"):
        _parse(json.dumps(payload))


def test_small_clock_skew_into_the_future_is_tolerated() -> None:
    parsed = _parse(snapshot_json(ingestion_ts_ns=NOW_NS + NS_PER_S // 2))
    assert parsed.market_context.symbol == "AAPL"
