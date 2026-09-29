"""ALI-157: seed scenarios (format, loader, retention and recall through every store)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from afe_vector_memory import AsyncMemoryAdapter, MemoryKind, MemoryValidationError
from afe_vector_memory.models import MS_PER_DAY, SEED_EXPIRES_MS
from afe_vector_memory.seed import (
    SEED_SYMBOL,
    load_seed_file,
    main,
    parse_seed_document,
    seed_store,
)
from tests.conftest import StoreFactory
from tests.helpers import Clock

SHIPPED = Path(__file__).resolve().parents[1] / "seeds" / "historical_scenarios.json"


def _doc(**entry_over: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": "test-event",
        "title": "Test event",
        "date": "2010-05-06",
        "regimes": ["CRISIS", "HIGH_VOL_CHOP"],
        "what_happened": "Liquidity vanished.",
        "failure_pattern": "Books thin out faster than prices move.",
        "lesson_for_red": "Ask whether the book will be there.",
        "sources": ["https://example.org/report"],
    }
    entry.update(entry_over)
    return {"schema_version": 1, "status": "APPROVED", "entries": [entry]}


def test_shipped_seed_file_is_valid_draft_covering_every_regime() -> None:
    seeds = load_seed_file(SHIPPED)
    assert seeds.status == "DRAFT"  # owner review pending: never silently APPROVED
    regimes = {r.regime for r in seeds.records()}
    assert regimes == {"TRENDING_BULL", "TRENDING_BEAR", "HIGH_VOL_CHOP", "LOW_VOL_CHOP", "CRISIS"}
    assert all(r.kind is MemoryKind.SEED_SCENARIO for r in seeds.records())


def test_one_record_per_regime_with_stable_ids() -> None:
    records = parse_seed_document(_doc()).records()
    assert [r.record_id for r in records] == [
        "seed:test-event:CRISIS",
        "seed:test-event:HIGH_VOL_CHOP",
    ]
    assert {r.symbol for r in records} == {SEED_SYMBOL}
    assert records[0].text.startswith(
        "HISTORICAL SEED SCENARIO (public event, not a trade of ours)"
    )
    assert "https://example.org/report" in records[0].text


@pytest.mark.parametrize(
    "over",
    [
        {"id": "Bad ID"},
        {"date": "2010-13-40"},
        {"regimes": []},
        {"regimes": ["CRISIS", "CRISIS"]},
        {"regimes": ["REGIME_UNKNOWN"]},
        {"sources": []},
        {"sources": ["http://insecure.example/x"]},
        {"title": "  "},
        {"lesson_for_red": "x" * 1501},
    ],
)
def test_invalid_entries_reject_the_whole_file(over: dict[str, Any]) -> None:
    with pytest.raises(MemoryValidationError):
        parse_seed_document(_doc(**over))


def test_unknown_keys_duplicates_and_bad_header_are_rejected() -> None:
    extra = _doc()
    extra["entries"][0]["note"] = "surprise"
    dup = _doc()
    dup["entries"].append(copy.deepcopy(dup["entries"][0]))
    for bad in (extra, dup, {**_doc(), "status": "OK"}, {**_doc(), "schema_version": 2}):
        with pytest.raises(MemoryValidationError):
            parse_seed_document(bad)


def test_retention_never_expires_seeds() -> None:
    from afe_vector_memory import RetentionPolicy

    policy = RetentionPolicy(10, 30)
    assert policy.expires_at_ms(MemoryKind.SEED_SCENARIO, 0) == SEED_EXPIRES_MS
    assert policy.expires_at_ms(MemoryKind.DEBATE_OUTCOME, 0) == 10 * MS_PER_DAY


def test_draft_file_is_refused_unless_explicitly_allowed(store_factory: StoreFactory) -> None:
    store = store_factory(Clock())
    draft = parse_seed_document({**_doc(), "status": "DRAFT"})
    with pytest.raises(MemoryValidationError, match="DRAFT"):
        seed_store(store, draft)
    assert store.count() == 0
    assert seed_store(store, draft, allow_draft=True) == 2


def test_seeds_survive_cleanup_and_reload_is_idempotent(store_factory: StoreFactory) -> None:
    clock = Clock()
    store = store_factory(clock)
    seeds = parse_seed_document(_doc())
    seed_store(store, seeds)
    seed_store(store, seeds)
    assert store.count() == 2  # upsert by record_id: no duplicates
    clock.now_ms += 10_000 * MS_PER_DAY  # far past any age-based retention
    store.cleanup_expired()
    assert store.count() == 2


@pytest.mark.asyncio
async def test_red_recall_finds_seeds_in_the_matching_regime_only(
    store_factory: StoreFactory,
) -> None:
    store = store_factory(Clock())
    seed_store(store, parse_seed_document(_doc()))
    adapter = AsyncMemoryAdapter(store)
    hits = await adapter.recall(symbol="AAPL", regime="CRISIS", query_text="liquidity vanished")
    assert [h.kind for h in hits] == ["seed_scenario"]
    assert await adapter.recall(symbol="AAPL", regime="TRENDING_BULL", query_text="x") == []


def test_cli_validate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = tmp_path / "seeds.json"
    good.write_text(json.dumps(_doc()), encoding="utf-8")
    assert main(["validate", str(good)]) == 0
    assert "1 entries, 2 records" in capsys.readouterr().out
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    assert main(["validate", str(bad)]) == 2
