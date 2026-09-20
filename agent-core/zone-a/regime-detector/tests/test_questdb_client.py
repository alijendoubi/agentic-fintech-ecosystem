"""ALI-30 (query injection) and ALI-29 (row alignment) for the QuestDB client."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from regime_detector.config import Settings
from regime_detector.questdb_client import (
    QuestDbClient,
    QuestDbError,
    parse_timestamp_ns,
)

COLUMNS = [
    {"name": "mid_price", "type": "DOUBLE"},
    {"name": "spread", "type": "DOUBLE"},
    {"name": "order_flow_imbalance", "type": "DOUBLE"},
    {"name": "is_stale", "type": "BOOLEAN"},
    {"name": "timestamp", "type": "TIMESTAMP"},
]


def _settings(**overrides: str) -> Settings:
    return Settings.from_env({"FEATURE_WINDOW": "50", **overrides})


class FakeFetch:
    """Records calls and returns a canned payload (or raises)."""

    def __init__(self, payload: Mapping[str, Any] | None = None, error: bool = False) -> None:
        self.payload = payload or {}
        self.error = error
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __call__(self, url: str, params: Mapping[str, str]) -> Mapping[str, Any]:
        self.calls.append((url, dict(params)))
        if self.error:
            raise QuestDbError("boom")
        return self.payload


@pytest.mark.asyncio
async def test_invalid_symbol_never_reaches_the_network() -> None:
    fetch = FakeFetch()
    client = QuestDbClient(_settings(), fetch)
    for bad in ("AAPL' OR 1=1 --", "aapl", "AAPL;DROP TABLE x", "", "A" * 11, "AAPL\n"):
        assert await client.fetch_rows(bad) is None
    assert fetch.calls == []


@pytest.mark.asyncio
async def test_query_uses_validated_symbol_and_limit() -> None:
    fetch = FakeFetch({"columns": COLUMNS, "dataset": []})
    client = QuestDbClient(_settings(), fetch)
    assert await client.fetch_rows("BRK.B") is None  # empty dataset
    _, params = fetch.calls[0]
    assert "symbol = 'BRK.B'" in params["query"]
    assert params["query"].endswith("LIMIT 50")


@pytest.mark.asyncio
async def test_rows_returned_chronological_and_aligned_with_nulls_kept() -> None:
    newest_first = [
        [103.0, 0.02, 0.3, False, "2026-09-19T10:00:03.500000Z"],
        [None, 0.02, 0.2, False, "2026-09-19T10:00:02.000000Z"],
        [101.0, None, 0.1, True, "2026-09-19T10:00:01.000000Z"],
    ]
    client = QuestDbClient(_settings(), FakeFetch({"columns": COLUMNS, "dataset": newest_first}))
    rows = await client.fetch_rows("AAPL")
    assert rows is not None
    assert rows.mid_prices == (101.0, None, 103.0)
    assert rows.spreads == (None, 0.02, 0.02)
    assert rows.ofis == (0.1, 0.2, 0.3)
    assert len(rows.mid_prices) == len(rows.spreads) == len(rows.ofis) == len(rows) == 3
    assert rows.latest_is_stale is False
    assert rows.latest_ts_ns == parse_timestamp_ns("2026-09-19T10:00:03.500000Z")


@pytest.mark.asyncio
async def test_latest_stale_flag_is_reported() -> None:
    row = [100.0, 0.01, 0.0, True, "2026-09-19T10:00:00.000000Z"]
    client = QuestDbClient(_settings(), FakeFetch({"columns": COLUMNS, "dataset": [row]}))
    rows = await client.fetch_rows("AAPL")
    assert rows is not None and rows.latest_is_stale


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"error": "table does not exist"},
        {"dataset": "nope", "columns": COLUMNS},
        {"dataset": [[1, 2]], "columns": [{"name": "x"}]},
        {"dataset": [[1, 2]]},
    ],
)
async def test_bad_payloads_fail_closed(payload: dict[str, Any]) -> None:
    client = QuestDbClient(_settings(), FakeFetch(payload))
    assert await client.fetch_rows("AAPL") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"error": "x"}, {"dataset": "nope"}, {}])
async def test_bad_symbol_payloads_fail_closed(payload: dict[str, Any]) -> None:
    assert await QuestDbClient(_settings(), FakeFetch(payload)).list_symbols() is None


@pytest.mark.asyncio
async def test_fetch_error_returns_none() -> None:
    client = QuestDbClient(_settings(), FakeFetch(error=True))
    assert await client.fetch_rows("AAPL") is None
    assert await client.list_symbols() is None


@pytest.mark.asyncio
async def test_list_symbols_drops_invalid_and_dedups() -> None:
    dataset = [["MSFT"], ["AAPL"], ["x'; DROP"], [None], ["AAPL"], [], ["brk.b"], ["BRK.B"]]
    client = QuestDbClient(_settings(), FakeFetch({"dataset": dataset}))
    assert await client.list_symbols() == ["AAPL", "BRK.B", "MSFT"]


@pytest.mark.asyncio
async def test_custom_timestamp_column_in_sql() -> None:
    fetch = FakeFetch({"columns": COLUMNS, "dataset": []})
    client = QuestDbClient(_settings(QUESTDB_TS_COLUMN="ingestion_ts"), fetch)
    await client.fetch_rows("AAPL")
    assert "ORDER BY ingestion_ts DESC" in fetch.calls[0][1]["query"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1970-01-01T00:00:01.000000Z", 1_000_000_000),
        ("1970-01-01T00:00:00.000001Z", 1_000),
        ("2026-09-19T10:00:00Z", 1_789_812_000_000_000_000),
        ("not a date", None),
        (None, None),
        (12345, None),
    ],
)
def test_parse_timestamp_ns(value: object, expected: int | None) -> None:
    assert parse_timestamp_ns(value) == expected


@pytest.mark.asyncio
async def test_real_http_roundtrip_url_encodes_query() -> None:
    """Default fetcher against a real local HTTP server: SQL arrives intact, encoded."""
    seen: list[str] = []

    async def handler(request: web.Request) -> web.Response:
        seen.append(request.query["query"])
        return web.json_response({"columns": COLUMNS, "dataset": []})

    app = web.Application()
    app.router.add_get("/exec", handler)
    async with TestServer(app) as server:
        settings = _settings(QUESTDB_HOST="127.0.0.1", QUESTDB_PORT=str(server.port))
        client = QuestDbClient(settings)
        try:
            assert await client.fetch_rows("AAPL") is None
            assert await client.list_symbols() == []
        finally:
            await client.close()
    assert "WHERE symbol = 'AAPL'" in seen[0]
    assert seen[1].startswith("SELECT DISTINCT symbol FROM market_data LIMIT")


@pytest.mark.asyncio
async def test_http_error_status_and_unreachable_fail_closed() -> None:
    async def handler(_: web.Request) -> web.Response:
        return web.Response(status=500, text="boom")

    app = web.Application()
    app.router.add_get("/exec", handler)
    async with TestServer(app) as server:
        client = QuestDbClient(_settings(QUESTDB_HOST="127.0.0.1", QUESTDB_PORT=str(server.port)))
        try:
            assert await client.fetch_rows("AAPL") is None
        finally:
            await client.close()
    dead = QuestDbClient(_settings(QUESTDB_HOST="127.0.0.1", QUESTDB_PORT="1"))
    try:
        assert await dead.list_symbols() is None
    finally:
        await dead.close()
