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


GOOD_KEY = "q7Xv2LmR9sTz4WbN8cKp1HdF6gJy3AeU"


def test_hmac_key_is_bytes_and_min_length_enforced() -> None:
    assert Settings.from_env({"MODEL_HMAC_KEY": GOOD_KEY}).model_hmac_key == GOOD_KEY.encode()
    with pytest.raises(ConfigError):
        Settings.from_env({"MODEL_HMAC_KEY": "short"})


@pytest.mark.parametrize(
    "key",
    [
        "change-me-in-production-change-me-in-production",
        "PLACEHOLDER-" + "a1b2c3d4e5f6g7h8i9j0k1l2m3n4",
        "my-default-hmac-key-0123456789abcdef",
    ],
)
def test_hmac_key_placeholder_is_rejected(key: str) -> None:
    """ALI-21: a placeholder must stop the process, not silently authenticate models."""
    with pytest.raises(ConfigError, match="placeholder"):
        Settings.from_env({"MODEL_HMAC_KEY": key})


def test_hmac_key_with_too_few_distinct_chars_is_rejected() -> None:
    with pytest.raises(ConfigError, match="distinct chars"):
        Settings.from_env({"MODEL_HMAC_KEY": "k" * 32})


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
