"""Real-PostgreSQL test harness: starts a throwaway postgres:16-alpine container whose first-boot
initialisation is the
project's REAL init procedure (sql/init-audit-db.sh mounted at /docker-entrypoint-initdb.d), so
tests exercise the
production role separation, triggers and DDL guard. Container is removed on teardown."""

from __future__ import annotations

import contextlib
import secrets
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg2

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
IMAGE = "postgres:16-alpine"
DB_NAME = "trading_audit_test"
SUPERUSER = "postgres"
READY_TIMEOUT_S = 120


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


@dataclass(frozen=True)
class PgInstance:
    container: str
    port: int
    superuser_password: str
    owner_password: str
    app_password: str

    def dsn(self, user: str, password: str) -> str:
        return (
            f"host=127.0.0.1 port={self.port} dbname={DB_NAME} user={user} "
            f"password={password} connect_timeout=5"
        )

    @property
    def super_dsn(self) -> str:
        return self.dsn(SUPERUSER, self.superuser_password)

    @property
    def owner_dsn(self) -> str:
        return self.dsn("afe_audit_owner", self.owner_password)

    @property
    def app_dsn(self) -> str:
        return self.dsn("afe_audit_app", self.app_password)


def _docker(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        encoding="utf-8",
    )


def _env_args(env: dict[str, str]) -> list[str]:
    args: list[str] = []
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    return args


@contextlib.contextmanager
def postgres_container(env_overrides: dict[str, str | None] | None = None) -> Iterator[PgInstance]:
    name = f"afe-pgz-test-{uuid.uuid4().hex[:10]}"
    instance = PgInstance(
        container=name,
        port=0,
        superuser_password=secrets.token_urlsafe(24),
        owner_password=secrets.token_urlsafe(24),
        app_password=secrets.token_urlsafe(24),
    )
    env: dict[str, str] = {
        "POSTGRES_USER": SUPERUSER,
        "POSTGRES_DB": DB_NAME,
        "POSTGRES_PASSWORD": instance.superuser_password,
        "AFE_AUDIT_OWNER_PASSWORD": instance.owner_password,
        "AFE_AUDIT_APP_PASSWORD": instance.app_password,
    }
    for key, value in (env_overrides or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    run = _docker(
        "run", "-d", "--name", name, "-p", "127.0.0.1::5432", *_env_args(env),
        "-v", f"{SQL_DIR}:/docker-entrypoint-initdb.d:ro", IMAGE,
    )  # fmt: skip
    if run.returncode != 0:
        raise RuntimeError(f"docker run failed: {run.stderr}")
    try:
        port_out = _docker("port", name, "5432/tcp").stdout.strip().splitlines()[0]
        ready = PgInstance(**{**instance.__dict__, "port": int(port_out.rsplit(":", 1)[1])})
        _wait_ready(ready)
        yield ready
    finally:
        _docker("rm", "-f", "-v", name)


def _wait_ready(pg: PgInstance) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_S
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with contextlib.closing(psycopg2.connect(pg.app_dsn)) as conn, conn.cursor() as cur:
                cur.execute("SELECT 1 FROM audit.audit_events LIMIT 1")
            return
        except psycopg2.Error as exc:
            last = exc
            time.sleep(1)
    logs = _docker("logs", "--tail", "40", pg.container)
    raise RuntimeError(f"postgres not ready: {last}\n{logs.stdout}\n{logs.stderr}")


def run_failing_init(env_overrides: dict[str, str | None]) -> subprocess.CompletedProcess[str]:
    """Run a foreground container whose init is expected to fail; returns the completed process."""
    name = f"afe-pgz-test-{uuid.uuid4().hex[:10]}"
    env = {
        "POSTGRES_USER": SUPERUSER,
        "POSTGRES_DB": DB_NAME,
        "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
        "AFE_AUDIT_OWNER_PASSWORD": secrets.token_urlsafe(24),
        "AFE_AUDIT_APP_PASSWORD": secrets.token_urlsafe(24),
    }
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    try:
        mount = f"{SQL_DIR}:/docker-entrypoint-initdb.d:ro"
        return _docker("run", "--name", name, *_env_args(env), "-v", mount, IMAGE, timeout=180)
    finally:
        _docker("rm", "-f", "-v", name)


_TRIGGERS_ALWAYS = (
    "audit_events_chain_insert",
    "audit_events_no_update", "audit_events_no_delete", "audit_events_no_truncate")


def _super_exec(pg: PgInstance, statements: list[str]) -> None:
    with contextlib.closing(psycopg2.connect(pg.super_dsn)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)


def restore_triggers(pg: PgInstance) -> None:
    stmts = [f"ALTER TABLE audit.audit_events ENABLE ALWAYS TRIGGER {t}" for t in _TRIGGERS_ALWAYS]
    _super_exec(pg, stmts)


@contextlib.contextmanager
def triggers_disabled(pg: PgInstance) -> Iterator[None]:
    """Simulate a privileged attacker (superuser) switching the protections off. Always restored."""
    _super_exec(pg, ["ALTER TABLE audit.audit_events DISABLE TRIGGER USER"])
    try:
        yield
    finally:
        restore_triggers(pg)


def reset_chain(pg: PgInstance) -> None:
    """Test isolation only: empty the table as a superuser with protections temporarily off."""
    with triggers_disabled(pg):
        _super_exec(pg, ["TRUNCATE audit.audit_events"])
