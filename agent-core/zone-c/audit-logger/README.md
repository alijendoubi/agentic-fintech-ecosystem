# audit-logger (Zone C) — append-only, hash-chained audit trail (ALI-49)

Implemented and tested against a real PostgreSQL 16 container. What is and is not guaranteed is stated explicitly
below; nothing here is a verified regulatory-compliance claim (needs qualified legal review).

## What it enforces

| Property | Mechanism |
|---|---|
| App role can only append | `afe_audit_app` has `SELECT, INSERT` on `audit.audit_events` only; no DDL, no `TEMP`, no schema/db `CREATE`, not owner, not superuser |
| No UPDATE / DELETE / TRUNCATE for **anyone** | statement-level `BEFORE` triggers raise (`restrict_violation`); `ENABLE ALWAYS`, so they also fire under `session_replication_role = replica`. Verified for the app role, the table owner and a superuser |
| Owner cannot quietly remove the triggers | `900_ddl_guard.sql` event triggers reject `ALTER TABLE/FUNCTION/...`, `DROP` of anything in schema `audit` for non-superusers |
| Chain cannot fork/gap/forge at insert time | `BEFORE INSERT` trigger takes the chain advisory lock, requires `seq = head+1`, `prev_hash = head.hash`, `hash = sha256(canonical)` and column/canonical consistency; `UNIQUE(prev_hash)` and `UNIQUE(hash)` |
| Tampering detectable | `ChainVerifier` (Python, independent) and `audit.verify_chain(from, to)` (SQL) return the **first** break: `seq`, defect code, expected/actual |
| Tail truncation / consistent rewrite detectable | external anchors (`AnchorPublisher` + `AnchorSink`; `FileAnchorSink` reference impl) checked with `ChainVerifier.verify_anchors` |
| Fail closed | `AuditLogger.append` returns only after COMMIT. Any failure raises `AuditError` subclasses (`AuditValidationError`, `AuditWriteError`); **the caller must not perform the audited action** |

### Not guaranteed (residual risk)

* A PostgreSQL **superuser** (or anyone with host/OS access to the data files) can still alter data or drop the
  guards. That is detected only by `verify_chain` (inside-table edits) and by **anchors** (truncation / full
  rewrite). Anchors are only as strong as the sink: `FileAnchorSink` on the same host proves nothing against a
  host-level attacker. TODO(owner): provision write-once, out-of-domain anchor storage and an anchoring schedule.
* If a commit is in flight when the connection drops, the outcome is unknown: `AuditWriteError` is raised, the
  chain remains valid either way, and the caller must not proceed or blindly retry.
* No key/HSM signing of the chain head yet (would strengthen anchors).

## Database initialisation (owner/migrator vs application role)

Roles: `afe_audit_owner` (owns objects, runs migrations, **never given to services**) and `afe_audit_app`
(runtime; the only credential services receive). Superuser is used only at first boot.

`sql/init-audit-db.sh` runs the migrations in order (mount the whole `sql/` dir at
`/docker-entrypoint-initdb.d`; the postgres image runs top-level `*.sh` on first initialisation only):

| Step | Run as | File |
|---|---|---|
| 1 | superuser | `migrations/000_bootstrap_roles.sql` (roles, database privileges; passwords via `\getenv`) |
| 2 | `afe_audit_owner` | `migrations/001_audit_events.sql` (table, triggers, grants) |
| 3 | `afe_audit_owner` | `migrations/002_verify_chain.sql` |
| 4 | superuser | `migrations/900_ddl_guard.sql` (DDL lock for non-superusers) |

Required environment: `POSTGRES_USER`, `POSTGRES_DB`, `POSTGRES_PASSWORD` (standard), plus
`AFE_AUDIT_OWNER_PASSWORD` and `AFE_AUDIT_APP_PASSWORD` (>= 16 chars, different). Missing/weak passwords abort init.
Services connect with `POSTGRES_USER=afe_audit_app`, `POSTGRES_PASSWORD=$AFE_AUDIT_APP_PASSWORD`.
Future schema changes require a superuser to drop `afe_audit_ddl_guard_end/_drop`, migrate as owner, re-run 900.
Shell scripts and SQL must be checked out with LF line endings.

## Canonical serialisation (hash contract v1)

Hashed document = JSON object `{actor, event_type, occurred_at, payload, prev_hash, seq, v}`:
sorted keys, no whitespace, all non-ASCII escaped as `\uXXXX` (pure-ASCII text), **no Unicode normalisation**,
NUL and lone surrogates rejected, integers only within int64 (booleans are not numbers), **floats rejected**
(carry decimals as strings/scaled ints — text form of floats is not stable across languages and jsonb re-renders
numerics), depth <= 32, size <= 1 MiB, `occurred_at` = UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ`,
`hash = sha256(utf8(canonical))` lowercase hex, genesis `prev_hash` = 64 zeros. The exact canonical text is stored in
`canonical`; SQL and Python verifiers recompute from it and compare it with the `payload`/`seq`/... columns.
Details: `afe_audit/canonical.py`.

## Usage

```python
from afe_audit import AuditLogger, DsnConnectionSource, AuditError

logger = AuditLogger(DsnConnectionSource.from_env())   # POSTGRES_* env, app role only
try:
    logger.record("order.submitted", actor="aegis", payload={"order_id": "...", "qty": 10})
except AuditError:
    raise  # do NOT place the order: it was not audited
```

`ChainVerifier(source).verify_chain()` / `verify_chain(from_seq, to_seq)`;
`AnchorPublisher(verifier, FileAnchorSink(path)).run_periodic(interval_s, stop_event)`.

## KL drift monitor (`afe_audit/drift.py`)

Pure numpy functions (`kl_divergence`, `align_counts`, `classify`) and `DriftMonitor`. Thresholds
(`KL_SOFT_ALERT_THRESHOLD`, `KL_SOFT_SWITCH_THRESHOLD`, `KL_LOGIC_SWITCH_THRESHOLD`) and the smoothing `epsilon`
are **required, uncalibrated inputs** — there are no built-in defaults; the 0.1/0.3/0.5 values in docker-compose are
placeholders. TODO(owner): calibrate and document (nothing in `docs/regulatory/` sets KL values).
Non-finite KL fails closed to the most severe level. `DriftReading.to_audit_payload()` is float-free for logging.

## Tests

```bash
cd agent-core/zone-c/audit-logger
python -m pytest tests --cov=afe_audit        # integration tests start a postgres:16-alpine container
python -m ruff check --config ../../../ruff.toml . && python -m mypy
```
Integration tests mount `sql/` into a throwaway container so the real init procedure is what is tested; the container
is removed afterwards. They are skipped (not failed) when Docker is unavailable.
