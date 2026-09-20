"""Read-only QuestDB REST client (``/exec``) for market_data rows.

Security (ALI-30): QuestDB's ``/exec`` endpoint has no bound parameters, so
symbols are validated against ``^[A-Z.\\-]{1,10}$`` *before* they reach SQL,
and the SQL travels as a URL-encoded query parameter (never spliced into the
URL). Symbols returned by discovery that fail validation are dropped.

Column names follow what the Rust sensory-array writes
(``questdb_writer.rs``): ``mid_price``, ``spread``, ``order_flow_imbalance``,
``is_stale``. The designated timestamp column is ILP's default ``timestamp``
and is configurable via ``QUESTDB_TS_COLUMN``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiohttp
import structlog

from regime_detector.config import Settings, is_valid_symbol

log = structlog.get_logger()

TABLE = "market_data"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_NS_PER_US = 1_000

FetchJson = Callable[[str, Mapping[str, str]], Awaitable[Mapping[str, Any]]]


class QuestDbError(RuntimeError):
    """Raised when QuestDB is unreachable or returns an unusable response."""


@dataclass(frozen=True, slots=True)
class BarRows:
    """Chronological (oldest first), row-aligned columns. ``None`` marks a null cell."""

    mid_prices: tuple[float | None, ...]
    spreads: tuple[float | None, ...]
    ofis: tuple[float | None, ...]
    latest_ts_ns: int | None
    latest_is_stale: bool

    def __len__(self) -> int:
        return len(self.mid_prices)


def parse_timestamp_ns(value: object) -> int | None:
    """QuestDB returns TIMESTAMP columns as ISO-8601 strings (microsecond precision)."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    delta = parsed - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * _NS_PER_US


class QuestDbClient:
    """Fetches symbols and recent rows; every failure yields ``None`` (never a guess)."""

    def __init__(self, settings: Settings, fetch: FetchJson | None = None) -> None:
        self._settings = settings
        self._base_url = f"http://{settings.questdb_host}:{settings.questdb_port}/exec"
        self._fetch = fetch or self._http_get_json
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def list_symbols(self) -> list[str] | None:
        """Valid symbols currently in the table, or None if QuestDB failed."""
        sql = f"SELECT DISTINCT symbol FROM {TABLE} LIMIT {self._settings.max_symbols}"
        try:
            payload = await self._fetch(self._base_url, {"query": sql})
            rows = _dataset(payload)
        except QuestDbError as exc:
            log.warning("questdb_symbols_failed", error=str(exc))
            return None
        symbols: list[str] = []
        for row in rows:
            candidate = row[0] if row else None
            if is_valid_symbol(candidate):
                symbols.append(str(candidate))
            else:
                log.warning("questdb_symbol_rejected", symbol=repr(candidate)[:32])
        return sorted(set(symbols))

    async def fetch_rows(self, symbol: str) -> BarRows | None:
        """Latest ``feature_window`` rows for ``symbol``; None on invalid symbol or failure."""
        if not is_valid_symbol(symbol):
            log.warning("questdb_symbol_rejected", symbol=repr(symbol)[:32])
            return None
        ts = self._settings.questdb_ts_column
        sql = (
            f"SELECT mid_price, spread, order_flow_imbalance, is_stale, {ts} "
            f"FROM {TABLE} WHERE symbol = '{symbol}' "
            f"ORDER BY {ts} DESC LIMIT {self._settings.feature_window}"
        )
        try:
            payload = await self._fetch(self._base_url, {"query": sql})
            return _to_bar_rows(payload, ts)
        except QuestDbError as exc:
            log.warning("questdb_fetch_failed", symbol=symbol, error=str(exc))
            return None

    async def _http_get_json(self, url: str, params: Mapping[str, str]) -> Mapping[str, Any]:
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self._settings.questdb_timeout_s)
            self._session = aiohttp.ClientSession(timeout=timeout)
        try:
            async with self._session.get(url, params=dict(params)) as response:
                if response.status != 200:
                    raise QuestDbError(f"HTTP {response.status}")
                body = await response.json()
        except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
            raise QuestDbError(f"request failed: {exc}") from exc
        if not isinstance(body, dict):
            raise QuestDbError("response is not a JSON object")
        return body


def _dataset(payload: Mapping[str, Any]) -> Sequence[Sequence[Any]]:
    if "error" in payload:
        raise QuestDbError(f"query error: {str(payload['error'])[:120]}")
    dataset = payload.get("dataset")
    if not isinstance(dataset, list):
        raise QuestDbError("response has no dataset")
    return dataset


def _to_bar_rows(payload: Mapping[str, Any], ts_column: str) -> BarRows | None:
    dataset = _dataset(payload)
    if not dataset:
        return None
    columns = payload.get("columns")
    if not isinstance(columns, list):
        raise QuestDbError("response has no column metadata")
    idx = {str(c.get("name")): i for i, c in enumerate(columns) if isinstance(c, dict)}
    try:
        mid_i, spr_i = idx["mid_price"], idx["spread"]
        ofi_i, stale_i, ts_i = idx["order_flow_imbalance"], idx["is_stale"], idx[ts_column]
    except KeyError as exc:
        raise QuestDbError(f"missing expected column: {exc}") from exc
    newest_first = list(dataset)
    chronological = list(reversed(newest_first))
    latest = newest_first[0]
    return BarRows(
        mid_prices=tuple(_cell(r, mid_i) for r in chronological),
        spreads=tuple(_cell(r, spr_i) for r in chronological),
        ofis=tuple(_cell(r, ofi_i) for r in chronological),
        latest_ts_ns=parse_timestamp_ns(latest[ts_i]),
        latest_is_stale=latest[stale_i] is True,
    )


def _cell(row: Sequence[Any], i: int) -> float | None:
    value = row[i]
    if value is None or isinstance(value, bool):
        return None
    return value if isinstance(value, (int, float)) else None
