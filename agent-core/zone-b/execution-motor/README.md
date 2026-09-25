# Execution Motor: attestation-gated, cap-checked, halt-aware execution (Zone B)

Receives an Aegis-approved `AegisDecision` (`Aegis.SubmitSignal`'s response, forwarded
unchanged per `agent-core/docs/specs/phase_3_aegis_execution.md` section 5 and section 10)
over gRPC, re-verifies its attestation, enforces single-use and the session notional cap,
submits the order to a broker exactly once, and reports the outcome back to Aegis.
**Fail closed** at every step: nothing here trusts the caller, and no partial or ambiguous
outcome is ever reported as a success. See `execution_motor/motor.py` for the full pipeline
docstring.

Status: implemented and unit/integration tested as listed below. Not yet run against a
real Alpaca account or a real running Aegis process (see "Known limitations").

## Build and test

```bash
cd agent-core/zone-b/execution-motor
pip install -r requirements-dev.txt
ruff check --config ../../../ruff.toml .
mypy .
pytest --cov=execution_motor --cov-report=term-missing
```

`buf lint` and breaking-change checks for the proto (from `agent-core/shared/proto`):

```bash
cd agent-core/shared/proto
buf lint                                    # or the bufbuild/buf docker image, see below
BASE_REF=origin/main bash check-breaking.sh
```

**Docker build context is `agent-core/`** (not this directory):

```bash
docker build -f agent-core/zone-b/execution-motor/Dockerfile -t afe/execution-motor:dev agent-core
```

Multi-stage (`python:3.12.7-slim-bookworm`), non-root (uid/gid 10001), healthcheck is a
plain TCP connect to the listen port (no shell/curl dependency in the runtime image; see
the Dockerfile comment for why it is not an authenticated `Health` RPC). The context filter
is `Dockerfile.dockerignore` (BuildKit).

## Running

```bash
cd agent-core/zone-b/execution-motor
python -m execution_motor
```

Generated protobuf/gRPC stubs must exist at `agent-core/shared/generated` first:

```bash
bash agent-core/shared/proto/generate.sh
```

## The gRPC contract

`agent-core/shared/proto/execution_motor.proto` defines `service ExecutionMotor`:

* `rpc Execute(AegisDecision) returns (ExecuteAck)` — the only mutating RPC. Reuses
  `AegisDecision` from `aegis.proto` unchanged (same message Aegis returns from
  `SubmitSignal`), so a caller that already has that message from Aegis needs no
  translation. `ExecuteAck.status`/`reject_reason` are the string values of
  `execution_motor.models.ExecutionStatus`/`RejectReason` (not a new proto enum — see
  "Known limitations").
* `rpc Health(Empty) returns (HealthStatus)` — liveness/readiness; reflects the local
  kill-switch state only, no policy requirement beyond mTLS.

## Kill-switch reaction (ALI-162)

Implements the motor's side of `docs/specs/phase_3_aegis_execution.md` §5.2
(`execution_motor/kill_watch.py`, wired in `__main__.py`):

* The motor subscribes to `Aegis.WatchKillSwitchState` (its client certificate needs the
  `state-reader` role in Aegis's identities file, as well as `execution-reporter`).
* **New submissions** are halted at any level above NORMAL and resume when Aegis reports
  NORMAL again. The motor mirrors Aegis's level and does not latch it itself: Aegis owns
  the latches and the authenticated reset.
* **Open orders** are cancelled at LOGIC (2) and above: listed per venue
  (`Broker.list_open_orders`, Alpaca `GET /v2/orders?status=open`) and cancelled one by one
  by a sweeper thread that each state message wakes at once, well inside the spec's 1 s
  budget. Sweeps never run on the stream thread, so a slow broker cannot delay reading an
  Aegis reset. The sweep is repeated every 5 s while the level stays elevated. That catches in-flight submits, failed cancels and
  any orders beyond the first 500-order page. SOFT (1) leaves open orders alone, per the
  spec.
* **Fail closed.** Until the first state arrives, and whenever the stream breaks or ends,
  the level is treated as HARD, never NORMAL (`aegis.proto` design risk 1). Every failed
  subscribe or broken stream, including the first attempt at start-up, triggers an
  immediate sweep. So a motor that restarts with orders open while Aegis is unreachable
  halts and cancels them at once. A normal restart with Aegis healthy does not sweep. The
  watcher reconnects with backoff (1 s doubling to 30 s).
* HTTP/2 keepalive (20 s) on the Aegis channel is meant to break a silently dead connection
  so a stale NORMAL is not trusted forever.
* At HARD the supervisor may already have cut broker egress. The sweep still tries, and
  every failed list or cancel is logged at CRITICAL (`kill_sweep_list_failed`,
  `kill_sweep_cancel_failed`) for manual cancellation per `docs/runbooks/kill-switch-drill.md`.

A non-OK gRPC status means the motor could not even evaluate the decision (e.g. the server
is at its connection/thread-pool limit); every policy outcome (rejected, duplicate, halted,
unknown) is always a normal `ExecuteAck`, never a transport error.

## mTLS requirement

Mutual TLS is **required**, mirroring `agent-core/zone-b/aegis/src/config.rs` /
`server.rs`: the server certificate/key and a CLIENT CA are mandatory, and client
certificates are required with no optional-auth fallback (`require_client_auth=True`). A
connection presenting no certificate — or one that does not chain to the configured CA —
never reaches a handler; this is enforced by the TLS handshake itself, not application
code (see `tests/test_server_integration.py`, which proves this with a real socket).

Plaintext (`MOTOR_INSECURE_DEV=1`) is dev-only: refused whenever `MOTOR_ENV` is production
(or unset), and refused unless `MOTOR_LISTEN_ADDR` is an explicit loopback address (the
`0.0.0.0` default never counts).

The motor is also a gRPC **client** of Aegis, for `Aegis.ReportExecution`: that channel
uses its own mTLS material (`AEGIS_CLIENT_TLS_CA/CERT/KEY`, the same variable names
`zone-a/cognitive-core/sinks.py` uses for its Aegis channel, kept consistent on purpose).
Production refuses to start without `AEGIS_CLIENT_TLS_CA` configured.

## Configuration

### Environment variables — transport / process (`server_config.py`)

| Variable | Required | Meaning |
|---|---|---|
| `MOTOR_ENV` | no | `production` (default when unset or empty!), `staging`, `development`, `test`. Anything else refuses to start. |
| `MOTOR_LISTEN_ADDR` | no | Default `0.0.0.0:50061`. |
| `MOTOR_TLS_CERT`, `MOTOR_TLS_KEY`, `MOTOR_TLS_CLIENT_CA` | yes (unless insecure-dev) | PEM server certificate, key, and the CA client certificates must chain to. |
| `MOTOR_INSECURE_DEV` | no | Only the literal `1`: plaintext, no authentication. Refused in production; requires an explicit loopback `MOTOR_LISTEN_ADDR`. |
| `MOTOR_ATTESTATION_KEYS_FILE` | yes | JSON array of Aegis's public verification keys (below). |
| `MOTOR_USE_MOCK_BROKER` | no | Only the literal `1`: use the in-memory `MockBroker` instead of Alpaca. Refused in production. |
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | yes (unless mock broker) | Alpaca paper credentials. Required in production. |
| `ALPACA_BASE_URL` | no | Defaults to the pinned paper endpoint. Live trading needs `AFE_ENABLE_LIVE_TRADING`/`AFE_LIVE_TRADING_CONFIRM` (see `alpaca.py`) and is out of scope for this server today. |
| `AEGIS_TARGET` | yes (production) | `host:port` of Aegis, used for `ReportExecution` and the `WatchKillSwitchState` subscription (see "Kill-switch reaction"). Required in production. Outside production, unset disables both (logged as warnings at startup). |
| `AEGIS_CLIENT_TLS_CA`, `AEGIS_CLIENT_TLS_CERT`, `AEGIS_CLIENT_TLS_KEY` | yes (production, if `AEGIS_TARGET` is set) | mTLS material for the outbound Aegis channel. `CERT`/`KEY` must be set together (mutual TLS) or both omitted (server-auth-only, trusting only `CA`). |

### Environment variables — business policy (`config.py`, `MotorConfig`)

| Variable | Required | Meaning |
|---|---|---|
| `MOTOR_MAX_ORDER_NOTIONAL_USD`, `MOTOR_MAX_SESSION_NOTIONAL_USD` | yes | No defaults: a missing cap is a startup error. |
| `MOTOR_MAX_ORDER_AGE_MS`, `MOTOR_MAX_CLOCK_SKEW_MS`, `MOTOR_MAX_QUOTE_AGE_MS` | no | Defaults 5000 / 1000 / 2000 ms. |
| `MOTOR_SHORT_SELLING_ENABLED` | no | `true`/`false` (default `false`). |
| `MOTOR_STATE_DIR` | yes (production) | Durable idempotency claims (`idempotency.jsonl`, file-backed, fsynced before a claim is acknowledged — see `limits.py`). |

### Attestation key registry (`MOTOR_ATTESTATION_KEYS_FILE`)

A JSON array of Aegis's public verification keys (the motor is a **consumer** of Aegis's
signatures, so it must know Aegis's public keys out of band — never trust a caller-supplied
key):

```json
[
  {"key_id": "dev-ed25519-...", "algorithm": "ED25519", "public_key_hex": "..."},
  {"key_id": "hsm-p256-...", "algorithm": "ECDSA_P256_SHA256", "public_key_hex": "..."}
]
```

`ED25519` (dev, software-signed) is refused whenever `MOTOR_ENV` is production. Both
algorithms are verified by the same dual-verifier (`execution_motor/verifiers.py`): dev
Ed25519 over the SHA-256 digest of the canonical text, HSM ECDSA P-256 over the text
itself with SHA-256 — this asymmetry matches exactly what Aegis's own signer does on each
path (`aegis/src/signing/dev.rs` vs `aegis/src/signing/pkcs11.rs`).

## Known limitations

* **Not tested against a real Alpaca account or a real running Aegis process.** No Alpaca
  paper credentials or a live Aegis instance were available while building this. What was
  tested instead: (1) `AlpacaPaperBroker` against a mocked `httpx` transport (pre-existing,
  see `tests/test_alpaca_http.py`/`test_alpaca_endpoint.py`); (2) `AegisReporter` against
  `InMemoryReportTransport` and a fake gRPC stub (pre-existing, `test_single_use_and_reporting.py`);
  (3) the new gRPC server against a **real** `grpc.Server` bound to loopback with ephemeral
  mTLS certificates and the genuine Aegis-signed fixture in
  `tests/fixtures/aegis_attestations.json` (real Ed25519 signatures from the actual Aegis
  Rust crate — see `tests/test_server_integration.py`). `MOTOR_USE_MOCK_BROKER=1` and
  `MockBroker` exist specifically so this service can be exercised end to end without a
  real broker in non-production environments.
* **Aegis reporting channel is never exercised against a real Aegis server**; only against
  the fakes above. `AEGIS_TARGET` unset simply disables reporting (logged, not fatal),
  which is itself unverified in production-shaped conditions.
* **Kill-switch reaction is tested against a Python fake of Aegis's streaming RPC over a
  real loopback `grpc.Server`** (`tests/test_kill_watch_grpc.py`), not against the Rust
  Aegis. Not verified: the keepalive settings against tonic's HTTP/2 ping policy, Alpaca's
  real `GET /v2/orders?status=open` response shape and page limit, and the timing with a
  real broker round trip. A kill-switch drill in the paper environment is still required
  (`docs/runbooks/kill-switch-drill.md`).
* **Broker-gateway-vs-motor-holds-credentials is ADR-004, still Proposed.** This server
  currently holds Alpaca credentials directly (env vars), following the existing
  `alpaca.py`/`alpaca_broker_from_env` design; it does not resolve or anticipate ADR-004's
  outcome, and should be revisited if/when that ADR is accepted.
* `ExecuteAck.status`/`reject_reason` are carried as plain strings (the existing Python
  enum values), not a dedicated proto enum mirroring `execution_motor.models`. This keeps
  the new proto small and avoids a second enum to keep in sync with the Python one, at the
  cost of no compile-time enum safety for non-Python callers.
* The container `HEALTHCHECK` is a plain TCP connect (like Aegis's), not an authenticated
  `Health` RPC: the healthcheck process would need its own client certificate, which the
  runtime image does not carry.
* No rate limiting / concurrency cap on the gRPC server itself (Aegis bounds concurrency
  with a semaphore; this server relies on the default `ThreadPoolExecutor` size passed to
  `build_grpc_server`).
