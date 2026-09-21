"""Connection sources. The audit logger only ever connects as the INSERT-only application role
(``afe_audit_app``); it must never be given the owner or a superuser credential."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Protocol

import psycopg2
from psycopg2.extensions import connection as PgConnection
from psycopg2.extensions import make_dsn

from afe_audit.errors import AuditError

ENV_KEYS = ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")


class ConnectionSource(Protocol):
    """Yields a connection for one unit of work and disposes of it afterwards (close or
    return-to-pool)."""

    def connection(self) -> Any:  # a context manager yielding a psycopg2 connection
        ...


class DsnConnectionSource:
    """Opens a fresh connection per unit of work. Inject a pooled ConnectionSource for higher
    throughput."""

    def __init__(self, dsn: str, connect_timeout_s: int = 5) -> None:
        if not dsn.strip():
            raise AuditError("empty DSN")
        self._dsn = dsn
        self._timeout = connect_timeout_s

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> DsnConnectionSource:
        source = os.environ if env is None else env
        missing = [key for key in ENV_KEYS if not source.get(key)]
        if missing:
            names = ", ".join(missing)
            raise AuditError(f"missing database environment variables: {names}")
        dsn = make_dsn(
            host=source["POSTGRES_HOST"],
            port=source["POSTGRES_PORT"],
            dbname=source["POSTGRES_DB"],
            user=source["POSTGRES_USER"],
            password=source["POSTGRES_PASSWORD"],
        )
        return cls(dsn)

    @contextmanager
    def connection(self) -> Iterator[PgConnection]:
        conn = psycopg2.connect(self._dsn, connect_timeout=self._timeout)
        try:
            yield conn
        finally:
            conn.close()
