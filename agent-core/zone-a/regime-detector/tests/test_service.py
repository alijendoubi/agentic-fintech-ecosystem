"""End-to-end behaviour of the loop with fakes: fail-closed paths, per-symbol models,
non-blocking training, retry semantics, persistence and Redis failures."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from regime_detector.config import Settings
from regime_detector.features import extract_features
from regime_detector.labels import STATE_REGIMES, RegimeLabel
from regime_detector.model import TrainedModel, TrainingError, train_model
from regime_detector.model_store import ModelStore
from regime_detector.publisher import RegimePublisher
from regime_detector.questdb_client import BarRows
from regime_detector.service import RegimeService
from synthetic import make_columns

KEY = "k" * 32
NOW = 1_800_000_000.0


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeQuestDb:
    def __init__(self, rows: dict[str, BarRows | None]) -> None:
        self.rows = rows
        self.symbols: list[str] | None = list(rows)
        self.closed = False

    async def list_symbols(self) -> list[str] | None:
        return None if self.symbols is None else list(self.symbols)

    async def fetch_rows(self, symbol: str) -> BarRows | None:
        result = self.rows[symbol]
        if isinstance(result, Exception):
            raise result
        return result

    async def close(self) -> None:
        self.closed = True


class FakeRedis:
    def __init__(self) -> None:
        self.fail = False
        self.sent: list[str] = []

    async def publish(self, channel: str, message: str) -> int:
        if self.fail:
            raise OSError("redis down")
        self.sent.append(message)
        return 1


def bar_rows(seed: int, ts: float = NOW, n_per_regime: int = 40, stale: bool = False) -> BarRows:
    prices, spreads, ofis = make_columns(np.random.default_rng(seed), n_per_regime)
    return BarRows(tuple(prices), tuple(spreads), tuple(ofis), int(ts * 1e9), stale)


@pytest.fixture(scope="module")
def real_model() -> TrainedModel:
    build = extract_features(*make_columns(np.random.default_rng(5), 40))
    assert build.matrix is not None
    return train_model(build.matrix, NOW)


class Harness:
    def __init__(self, tmp: Path, rows: dict[str, BarRows | None], **env: str) -> None:
        self.settings = Settings.from_env(
            {
                "MIN_CONFIDENCE": "0",
                "HEARTBEAT_PATH": str(tmp / "hb"),
                "MODEL_DIR": str(tmp / "models"),
                "MODEL_HMAC_KEY": KEY,
                "RETRAIN_INTERVAL_S": "3600",
                "TRAIN_RETRY_BACKOFF_S": "30",
                **env,
            }
        )
        self.clock = Clock()
        self.questdb = FakeQuestDb(rows)
        self.redis = FakeRedis()
        self.trained_on: list[np.ndarray] = []
        self.model: TrainedModel | None = None
        self.train_error: Exception | None = None
        self.store = ModelStore(self.settings.model_dir, self.settings.model_hmac_key)
        self.service = self.build()

    def build(self, trainer=None) -> RegimeService:  # type: ignore[no-untyped-def]
        return RegimeService(
            self.settings,
            self.questdb,  # type: ignore[arg-type]
            RegimePublisher(self.redis, self.settings.redis_channel),
            self.store,
            clock=self.clock,
            trainer=trainer or self.fake_trainer,
        )

    def fake_trainer(self, matrix: np.ndarray, trained_at: float) -> TrainedModel:
        self.trained_on.append(matrix)
        if self.train_error is not None:
            raise self.train_error
        assert self.model is not None
        return replace(self.model, trained_at=trained_at)

    async def cycle(self) -> dict[str, RegimeLabel]:
        messages = await self.service.run_cycle()
        return {m.symbol: m.label for m in messages}

    async def reasons(self) -> dict[str, str | None]:
        return {m.symbol: m.reason for m in await self.service.run_cycle()}


@pytest.fixture
def harness(tmp_path: Path, real_model: TrainedModel):  # type: ignore[no-untyped-def]
    h = Harness(tmp_path, {"AAPL": bar_rows(1)})
    h.model = real_model
    return h


@pytest.mark.asyncio
async def test_untrained_publishes_unknown_then_trains_in_background(harness) -> None:  # type: ignore[no-untyped-def]
    first = await harness.reasons()
    assert first == {"AAPL": "model_untrained"}
    await harness.service.drain_training()
    labels = await harness.cycle()
    assert labels["AAPL"] in STATE_REGIMES
    assert len(harness.trained_on) == 1


@pytest.mark.asyncio
async def test_published_json_is_valid(harness) -> None:  # type: ignore[no-untyped-def]
    await harness.cycle()
    assert len(harness.redis.sent) == 1
    assert '"label":"REGIME_UNKNOWN"' in harness.redis.sent[0]


@pytest.mark.asyncio
async def test_low_confidence_publishes_unknown(tmp_path: Path, real_model: TrainedModel) -> None:
    h = Harness(tmp_path, {"AAPL": bar_rows(1)}, MIN_CONFIDENCE="1.0")
    h.model = real_model
    await h.cycle()
    await h.service.drain_training()
    reasons = await h.reasons()
    assert reasons["AAPL"] is not None and reasons["AAPL"].startswith("low_confidence")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ts_offset", "reason"),
    [(-60.0, "stale_data"), (+60.0, "stale_data"), (-1.0, None)],
)
async def test_data_staleness_check(  # type: ignore[no-untyped-def]
    harness, ts_offset: float, reason: str | None
) -> None:
    harness.questdb.rows["AAPL"] = bar_rows(1, ts=NOW + ts_offset)
    await harness.cycle()
    await harness.service.drain_training()
    result = await harness.reasons()
    if reason is None:
        assert result["AAPL"] is None
    else:
        assert result["AAPL"] == reason
        assert (await harness.cycle())["AAPL"] is RegimeLabel.UNKNOWN


@pytest.mark.asyncio
async def test_stale_flag_and_missing_timestamp_fail_closed(harness) -> None:  # type: ignore[no-untyped-def]
    good = bar_rows(1)
    harness.questdb.rows["AAPL"] = replace(good, latest_is_stale=True)
    assert await harness.reasons() == {"AAPL": "source_flagged_stale"}
    harness.questdb.rows["AAPL"] = replace(good, latest_ts_ns=None)
    assert await harness.reasons() == {"AAPL": "bad_timestamp"}


@pytest.mark.asyncio
async def test_questdb_outage_publishes_unknown_for_known_symbols(harness) -> None:  # type: ignore[no-untyped-def]
    await harness.cycle()
    harness.questdb.symbols = None
    assert await harness.reasons() == {"AAPL": "questdb_unavailable"}
    harness.questdb.rows["AAPL"] = None
    harness.questdb.symbols = ["AAPL"]
    assert await harness.reasons() == {"AAPL": "no_data"}


@pytest.mark.asyncio
async def test_outage_before_any_symbol_known_publishes_nothing(harness) -> None:  # type: ignore[no-untyped-def]
    harness.questdb.symbols = None
    assert await harness.service.run_cycle() == []


@pytest.mark.asyncio
async def test_insufficient_rows_publish_unknown(tmp_path: Path) -> None:
    h = Harness(tmp_path, {"AAPL": bar_rows(1, n_per_regime=2)})
    reasons = await h.reasons()
    assert reasons["AAPL"] is not None and reasons["AAPL"].startswith("insufficient_data")
    assert h.trained_on == []


@pytest.mark.asyncio
async def test_nan_and_none_cells_do_not_crash_the_loop(harness) -> None:  # type: ignore[no-untyped-def]
    rows = bar_rows(1)
    spreads = list(rows.spreads)
    prices = list(rows.mid_prices)
    spreads[100] = None
    prices[120] = float("nan")
    prices[121] = None
    harness.questdb.rows["AAPL"] = replace(rows, spreads=tuple(spreads), mid_prices=tuple(prices))
    await harness.cycle()
    await harness.service.drain_training()
    assert (await harness.cycle())["AAPL"] in STATE_REGIMES
    assert np.isfinite(harness.trained_on[0]).all()


@pytest.mark.asyncio
async def test_per_symbol_models_not_one_global_model(tmp_path: Path, real_model) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path, {"AAPL": bar_rows(1), "MSFT": bar_rows(2)})
    h.model = real_model
    await h.cycle()
    await h.service.drain_training()
    assert len(h.trained_on) == 2
    assert not np.array_equal(h.trained_on[0], h.trained_on[1])
    assert (await h.cycle()).keys() == {"AAPL", "MSFT"}
    assert (tmp_path / "models" / "regime_AAPL.model").exists()
    assert (tmp_path / "models" / "regime_MSFT.model").exists()


@pytest.mark.asyncio
async def test_failed_training_backs_off_and_does_not_reset_retrain_clock(harness) -> None:  # type: ignore[no-untyped-def]
    harness.train_error = TrainingError("singular")
    await harness.cycle()
    await harness.service.drain_training()
    assert (await harness.reasons())["AAPL"] == "model_untrained"
    assert len(harness.trained_on) == 1  # in backoff: no hot retry loop
    harness.clock.now += 31.0  # past the 30s backoff, far short of the 4h interval
    harness.questdb.rows["AAPL"] = bar_rows(1, ts=harness.clock.now)
    await harness.cycle()
    await harness.service.drain_training()
    assert len(harness.trained_on) == 2
    await harness.cycle()  # collects the second failure; retry now scheduled 30s out
    harness.train_error = None
    harness.clock.now += 31.0
    harness.questdb.rows["AAPL"] = bar_rows(1, ts=harness.clock.now)
    await harness.cycle()
    await harness.service.drain_training()
    assert len(harness.trained_on) == 3
    assert (await harness.cycle())["AAPL"] in STATE_REGIMES


@pytest.mark.asyncio
async def test_retrain_only_after_interval_elapsed(harness) -> None:  # type: ignore[no-untyped-def]
    await harness.cycle()
    await harness.service.drain_training()
    await harness.cycle()
    assert len(harness.trained_on) == 1
    harness.clock.now += 3601.0
    harness.questdb.rows["AAPL"] = bar_rows(1, ts=harness.clock.now)
    await harness.cycle()
    await harness.service.drain_training()
    assert len(harness.trained_on) == 2


@pytest.mark.asyncio
async def test_training_does_not_block_the_event_loop(tmp_path: Path, real_model) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path, {"AAPL": bar_rows(1)})
    started, release = threading.Event(), threading.Event()

    def slow_trainer(_: np.ndarray, trained_at: float) -> TrainedModel:
        started.set()
        release.wait(timeout=10)
        return replace(real_model, trained_at=trained_at)

    service = h.build(trainer=slow_trainer)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while not release.is_set():
            ticks += 1
            await asyncio.sleep(0.005)

    ticker_task = asyncio.create_task(ticker())
    await service.run_cycle()
    for _ in range(200):
        if started.is_set():
            break
        await asyncio.sleep(0.01)
    assert started.is_set()
    before = ticks
    await asyncio.sleep(0.1)  # training is blocked in its thread; the loop keeps ticking
    assert ticks > before + 3
    assert (await service.run_cycle())[0].label is RegimeLabel.UNKNOWN  # still publishing
    release.set()
    await service.drain_training()
    await ticker_task
    await service.shutdown()


@pytest.mark.asyncio
async def test_redis_publish_errors_do_not_kill_the_cycle(harness) -> None:  # type: ignore[no-untyped-def]
    harness.redis.fail = True
    assert len(await harness.service.run_cycle()) == 1  # no exception
    harness.redis.fail = False
    await harness.cycle()
    assert len(harness.redis.sent) == 1  # retried on the next cycle


@pytest.mark.asyncio
async def test_unexpected_symbol_error_yields_unknown_and_others_continue(  # type: ignore[no-untyped-def]
    tmp_path: Path, real_model
) -> None:
    h = Harness(tmp_path, {"AAPL": bar_rows(1), "MSFT": bar_rows(2)})  # type: ignore[dict-item]
    h.model = real_model
    h.questdb.rows["AAPL"] = RuntimeError("bug")  # type: ignore[assignment]
    result = await h.reasons()
    assert result["AAPL"] == "internal_error"
    assert result["MSFT"] == "model_untrained"


@pytest.mark.asyncio
async def test_persisted_model_is_loaded_on_restart(harness, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    await harness.cycle()
    await harness.service.drain_training()

    def must_not_train(*_: object) -> TrainedModel:
        raise AssertionError("restart must load the persisted model, not retrain")

    restarted = harness.build(trainer=must_not_train)
    messages = await restarted.run_cycle()
    assert messages[0].label in STATE_REGIMES


@pytest.mark.asyncio
async def test_tampered_model_is_refused_and_retrained(harness) -> None:  # type: ignore[no-untyped-def]
    await harness.cycle()
    await harness.service.drain_training()
    path = harness.settings.model_dir / "regime_AAPL.model"
    blob = bytearray(path.read_bytes())
    blob[-5] ^= 0xFF
    path.write_bytes(bytes(blob))
    restarted = harness.build()
    messages = await restarted.run_cycle()
    assert messages[0].label is RegimeLabel.UNKNOWN
    await restarted.drain_training()
    assert len(harness.trained_on) == 2


@pytest.mark.asyncio
async def test_without_hmac_key_nothing_is_persisted(tmp_path: Path, real_model) -> None:  # type: ignore[no-untyped-def]
    h = Harness(tmp_path, {"AAPL": bar_rows(1)}, MODEL_HMAC_KEY="")
    h.model = real_model
    await h.cycle()
    await h.service.drain_training()
    assert not (tmp_path / "models").exists()
    assert (await h.cycle())["AAPL"] in STATE_REGIMES  # still serves from memory


@pytest.mark.asyncio
async def test_run_loop_stops_touches_heartbeat_and_closes(harness) -> None:  # type: ignore[no-untyped-def]
    stop = asyncio.Event()
    task = asyncio.create_task(harness.service.run(stop))
    for _ in range(200):
        if harness.redis.sent:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert harness.settings.heartbeat_path.exists()
    assert harness.questdb.closed


@pytest.mark.asyncio
async def test_cycle_level_crash_is_survived_and_abstains(harness) -> None:  # type: ignore[no-untyped-def]
    await harness.cycle()
    harness.questdb.symbols = None

    async def boom() -> list[str] | None:
        raise RuntimeError("discovery bug")

    harness.questdb.list_symbols = boom  # type: ignore[method-assign]
    harness.redis.sent.clear()
    await harness.service._guarded_cycle()  # noqa: SLF001 - exercising the loop guard
    assert len(harness.redis.sent) == 1
    assert '"reason":"internal_error"' in harness.redis.sent[0]
