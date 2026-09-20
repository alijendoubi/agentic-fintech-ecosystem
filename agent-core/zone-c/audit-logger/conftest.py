"""`audit-logger` has a hyphen in its directory name, so it cannot be imported as a dotted package.
Put this directory on sys.path so the real package `afe_audit` (and tests/ helpers) import flat.
Also hosts the real-PostgreSQL fixtures (session-scoped container, per-test chain reset)."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from pg_harness import PgInstance, docker_available, postgres_container, reset_chain  # noqa: E402

from afe_audit import AuditLogger, ChainVerifier, DsnConnectionSource  # noqa: E402


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if docker_available():
        return
    skip = pytest.mark.skip(
        reason="Docker not available: integration tests need a real PostgreSQL container"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def pg() -> Iterator[PgInstance]:
    with postgres_container() as instance:
        yield instance


@pytest.fixture
def fresh_pg(pg: PgInstance) -> PgInstance:
    reset_chain(pg)
    return pg


@pytest.fixture
def logger(fresh_pg: PgInstance) -> AuditLogger:
    return AuditLogger(DsnConnectionSource(fresh_pg.app_dsn))


@pytest.fixture
def verifier(fresh_pg: PgInstance) -> ChainVerifier:
    return ChainVerifier(DsnConnectionSource(fresh_pg.app_dsn))
