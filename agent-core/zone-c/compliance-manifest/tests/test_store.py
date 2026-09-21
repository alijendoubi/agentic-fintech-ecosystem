from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from factories import FakeAudit, make_inputs, make_ptc

from afe_manifest import (
    GENESIS_HASH,
    FilesystemManifestStore,
    ManifestAuditError,
    ManifestStoreError,
    build_manifest,
)


def _mk(pb: SimpleNamespace, prev: str, n: int, **kw: Any) -> Any:
    return build_manifest(
        make_inputs(pb, prev=prev, **kw),
        clock_ns=lambda: 1_800_000_000_000_000_000 + n,
        id_factory=lambda: f"m-{n}",
    )


@pytest.fixture
def store(tmp_path: Path) -> tuple[FilesystemManifestStore, FakeAudit]:
    audit = FakeAudit()
    return FilesystemManifestStore(tmp_path / "manifests", audit, actor="svc-test"), audit


def test_put_writes_a_read_only_file_and_audits_it(
    pb: SimpleNamespace, store: tuple[FilesystemManifestStore, FakeAudit]
) -> None:
    fs, audit = store
    built = _mk(pb, GENESIS_HASH, 1)
    stored = fs.put(built)
    assert stored.seq == 1
    assert stored.path.name == "000000000001.json"
    assert stored.path.read_text(encoding="utf-8").strip() == built.document_json
    assert not os.access(stored.path, os.W_OK)
    assert not stored.path.stat().st_mode & stat.S_IWUSR
    assert audit.events == [
        (
            "manifest.stored",
            "svc-test",
            {
                "manifest_id": "m-1",
                "seq": 1,
                "manifest_digest": built.digest,
                "prev_manifest_hash": GENESIS_HASH,
                "retain_until": built.document["retention"]["retain_until"],
                "file": "000000000001.json",
            },
        )
    ]
    assert fs.get(1)["manifest_digest"] == built.digest


def test_overwrite_is_refused_even_against_a_stale_writer(
    pb: SimpleNamespace, store: tuple[FilesystemManifestStore, FakeAudit]
) -> None:
    fs, _ = store
    first = fs.put(_mk(pb, GENESIS_HASH, 1))
    original = first.path.read_bytes()
    with pytest.raises(ManifestStoreError, match="does not match the current chain head"):
        fs.put(_mk(pb, GENESIS_HASH, 2))  # forked from genesis again
    assert first.path.read_bytes() == original
    # direct low-level attempt to create the same file must hit O_EXCL
    with pytest.raises(ManifestStoreError, match="write-once"):
        FilesystemManifestStore._write_exclusive(first.path, b"evil")
    assert first.path.read_bytes() == original


def test_chain_is_enforced_and_verifiable(
    pb: SimpleNamespace, store: tuple[FilesystemManifestStore, FakeAudit]
) -> None:
    fs, _ = store
    a = fs.put(_mk(pb, GENESIS_HASH, 1))
    assert fs.latest_digest() == a.digest
    b = fs.put(_mk(pb, a.digest, 2))
    fs.put(_mk(pb, b.digest, 3))
    result = fs.verify_store()
    assert result.ok
    assert result.manifests_checked == 3


def test_verify_store_detects_file_tampering_and_gaps(
    pb: SimpleNamespace, store: tuple[FilesystemManifestStore, FakeAudit]
) -> None:
    fs, _ = store
    a = fs.put(_mk(pb, GENESIS_HASH, 1))
    b = fs.put(_mk(pb, a.digest, 2))
    fs.put(_mk(pb, b.digest, 3))
    os.chmod(b.path, stat.S_IWRITE | stat.S_IREAD)  # an attacker with OS access
    b.path.write_text(b.path.read_text(encoding="utf-8").replace("TEST", "EVIL"), encoding="utf-8")
    bad = fs.verify_store()
    assert not bad.ok
    assert "000000000002.json" in bad.problems[0]
    with pytest.raises(ManifestStoreError, match="failed verification"):
        fs.get(2)
    os.chmod(b.path, stat.S_IWRITE | stat.S_IREAD)
    b.path.unlink()
    gap = fs.verify_store()
    assert not gap.ok
    assert "sequence gap" in gap.problems[0]


def test_corrupt_head_blocks_further_writes(
    pb: SimpleNamespace, store: tuple[FilesystemManifestStore, FakeAudit]
) -> None:
    fs, _ = store
    a = fs.put(_mk(pb, GENESIS_HASH, 1))
    os.chmod(a.path, stat.S_IWRITE | stat.S_IREAD)
    a.path.write_text("not json", encoding="utf-8")
    with pytest.raises(ManifestStoreError, match="cannot read manifest file"):
        fs.put(_mk(pb, a.digest, 2))


def test_invalid_manifest_is_refused(
    pb: SimpleNamespace, store: tuple[FilesystemManifestStore, FakeAudit]
) -> None:
    fs, audit = store
    built = _mk(pb, GENESIS_HASH, 1)
    built.proto.signal.omega = 0.123  # diverges from the digested document
    with pytest.raises(ManifestStoreError, match="invalid manifest"):
        fs.put(built)
    assert audit.events == []
    assert list(fs._root.iterdir()) == []


def test_audit_failure_is_fail_closed_and_recoverable(
    pb: SimpleNamespace, store: tuple[FilesystemManifestStore, FakeAudit]
) -> None:
    fs, audit = store
    audit.fail = True
    built = _mk(pb, GENESIS_HASH, 1)
    with pytest.raises(ManifestAuditError, match="NOT audited") as info:
        fs.put(built)
    assert (info.value.seq, info.value.manifest_id) == (1, "m-1")
    assert fs.get(1)["manifest_digest"] == built.digest  # write-once evidence preserved
    audit.fail = False
    fs.reaudit(1)
    assert [e[2]["seq"] for e in audit.events] == [1]
    with pytest.raises(ManifestStoreError, match="no manifest at sequence 5"):
        fs.get(5)


def test_hard_blocked_record_can_be_stored(
    pb: SimpleNamespace, store: tuple[FilesystemManifestStore, FakeAudit]
) -> None:
    fs, _ = store
    blocked = _mk(pb, GENESIS_HASH, 1, ptc_checks=[make_ptc(pb, "limit", passed=False)], order=None)
    assert fs.put(blocked).seq == 1


def test_store_requires_an_actor_and_ignores_foreign_files(
    tmp_path: Path, pb: SimpleNamespace
) -> None:
    with pytest.raises(ManifestStoreError, match="actor"):
        FilesystemManifestStore(tmp_path, FakeAudit(), actor=" ")
    fs = FilesystemManifestStore(tmp_path / "m", FakeAudit(), actor="svc")
    (tmp_path / "m" / "notes.txt").write_text("x", encoding="utf-8")
    assert fs.latest_digest() == GENESIS_HASH
    assert fs.put(_mk(pb, GENESIS_HASH, 1)).seq == 1
