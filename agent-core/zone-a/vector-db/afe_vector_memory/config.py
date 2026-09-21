"""Environment-driven settings, validated once. Any bad value raises `MemoryConfigError`.

Variables (names match agent-core/infrastructure/docker-compose.yml where they exist):

    CHROMA_HOST                        default "localhost"   (compose sets "vector-db")
    CHROMA_PORT                        default 8000
    CHROMA_SSL                         default false; must be true when ENVIRONMENT=production
    CHROMA_SSL_VERIFY                  default true; "false" or a path to a CA bundle (PEM).
                                       "false" is refused when ENVIRONMENT=production
    CHROMA_AUTH_TOKEN                  optional; sent as "Authorization: Bearer <token>"
    CHROMA_TIMEOUT_S                   default 2.0, range [0.1, 60]  (per-call deadline)
    CHROMA_CONNECT_TIMEOUT_S           default 30, range [1, 300]  (client init + heartbeat)
    MEMORY_COLLECTION                  default "afe_memory"
    MEMORY_RETENTION_DAYS_DEBATE       default 90
    MEMORY_RETENTION_DAYS_REFLECTION   default 365
    MEMORY_MAX_RESULTS                 default 20, range [1, 100]
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from .errors import MemoryConfigError, MemoryValidationError
from .models import MAX_RETENTION_DAYS, RetentionPolicy

_HOST = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?")
_COLLECTION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]{1,61}[A-Za-z0-9]")
_TOKEN = re.compile(r"[\x21-\x7e]{1,4096}")  # printable ASCII, no whitespace: cannot split a header
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
MAX_RESULTS_CEILING = 100
MAX_PORT = 65_535


def _read[T](source: Mapping[str, str], name: str, default: str, parse: Callable[[str], T]) -> T:
    raw = source.get(name, default).strip()
    try:
        return parse(raw)
    except (ValueError, MemoryValidationError) as exc:
        raise MemoryConfigError(f"{name}={raw!r} is invalid: {exc}") from exc


def _host(raw: str) -> str:
    if _HOST.fullmatch(raw) is None:
        raise ValueError("must be a bare hostname or IPv4 address (no scheme, port or path)")
    return raw


def _bounded_int(low: int, high: int) -> Callable[[str], int]:
    def parse(raw: str) -> int:
        value = int(raw)
        if not low <= value <= high:
            raise ValueError(f"must be in [{low}, {high}]")
        return value

    return parse


def _bounded_float(low: float, high: float) -> Callable[[str], float]:
    def parse(raw: str) -> float:
        value = float(raw)
        if not low <= value <= high:  # also rejects NaN
            raise ValueError(f"must be in [{low}, {high}]")
        return value

    return parse


def _bool(raw: str) -> bool:
    lowered = raw.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ValueError("must be one of true/false/1/0/yes/no/on/off")


def _ssl_verify(raw: str) -> bool | str:
    lowered = raw.lower()
    if lowered in _TRUE or lowered in _FALSE:
        return lowered in _TRUE
    if not raw or "\x00" in raw:
        raise ValueError("must be true, false, or a path to a CA bundle")
    return raw


def _auth_token(source: Mapping[str, str]) -> str | None:
    """Read CHROMA_AUTH_TOKEN. Never echoes the value in an error (it is a credential)."""
    raw = source.get("CHROMA_AUTH_TOKEN", "").strip()
    if not raw:
        return None
    if _TOKEN.fullmatch(raw) is None:
        raise MemoryConfigError(
            "CHROMA_AUTH_TOKEN must be 1-4096 printable ASCII characters without whitespace"
        )
    return raw


def _refuse_plaintext_in_production(
    source: Mapping[str, str], ssl: bool, verify: bool | str
) -> None:
    if source.get("ENVIRONMENT", "").strip().lower() != "production":
        return
    if not ssl:
        raise MemoryConfigError("ENVIRONMENT=production requires CHROMA_SSL=true (no plaintext)")
    if verify is False:
        raise MemoryConfigError(
            "ENVIRONMENT=production refuses CHROMA_SSL_VERIFY=false; use true or a CA bundle path"
        )


def _collection(raw: str) -> str:
    if _COLLECTION.fullmatch(raw) is None:
        raise ValueError("must be 3-63 chars of letters, digits, '.', '_' or '-'")
    return raw


@dataclass(frozen=True, slots=True)
class VectorMemorySettings:
    host: str = "localhost"
    port: int = 8000
    ssl: bool = False
    ssl_verify: bool | str = True
    timeout_s: float = 2.0
    connect_timeout_s: float = 30.0
    collection: str = "afe_memory"
    max_results: int = 20
    retention: RetentionPolicy = field(default_factory=RetentionPolicy)
    auth_token: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> VectorMemorySettings:
        """Read settings from `env` (default `os.environ`). Raises `MemoryConfigError`."""
        source = os.environ if env is None else env
        days = _bounded_int(1, MAX_RETENTION_DAYS)
        retention_debate = _read(source, "MEMORY_RETENTION_DAYS_DEBATE", "90", days)
        retention_reflection = _read(source, "MEMORY_RETENTION_DAYS_REFLECTION", "365", days)
        ssl = _read(source, "CHROMA_SSL", "false", _bool)
        ssl_verify = _read(source, "CHROMA_SSL_VERIFY", "true", _ssl_verify)
        _refuse_plaintext_in_production(source, ssl, ssl_verify)
        return cls(
            host=_read(source, "CHROMA_HOST", "localhost", _host),
            port=_read(source, "CHROMA_PORT", "8000", _bounded_int(1, MAX_PORT)),
            ssl=ssl,
            ssl_verify=ssl_verify,
            auth_token=_auth_token(source),
            timeout_s=_read(source, "CHROMA_TIMEOUT_S", "2.0", _bounded_float(0.1, 60.0)),
            connect_timeout_s=_read(
                source, "CHROMA_CONNECT_TIMEOUT_S", "30", _bounded_float(1.0, 300.0)
            ),
            collection=_read(source, "MEMORY_COLLECTION", "afe_memory", _collection),
            max_results=_read(
                source, "MEMORY_MAX_RESULTS", "20", _bounded_int(1, MAX_RESULTS_CEILING)
            ),
            retention=RetentionPolicy(retention_debate, retention_reflection),
        )
