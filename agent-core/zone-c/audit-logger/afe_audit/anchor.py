"""Tamper-evidence anchoring.

A hash chain inside one database cannot prove that the *tail* was not truncated or that the whole
suffix was not
consistently rewritten by a privileged attacker. Periodically publishing the head hash to a sink
OUTSIDE the
database's trust domain (WORM bucket, separate host, transparency log, ...) closes that gap:
``ChainVerifier.verify_anchors`` then flags any anchored (seq, hash) that the table no longer
reproduces.

``AnchorSink`` is the interface; ``FileAnchorSink`` is a reference implementation (append-only JSON
lines, each line
additionally hash-linked to the previous line so the file itself is tamper-evident). A production
sink must be
write-once storage that the audit database host cannot modify -- TODO(owner): choose and provision
it.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from afe_audit.canonical import GENESIS_HASH
from afe_audit.errors import AnchorError
from afe_audit.models import Anchor
from afe_audit.verifier import ChainVerifier


class AnchorSink(Protocol):
    def emit(self, anchor: Anchor) -> None:
        """Durably publish the anchor or raise AnchorError."""
        ...


class FileAnchorSink:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    def emit(self, anchor: Anchor) -> None:
        with self._lock:
            try:
                previous = self._last_line_hash()
                body = {
                    "seq": anchor.seq,
                    "hash": anchor.hash,
                    "emitted_at": anchor.emitted_at,
                    "prev_line": previous,
                }
                line = json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n"
                fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    os.write(fd, line.encode("utf-8"))
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError as exc:
                raise AnchorError(f"cannot write anchor file {self._path}: {exc}") from exc

    def read_all(self) -> list[Anchor]:
        """Parse the file and verify its internal line-hash chain. Any malformation raises
        AnchorError."""
        if not self._path.exists():
            return []
        anchors: list[Anchor] = []
        previous = GENESIS_HASH
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise AnchorError(f"cannot read anchor file {self._path}: {exc}") from exc
        for number, line in enumerate(lines, start=1):
            try:
                doc = json.loads(line)
                anchor = Anchor(
                    seq=int(doc["seq"]), hash=str(doc["hash"]), emitted_at=str(doc["emitted_at"])
                )
                linked = doc["prev_line"]
            except (ValueError, KeyError, TypeError) as exc:
                raise AnchorError(f"anchor file line {number} is malformed") from exc
            if linked != previous:
                raise AnchorError(f"anchor file line {number} breaks the line-hash chain")
            previous = hashlib.sha256(line.encode("utf-8")).hexdigest()
            anchors.append(anchor)
        return anchors

    def _last_line_hash(self) -> str:
        if not self._path.exists():
            return GENESIS_HASH
        lines = self.read_all()  # validates the whole file before extending it
        if not lines:
            return GENESIS_HASH
        last = self._path.read_text(encoding="utf-8").splitlines()[-1]
        return hashlib.sha256(last.encode("utf-8")).hexdigest()


class AnchorPublisher:
    """Emits the current chain head to a sink, once or periodically."""

    def __init__(
        self,
        verifier: ChainVerifier,
        sink: AnchorSink,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._verifier = verifier
        self._sink = sink
        self._clock = clock

    def publish(self) -> Anchor | None:
        head = self._verifier.head()
        if head is None:
            return None
        anchor = Anchor(
            seq=head[0], hash=head[1], emitted_at=self._clock().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        )
        self._sink.emit(anchor)
        return anchor

    def run_periodic(
        self, interval_s: float, stop: threading.Event, max_iterations: int | None = None
    ) -> int:
        """Publish every ``interval_s`` seconds until ``stop`` is set. Errors propagate (fail
        closed): a scheduler
        must treat an exception here as an incident, not retry silently. Returns the number of
        publishes."""
        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        count = 0
        while not stop.is_set():
            self.publish()
            count += 1
            if max_iterations is not None and count >= max_iterations:
                break
            stop.wait(interval_s)
        return count
