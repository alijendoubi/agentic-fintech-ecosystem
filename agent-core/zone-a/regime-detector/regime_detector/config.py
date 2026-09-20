"""Environment-driven configuration for the regime detector.

All values are validated once, at start-up, into an immutable ``Settings``.
Invalid configuration raises ``ConfigError`` and the process refuses to start,
which is safe: with no detector running, consumers see no fresh labels.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

#: Valid ticker symbols. Enforced on every symbol read from QuestDB (ALI-30).
SYMBOL_PATTERN = re.compile(r"[A-Z.\-]{1,10}")
_IDENTIFIER_PATTERN = re.compile(r"[a-z_][a-z0-9_]{0,62}")
MIN_HMAC_KEY_CHARS = 32
DEFAULT_MODEL_DIR = "/app/models"
DEFAULT_HEARTBEAT_PATH = "/tmp/regime-detector.heartbeat"  # noqa: S108 - container-local file


class ConfigError(ValueError):
    """Raised when an environment variable is missing or invalid."""


def is_valid_symbol(symbol: object) -> bool:
    """True only for strings that fully match ``^[A-Z.\\-]{1,10}$``."""
    return isinstance(symbol, str) and SYMBOL_PATTERN.fullmatch(symbol) is not None


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable runtime settings."""

    questdb_host: str
    questdb_port: int
    questdb_ts_column: str
    questdb_timeout_s: float
    redis_url: str
    redis_channel: str
    model_dir: Path
    model_hmac_key: bytes | None
    inference_interval_s: float
    retrain_interval_s: float
    train_retry_backoff_s: float
    feature_window: int
    min_bars_for_inference: int
    min_train_rows: int
    max_data_age_s: float
    min_confidence: float
    max_symbols: int
    heartbeat_path: Path
    log_level: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build settings from ``env`` (defaults to ``os.environ``)."""
        source = os.environ if env is None else env
        return cls(
            questdb_host=_text(source, "QUESTDB_HOST", "localhost"),
            questdb_port=_int(source, "QUESTDB_PORT", 9000, 1, 65535),
            questdb_ts_column=_identifier(source, "QUESTDB_TS_COLUMN", "timestamp"),
            questdb_timeout_s=_float(source, "QUESTDB_TIMEOUT_S", 2.0, 0.1, 60.0),
            redis_url=_text(source, "REDIS_URL", "redis://localhost:6379"),
            redis_channel=_text(source, "REGIME_CHANNEL", "regime:labels"),
            model_dir=_model_dir(source),
            model_hmac_key=_hmac_key(source),
            inference_interval_s=_float(source, "INFERENCE_INTERVAL_S", 1.0, 0.05, 3600.0),
            retrain_interval_s=_float(source, "RETRAIN_INTERVAL_S", 4 * 3600.0, 1.0, 1e9),
            train_retry_backoff_s=_float(source, "TRAIN_RETRY_BACKOFF_S", 30.0, 0.0, 1e9),
            feature_window=_int(source, "FEATURE_WINDOW", 200, 30, 100_000),
            min_bars_for_inference=_int(source, "MIN_BARS_FOR_INFERENCE", 20, 6, 100_000),
            min_train_rows=_int(source, "MIN_TRAIN_ROWS", 100, 30, 100_000),
            max_data_age_s=_float(source, "MAX_DATA_AGE_S", 10.0, 0.001, 86_400.0),
            min_confidence=_float(source, "MIN_CONFIDENCE", 0.55, 0.0, 1.0),
            max_symbols=_int(source, "MAX_SYMBOLS", 100, 1, 10_000),
            heartbeat_path=Path(_text(source, "HEARTBEAT_PATH", DEFAULT_HEARTBEAT_PATH)),
            log_level=_text(source, "LOG_LEVEL", "info").upper(),
        )


def _text(env: Mapping[str, str], name: str, default: str) -> str:
    value = env.get(name, default).strip()
    if not value:
        raise ConfigError(f"{name} must not be empty")
    return value


def _int(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = env.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if not low <= value <= high:
        raise ConfigError(f"{name}={value} outside [{low}, {high}]")
    return value


def _float(env: Mapping[str, str], name: str, default: float, low: float, high: float) -> float:
    raw = env.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if not low <= value <= high:  # also rejects NaN
        raise ConfigError(f"{name}={value} outside [{low}, {high}]")
    return value


def _identifier(env: Mapping[str, str], name: str, default: str) -> str:
    value = _text(env, name, default)
    if _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ConfigError(f"{name}={value!r} is not a safe SQL identifier")
    return value


def _model_dir(env: Mapping[str, str]) -> Path:
    """``MODEL_DIR``; falls back to the parent of the legacy ``MODEL_PATH`` file."""
    explicit = env.get("MODEL_DIR", "").strip()
    if explicit:
        return Path(explicit)
    legacy = env.get("MODEL_PATH", "").strip()
    if legacy:
        return Path(legacy).parent
    return Path(DEFAULT_MODEL_DIR)


def _hmac_key(env: Mapping[str, str]) -> bytes | None:
    """``MODEL_HMAC_KEY``. Absent means models are never loaded or persisted."""
    raw = env.get("MODEL_HMAC_KEY", "").strip()
    if not raw:
        return None
    if len(raw) < MIN_HMAC_KEY_CHARS:
        raise ConfigError(f"MODEL_HMAC_KEY must be at least {MIN_HMAC_KEY_CHARS} characters")
    return raw.encode("utf-8")
