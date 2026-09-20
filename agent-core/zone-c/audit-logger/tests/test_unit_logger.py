"""Docker-free tests: fail-closed behaviour of AuditLogger with a broken connection, config validation."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg2
import pytest

from afe_audit import (
    AuditError,
    AuditEvent,
    AuditLogger,
    AuditValidationError,
    AuditWriteError,
    DsnConnectionSource,
)


class _BrokenSource:
    """Connection source whose connection raises on the first statement (e.g. server went away)."""

    def __init__(self) -> None:
        self.rolled_back = False

    @contextmanager
    def connection(self) -> Iterator[Any]:
        source = self

        class Conn:
            def cursor(self) -> Any:
                raise psycopg2.OperationalError("server closed the connection")

            def commit(self) -> None:  # pragma: no cover - must never be reached
                raise AssertionError("commit must not be attempted after a failure")

            def rollback(self) -> None:
                source.rolled_back = True

        yield Conn()


def test_database_failure_becomes_audit_write_error_and_rolls_back() -> None:
    source = _BrokenSource()
    with pytest.raises(AuditWriteError, match="OperationalError"):
        AuditLogger(source).record("t", "a", {})
    assert source.rolled_back


def test_non_database_failure_also_propagates_and_never_commits() -> None:
    def bad_clock() -> Any:
        raise RuntimeError("clock unavailable")

    class Cursor:
        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def execute(self, *args: object) -> None:
            return None

        def fetchone(self) -> None:
            return None

    class Source:
        @contextmanager
        def connection(self) -> Iterator[Any]:
            class Conn:
                def cursor(self) -> Cursor:
                    return Cursor()

                def commit(self) -> None:  # pragma: no cover
                    raise AssertionError("must not commit")

                def rollback(self) -> None:
                    return None

            yield Conn()

    with pytest.raises(RuntimeError, match="clock unavailable"):
        AuditLogger(Source(), clock=bad_clock).record("t", "a", {})


def test_invalid_timeouts_are_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        AuditLogger(_BrokenSource(), lock_timeout_ms=0)


@pytest.mark.parametrize(
    ("event_type", "actor"), [("", "a"), ("t", ""), ("  ", "a"), ("t", "x" * 201), ("t\x00", "a")]
)
def test_event_labels_are_validated(event_type: str, actor: str) -> None:
    with pytest.raises(AuditValidationError):
        AuditEvent(event_type, actor, {})


def test_event_payload_must_be_a_mapping() -> None:
    with pytest.raises(AuditValidationError, match="mapping"):
        AuditEvent("t", "a", [1, 2])  # type: ignore[arg-type]


def test_dsn_source_from_env_requires_every_variable() -> None:
    env = {
        "POSTGRES_HOST": "h",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DB": "d",
        "POSTGRES_USER": "u",
        "POSTGRES_PASSWORD": "p w'd",
    }
    DsnConnectionSource.from_env(env)
    with pytest.raises(AuditError, match="POSTGRES_PASSWORD"):
        DsnConnectionSource.from_env({k: v for k, v in env.items() if k != "POSTGRES_PASSWORD"})
    with pytest.raises(AuditError, match="empty DSN"):
        DsnConnectionSource("  ")
