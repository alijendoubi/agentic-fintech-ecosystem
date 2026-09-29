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


QDB_PASSWORD = "Zq8vN2kLx7Rt4bWm"


def test_questdb_basic_auth_is_optional_and_absent_by_default() -> None:
    settings = Settings.from_env({})
    assert settings.questdb_http_user is None
    assert settings.questdb_http_password is None


def test_questdb_basic_auth_pair_is_read_and_password_not_in_repr() -> None:
    settings = Settings.from_env(
        {"QUESTDB_HTTP_USER": "afe_reader", "QUESTDB_HTTP_PASSWORD": QDB_PASSWORD}
    )
    assert settings.questdb_http_user == "afe_reader"
    assert settings.questdb_http_password == QDB_PASSWORD
    assert QDB_PASSWORD not in repr(settings)


@pytest.mark.parametrize(
    "env",
    [
        {"QUESTDB_HTTP_USER": "afe_reader"},
        {"QUESTDB_HTTP_PASSWORD": QDB_PASSWORD},
        {"QUESTDB_HTTP_USER": "  ", "QUESTDB_HTTP_PASSWORD": QDB_PASSWORD},
    ],
)
def test_questdb_basic_auth_must_be_a_pair(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError, match="set together"):
        Settings.from_env(env)


@pytest.mark.parametrize("user", ["afe:reader", "a b", "x" * 65, "us\u00e9r"])
def test_questdb_user_rejects_unsafe_names(user: str) -> None:
    with pytest.raises(ConfigError, match="QUESTDB_HTTP_USER"):
        Settings.from_env({"QUESTDB_HTTP_USER": user, "QUESTDB_HTTP_PASSWORD": QDB_PASSWORD})


@pytest.mark.parametrize(
    ("password", "match"),
    [
        ("short-Pw1", "at least 16"),
        ("with space inside 0123", "printable"),
        ("change-me-questdb-0123456789", "placeholder"),
        ("PLACEHOLDER0123456789abc", "placeholder"),
        ("my-default-password-0123", "placeholder"),
    ],
)
def test_questdb_password_rejects_weak_values_without_echoing(password: str, match: str) -> None:
    """ALI-20/ALI-21: a short or placeholder password stops the process and is never echoed."""
    with pytest.raises(ConfigError, match=match) as info:
        Settings.from_env({"QUESTDB_HTTP_USER": "afe_reader", "QUESTDB_HTTP_PASSWORD": password})
    assert password not in str(info.value)
