"""Cross-package integration: manifests are written write-once AND recorded in the REAL hash-chained
audit log
(afe_audit + PostgreSQL container running the audit-logger's init procedure). Test-time only
dependency on the
sibling audit-logger directory; the manifest package itself only knows the AuditSink protocol."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import psycopg2
import pytest
from afe_audit import AuditLogger, ChainVerifier, DsnConnectionSource
from factories import make_inputs
from pg_harness import PgInstance, docker_available, postgres_container

from afe_manifest import (
    GENESIS_HASH,
    FilesystemManifestStore,
    ManifestAuditError,
    build_manifest,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="Docker not available"),
]


@pytest.fixture(scope="module")
def pg() -> Iterator[PgInstance]:
    with postgres_container() as instance:
        yield instance


def test_each_stored_manifest_is_recorded_in_the_real_audit_chain(
    pb: SimpleNamespace, pg: PgInstance, tmp_path: Path
) -> None:
    source = DsnConnectionSource(pg.app_dsn)
    store = FilesystemManifestStore(
        tmp_path / "m", AuditLogger(source), actor="manifest-service-test"
    )
    first = store.put(build_manifest(make_inputs(pb, prev=GENESIS_HASH), id_factory=lambda: "m-1"))
    second = store.put(build_manifest(make_inputs(pb, prev=first.digest), id_factory=lambda: "m-2"))

    verifier = ChainVerifier(source)
    assert verifier.verify_chain().ok
    with psycopg2.connect(pg.app_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT event_type, actor, payload->>'manifest_id', payload->>'manifest_digest' "
            "FROM audit.audit_events ORDER BY seq"
        )
        rows = cur.fetchall()
    assert rows == [
        ("manifest.stored", "manifest-service-test", "m-1", first.digest),
        ("manifest.stored", "manifest-service-test", "m-2", second.digest),
    ]
    assert store.verify_store().ok


def test_audit_database_outage_blocks_the_trade_but_keeps_the_write_once_file(
    pb: SimpleNamespace, tmp_path: Path
) -> None:
    dead = AuditLogger(
        DsnConnectionSource("host=127.0.0.1 port=1 dbname=x user=x connect_timeout=1")
    )
    store = FilesystemManifestStore(tmp_path / "m", dead, actor="manifest-service-test")
    with pytest.raises(ManifestAuditError, match="NOT audited"):
        store.put(build_manifest(make_inputs(pb, prev=GENESIS_HASH), id_factory=lambda: "m-1"))
    assert store.get(1)["manifest"]["manifest_id"] == "m-1"
