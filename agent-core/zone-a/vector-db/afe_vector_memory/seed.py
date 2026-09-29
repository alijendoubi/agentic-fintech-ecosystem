"""Seed scenarios for the Red debater (ALI-157): curated public market events, loaded into memory.

A seed file is reviewed reference data, not model output. Each entry describes one public,
sourced event and the failure pattern it illustrates; it is stored once per regime it applies
to (recall filters on exact regime) under the pseudo-symbol ``MARKET``, as kind
``seed_scenario``, which never expires (``models.SEED_EXPIRES_MS``). ``record_id`` is
``seed:<id>:<regime>``, so reloading a file overwrites rather than duplicates.

Fail closed: any invalid entry rejects the whole file, and a file whose ``status`` is not
``APPROVED`` is refused by ``load`` unless ``--allow-draft`` is given (dev/test stores only).

    python -m afe_vector_memory.seed validate seeds/historical_scenarios.json
    python -m afe_vector_memory.seed load seeds/historical_scenarios.json [--allow-draft]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .errors import MemoryValidationError
from .models import MAX_TEXT_CHARS, MemoryKind, MemoryRecord, Outcome
from .store import MemoryStore

SCHEMA_VERSION = 1
SEED_SYMBOL = "MARKET"
REGIMES = frozenset({"TRENDING_BULL", "TRENDING_BEAR", "HIGH_VOL_CHOP", "LOW_VOL_CHOP", "CRISIS"})
STATUSES = frozenset({"DRAFT", "APPROVED"})
MAX_FIELD_CHARS = 1500
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_TEXT_FIELDS = ("title", "what_happened", "failure_pattern", "lesson_for_red")


@dataclass(frozen=True, slots=True)
class SeedEntry:
    id: str
    title: str
    date: date
    regimes: tuple[str, ...]
    what_happened: str
    failure_pattern: str
    lesson_for_red: str
    sources: tuple[str, ...]

    def text(self) -> str:
        return (
            f"HISTORICAL SEED SCENARIO (public event, not a trade of ours): {self.title} "
            f"({self.date.isoformat()}). What happened: {self.what_happened} "
            f"Failure pattern: {self.failure_pattern} Lesson: {self.lesson_for_red} "
            f"Sources: {' '.join(self.sources)}"
        )

    def records(self) -> list[MemoryRecord]:
        ts_ms = (
            int(datetime(self.date.year, self.date.month, self.date.day, tzinfo=UTC).timestamp())
            * 1000
        )
        return [
            MemoryRecord(
                record_id=f"seed:{self.id}:{regime}",
                kind=MemoryKind.SEED_SCENARIO,
                text=self.text(),
                symbol=SEED_SYMBOL,
                regime=regime,
                ts_ms=ts_ms,
                outcome=Outcome.UNKNOWN,
            )
            for regime in self.regimes
        ]


@dataclass(frozen=True, slots=True)
class SeedFile:
    status: str
    entries: tuple[SeedEntry, ...]

    def records(self) -> list[MemoryRecord]:
        return [record for entry in self.entries for record in entry.records()]


def _fail(where: str, message: str) -> MemoryValidationError:
    return MemoryValidationError(f"{where}: {message}")


def _entry(raw: Any, index: int) -> SeedEntry:
    where = f"entries[{index}]"
    if not isinstance(raw, dict):
        raise _fail(where, "must be an object")
    expected = {"id", "date", "regimes", "sources", *_TEXT_FIELDS}
    if set(raw) != expected:
        raise _fail(where, f"keys must be exactly {sorted(expected)}")
    entry_id = raw["id"]
    if not isinstance(entry_id, str) or _ID.fullmatch(entry_id) is None:
        raise _fail(where, f"id {entry_id!r} must match {_ID.pattern}")
    where = f"entries[{index}] ({entry_id})"
    for name in _TEXT_FIELDS:
        value = raw[name]
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_FIELD_CHARS:
            raise _fail(
                where, f"{name} must be a non-empty string of at most {MAX_FIELD_CHARS} chars"
            )
    try:
        when = date.fromisoformat(raw["date"])
    except (TypeError, ValueError) as exc:
        raise _fail(where, "date must be YYYY-MM-DD") from exc
    regimes = raw["regimes"]
    if (
        not isinstance(regimes, list)
        or not regimes
        or len(set(regimes)) != len(regimes)
        or not set(regimes) <= REGIMES
    ):
        raise _fail(
            where, f"regimes must be a non-empty list of distinct values from {sorted(REGIMES)}"
        )
    sources = raw["sources"]
    if (
        not isinstance(sources, list)
        or not sources
        or not all(
            isinstance(s, str) and s.startswith("https://") and " " not in s for s in sources
        )
    ):
        raise _fail(where, "sources must be a non-empty list of https:// URLs")
    entry = SeedEntry(
        id=entry_id,
        title=raw["title"].strip(),
        date=when,
        regimes=tuple(regimes),
        what_happened=raw["what_happened"].strip(),
        failure_pattern=raw["failure_pattern"].strip(),
        lesson_for_red=raw["lesson_for_red"].strip(),
        sources=tuple(sources),
    )
    if len(entry.text()) > MAX_TEXT_CHARS:
        raise _fail(where, f"combined text exceeds {MAX_TEXT_CHARS} chars")
    return entry


def parse_seed_document(doc: Any) -> SeedFile:
    if not isinstance(doc, dict) or set(doc) != {"schema_version", "status", "entries"}:
        raise _fail("file", "top level must be {schema_version, status, entries}")
    if doc["schema_version"] != SCHEMA_VERSION:
        raise _fail("file", f"schema_version must be {SCHEMA_VERSION}")
    if doc["status"] not in STATUSES:
        raise _fail("file", f"status must be one of {sorted(STATUSES)}")
    raw_entries = doc["entries"]
    if not isinstance(raw_entries, list) or not raw_entries:
        raise _fail("file", "entries must be a non-empty list")
    entries = tuple(_entry(raw, i) for i, raw in enumerate(raw_entries))
    ids = [e.id for e in entries]
    if len(set(ids)) != len(ids):
        raise _fail("file", "entry ids must be unique")
    return SeedFile(status=doc["status"], entries=entries)


def load_seed_file(path: str | Path) -> SeedFile:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _fail(str(path), f"cannot read JSON: {exc}") from exc
    return parse_seed_document(doc)


def seed_store(store: MemoryStore, seeds: SeedFile, *, allow_draft: bool = False) -> int:
    """Upsert every record; returns how many. Refuses a DRAFT file unless ``allow_draft``."""
    if seeds.status != "APPROVED" and not allow_draft:
        raise MemoryValidationError(
            f"seed file status is {seeds.status}: an owner must review it and set APPROVED "
            "(or pass allow_draft for a dev/test store)"
        )
    records = seeds.records()
    for record in records:
        store.add(record)
    return len(records)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m afe_vector_memory.seed", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("validate", help="validate a seed file, write nothing")
    check.add_argument("path")
    load = sub.add_parser("load", help="upsert a seed file into the configured Chroma store")
    load.add_argument("path")
    load.add_argument("--allow-draft", action="store_true", help="dev/test stores only")
    args = parser.parse_args(argv)
    try:
        seeds = load_seed_file(args.path)
    except MemoryValidationError as exc:
        print(f"invalid seed file: {exc}", file=sys.stderr)
        return 2
    records = seeds.records()
    if args.command == "validate":
        print(f"ok: {len(seeds.entries)} entries, {len(records)} records, status={seeds.status}")
        return 0
    from .chroma_store import open_chroma_store
    from .config import VectorMemorySettings

    store = open_chroma_store(VectorMemorySettings.from_env())
    try:
        count = seed_store(store, seeds, allow_draft=args.allow_draft)
    except MemoryValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"loaded {count} records from {len(seeds.entries)} entries (status={seeds.status})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
