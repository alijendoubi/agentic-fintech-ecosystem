from __future__ import annotations

from pathlib import Path

import pytest
from regime_detector.config import ConfigError, Settings, is_valid_symbol


def test_defaults() -> None:
    settings = Settings.from_env({})
    assert settings.feature_window == 200
    assert settings.model_hmac_key is None
    assert settings.questdb_ts_column == "timestamp"


def test_legacy_model_path_maps_to_directory() -> None:
    settings = Settings.from_env({"MODEL_PATH": "/app/models/hmm_5state.pkl"})
    assert settings.model_dir == Path("/app/models")


def test_model_dir_wins_over_legacy_path() -> None:
    env = {"MODEL_DIR": "/data/m", "MODEL_PATH": "/app/models/x.pkl"}
    assert Settings.from_env(env).model_dir == Path("/data/m")


def test_hmac_key_is_bytes_and_min_length_enforced() -> None:
    assert Settings.from_env({"MODEL_HMAC_KEY": "k" * 32}).model_hmac_key == b"k" * 32
    with pytest.raises(ConfigError):
        Settings.from_env({"MODEL_HMAC_KEY": "short"})


@pytest.mark.parametrize(
    "env",
    [
        {"QUESTDB_PORT": "abc"},
        {"QUESTDB_PORT": "70000"},
        {"MIN_CONFIDENCE": "nan"},
        {"MIN_CONFIDENCE": "1.5"},
        {"INFERENCE_INTERVAL_S": "0"},
        {"QUESTDB_TS_COLUMN": "ts; DROP TABLE x"},
        {"QUESTDB_HOST": "  "},
    ],
)
def test_invalid_values_rejected(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        Settings.from_env(env)


@pytest.mark.parametrize("symbol", ["AAPL", "BRK.B", "BF-B", "A", "ABCDEFGHIJ"])
def test_valid_symbols(symbol: str) -> None:
    assert is_valid_symbol(symbol)


@pytest.mark.parametrize(
    "symbol",
    ["", "aapl", "AAPL\n", "A'B", "AAPL;DROP", "ABCDEFGHIJK", "AA PL", "A%27", None, 5],
)
def test_invalid_symbols(symbol: object) -> None:
    assert not is_valid_symbol(symbol)
