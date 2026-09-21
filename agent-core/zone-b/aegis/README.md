# Aegis: deterministic pre-trade risk gate (Zone B)

Aegis sits between LLM trade signals (Zone A) and the execution path. It
evaluates every signal against pre-trade controls C01-C19, maintains the
kill-switch hierarchy, and signs a short-lived attestation over each approved
order. **Default deny**: every failure, missing configuration, parse error,
expired signal, unknown enum, missing kill-switch state, replay, HSM/audit/state
failure or timeout is a REJECT (or HELD_FOR_HUMAN where the spec says so).

Authority: `agent-core/docs/specs/phase_3_aegis_execution.md`. Where the spec
says PROPOSED the proposal is implemented and the value is configurable and
marked `TODO(owner)` below. Nothing here is a regulatory claim (requires
qualified legal review).

Status of this crate: implemented and tested as listed in "Exit-criteria
mapping". Not implemented: see "Not implemented / known gaps".

## Build and test

Rust cannot be built natively on the dev machine; use the `afe-rust-dev` image
(rust:1-bookworm + protoc + clippy + rustfmt).

```bash
# from the repo root, Git Bash
MSYS_NO_PATHCONV=1 docker run --rm -v "$(pwd -W):/src:ro" -v afe-cargo-cache:/usr/local/cargo/registry \
  -v afe-cargo-target-pkge:/target -e CARGO_TARGET_DIR=/target -w /work afe-rust-dev bash -c \
  'cd /src && tar --exclude=target --exclude=.git -cf - agent-core | tar -xf - -C /work \
   && cd /work/agent-core/zone-b/aegis \
   && cargo fmt --check && cargo clippy --all-targets -- -D warnings \
   && cargo clippy --all-targets --features pkcs11 -- -D warnings && cargo test'
```

`build.rs` reads the protos from `AEGIS_PROTO_DIR`, else `../../shared/proto`
(valid in the repo and inside the Docker build).

**Docker build context is `agent-core/`** (not this directory):

```bash
docker build -f agent-core/zone-b/aegis/Dockerfile -t afe/aegis:dev agent-core
# optional PKCS#11 signer: --build-arg AEGIS_FEATURES=pkcs11
```

Multi-stage, distroless/cc runtime, non-root (uid 65532), healthcheck is
`aegis healthcheck` (TCP connect to the listen port, no shell/procps needed).
The context filter is `Dockerfile.dockerignore` (BuildKit).

## Configuration

### Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `AEGIS_ENV` | no | `production` (default when unset or empty!), `staging`, `development`, `test`. Anything else refuses to start. |
| `AEGIS_LIMITS_FILE` | yes | Owner-supplied limits JSON (below). No defaults for risk limits. |
| `AEGIS_IDENTITIES_FILE` | yes | Peer roles and approver public keys JSON (below). |
| `AEGIS_STATE_DIR` | yes | Durable state: `kill_state.json`, `replay.log`, `portfolio.json`, `audit.wal`. Mount a persistent volume; do not delete files individually (see kill switch). |
| `AEGIS_TLS_CERT`, `AEGIS_TLS_KEY`, `AEGIS_TLS_CLIENT_CA` | yes (TLS) | PEM server certificate, key, and the CA that client certificates must chain to. Client certificates are REQUIRED. |
| `AEGIS_ALLOW_FRESH_STATE` | no | Only the literal `1`. In production (or unset `AEGIS_ENV`) an EMPTY state dir is refused at start (it may be a wiped or wrongly mounted volume, which would silently reset every latch, the replay history and the portfolio); set this for the very first start only, then remove it. Outside production an empty dir bootstraps freely. A state dir that already holds files but lacks `kill_state.json`, `portfolio.json` or `replay.log` is never a first boot: Aegis starts at persisted HARD instead. |
| `AEGIS_INSECURE_DEV` | no | Only the literal `1`: plaintext, no authentication. Refused when `AEGIS_ENV` is production or unset, and requires an explicit loopback `AEGIS_LISTEN_ADDR` (`127.0.0.1:port` / `[::1]:port`); the `0.0.0.0` default does not qualify. The server also refuses to serve plaintext on any non-loopback listener. |
| `AEGIS_SIGNER` | yes | `dev` (software Ed25519, DEV ONLY, refused in production) or `pkcs11`. |
| `AEGIS_DEV_SIGNING_SEED_FILE` | no | 64 hex chars; else an ephemeral key per start. |
| `AEGIS_PKCS11_MODULE`, `AEGIS_PKCS11_TOKEN_LABEL`, `AEGIS_PKCS11_KEY_LABEL`, `AEGIS_PKCS11_PIN_FILE` | yes for `pkcs11` | Module path, token and key labels (EC P-256 private key), file holding the user PIN. The PIN is never logged and not in `Debug` output. Requires a build with `--features pkcs11`. |
| `AEGIS_LISTEN_ADDR` | no | Default `0.0.0.0:50051` (mTLS only; see `AEGIS_INSECURE_DEV`). |
| `AEGIS_MAX_CONCURRENCY` | no | Default 64; excess unary calls fail fast with RESOURCE_EXHAUSTED. |
| `AEGIS_SUBMIT_TIMEOUT_MS` / `AEGIS_RPC_TIMEOUT_MS` | no | Deadline caps, default 100 / 2000 ms. DEADLINE_EXCEEDED is a reject for the caller. A `SubmitSignal` whose caller has timed out or disconnected is cancelled: it does not sign, and a reservation it already committed is released (see Known limitations for the residual window). |
| `RUST_LOG` | no | tracing filter, default `info` (JSON lines). |

The service refuses to start (exit code 2) on any missing/invalid required
setting, on unreadable or incomplete limits, when the audit WAL cannot be
opened while `audit_mandatory` is true, and when the signer cannot be built.

### Limits file (`AEGIS_LIMITS_FILE`)

JSON, `deny_unknown_fields`, every field below REQUIRED except `timings`. There
are NO permissive defaults: a missing field, a zero or negative limit, or an
empty allowlist is a parse error and Aegis does not start. The SHA-256 of the
raw file is exposed via `GetAegisState.limits_config_sha256` and embedded in
every attestation. All values are `TODO(owner)` unless stated; the numbers in
the spec's table are UNSUBSTANTIATED until Phase 4 calibration.

| Field | Control | Notes |
|---|---|---|
| `symbols` | C03, C10, C11, C13 | Map symbol -> `max_order_qty_nanos`, `max_order_notional_nanos`, `max_position_nanos`. The keys ARE the allowlist, so no symbol can be allowed without explicit caps. |
| `short_selling_enabled` | C04 | Explicit bool. PROPOSED false; enabling needs legal review, and shorts are not supported end to end yet (see NOTES). |
| `accept_legacy_double_fields` | C02 | Accept deprecated `double` money fields converted at the boundary (spec section 3). Otherwise only `_nanos` fields are read. |
| `session` | C05 | Fixed UTC window (`weekdays_utc` ISO 1-7, `start_minute_utc`, `end_minute_utc`); no DST or holiday calendar. TODO(owner): calendar source. |
| `price_collar_bps` | C09 | [EXISTING 1.5% = 150] UNSUBSTANTIATED. |
| `max_order_adv_bps` | C12 | [EXISTING 0.5% = 50] of `adv_30d`. UNSUBSTANTIATED (docs say 20-day ADV, snapshot has 30-day). |
| `max_gross_exposure_nanos` | C14 | |
| `max_daily_drawdown_bps` | C15 | [EXISTING 2.0% = 200] UNSUBSTANTIATED. Breach latches LOGIC. |
| `rate_global`, `rate_per_symbol` | C16 | Token bucket `capacity` and `refill_per_sec`. |
| `omega_min` | C17 | [EXISTING 0.55] UNSUBSTANTIATED. Below it the signal is an abstain (hard reject). |
| `regime.min_confidence`, `regime.allowed` | C18 | `C_MIN` (PROPOSED 0.6) and the regime names the strategy is validated for. |
| `hold_requires_second_approver`, `hold_requires_cooling_period`, `hold_max_distress_score` | ResolveHold | TODO(owner): whether a second approver / cooling period is mandatory. |
| `audit_mandatory` | audit | If true a sink failure rejects the decision (`REASON_AUDIT_UNAVAILABLE`) and refuses a kill reset. |
| `timings` (optional) | various | PROPOSED defaults, all TODO(owner): `clock_skew_ms` 250, `max_signal_age_ms` 5000, `max_ref_age_ms` 1000, `max_regime_age_ms` 60000 (not in spec), `attestation_ttl_ms` 5000, `hold_window_ms` 60000, `replay_retention_ms` 86400000, `operator_heartbeat_interval_ms` 14400000, `operator_heartbeat_warn_ms` 12600000, `liveness_trip_ms` 60000, `approval_max_age_ms` 300000, `unusual_size_min_history` 30, `unusual_size_window_days` 30. |

### Identities file (`AEGIS_IDENTITIES_FILE`)

Operator authentication is `TODO(owner)` in the spec; this implements the
PROPOSED option (mTLS client certificate per identity) plus signed approvals.

```json
{"peers": {"cognitive-core": ["signal-submitter"],
           "operator-console": ["operator", "kill-trigger", "kill-reset", "hold-resolver", "state-reader"],
           "execution-motor": ["state-reader", "execution-reporter"],
           "market-data": ["market-data-writer"],
           "supervisor": ["state-reader", "kill-trigger"]},
 "approvers": {"alice": {"roles": ["operator"], "ed25519_pubkey_hex": "<64 hex>"}}}
```

`peers` maps the client certificate subject CN (else first DNS/URI SAN) to RPC
roles. A verified certificate that is not listed has no roles (PERMISSION_DENIED).

| RPC | Required role |
|---|---|
| SubmitSignal | `signal-submitter` |
| ResolveHold | `hold-resolver`; `operator_id` must equal the certificate identity |
| TriggerKillSwitch | `kill-trigger` |
| ResetKillSwitch | `kill-reset` plus signed approvals (below) |
| Heartbeat | `operator`; `operator_id` must equal the certificate identity |
| GetKillSwitchState, WatchKillSwitchState, GetAegisState | `state-reader` |
| ReportExecution | `execution-reporter` |
| PushReferenceData | `market-data-writer` (nothing else; default deny) |

## Reference data feed (`PushReferenceData`)

Aegis trusts nothing from Zone A, so market data and the regime label reach
`MemoryReferenceData` (`App.refdata`) only through the additive RPC
`PushReferenceData` (not in spec Appendix A.1; `buf breaking` against
`integration/wave1` passes). Caller role: `market-data-writer`.

A request carries `ReferenceSnapshot`s (`symbol`, `mid_price_nanos`,
`adv_30d_nanos`, `as_of_ns`, `is_stale`, all int64 nanos) and/or one
`RegimeLabelPacket` (label, confidence in [0,1], `timestamp_ns`). The push is
NOT atomic: each item is checked and refused individually (`rejected` lists
`key` + `reason`); refused items are never stored. An item is applied only if:

* the symbol is on the C03 allowlist (`unknown_symbol`), at most 512 per push;
* mid, ADV and `as_of_ns` are strictly positive (`invalid`);
* `as_of_ns` is not older than `timings.max_ref_age_ms` (regime:
  `max_regime_age_ms`) against Aegis's own clock (`stale`) and not more than
  `clock_skew_ms` ahead (`future`): the same bounds C08 / C18 enforce, so there
  is no default that accepts stale data (the limits file sets them; default
  1000 ms for prices);
* it is strictly newer than the stored value for that symbol / the regime
  (`out_of_order`, duplicates included).

`is_stale = true` from the producer is stored, so C08 fails until a fresh
snapshot replaces it. Data also ages out by itself: a feed that stops makes C08
fail after `max_ref_age_ms`, and `GetAegisState.reference_data_fresh` is
age-aware. A service built without a store refuses the RPC
(`FAILED_PRECONDITION`). Tests: `src/state/ingest/tests.rs`,
`tests/reference_data_push.rs` (real mTLS, including C08 fail then pass).

## Supervisor (`aegis supervisor`)

The spec's independent liveness watchdog is the same binary started with the
`supervisor` argument (separate process, no shared state with Aegis). Every
`AEGIS_SUPERVISOR_PROBE_INTERVAL_MS` it calls `GetAegisState` over mTLS (this
runs on Aegis's engine worker pool, so a wedged engine fails the probe). When
probes have failed continuously for MORE than the trip window it calls
`TriggerKillSwitch(HARD, actor_id "supervisor/liveness")` (Aegis records the
latch actor as `<cert CN>:supervisor/liveness`) and requires the answer to show
an effective level of at least HARD. One latch per outage; it re-arms after Aegis
answers again. The window uses a monotonic clock and starts when the Supervisor
starts, so an Aegis that never came up is tripped one window later.

| Variable | Required | Meaning |
|---|---|---|
| `AEGIS_ENV` | no | as for the server (unset = production) |
| `AEGIS_SUPERVISOR_TARGET` | yes | `https://host:port` (`http://` only with insecure dev) |
| `AEGIS_SUPERVISOR_TLS_CA`, `AEGIS_SUPERVISOR_TLS_CERT`, `AEGIS_SUPERVISOR_TLS_KEY` | yes (TLS) | server CA and the Supervisor's client identity; its CN needs `state-reader` + `kill-trigger` in the identities file |
| `AEGIS_SUPERVISOR_TLS_DOMAIN` | no | server name to verify (default: host of the target) |
| `AEGIS_SUPERVISOR_INSECURE_DEV` | no | literal `1`; refused when `AEGIS_ENV` is production or unset |
| `AEGIS_SUPERVISOR_TRIP_AFTER_MS` | no | default 60000; MUST be in [10000, 60000] (out of range refuses to start: 60 s is the spec value and a ceiling, the floor stops a hair trigger) |
| `AEGIS_SUPERVISOR_PROBE_INTERVAL_MS`, `AEGIS_SUPERVISOR_PROBE_TIMEOUT_MS` | no | defaults 1000 / 2000; each in [100, trip/4] |

Fails closed on its own errors (the process ends non-zero, it never idles
unprotected): exit 2 for missing/invalid config, unreadable TLS files or a bad
target; 3 when HARD stays undeliverable for a further full window after
liveness was lost (a wrong certificate role, or Aegis unreachable); 1 for an
unreadable clock. A panic aborts (release profile). Run it under a restart
policy and alert on restarts. The container healthcheck of the image
(`aegis healthcheck`, a TCP connect to `AEGIS_LISTEN_ADDR`) does not apply to it:
disable the healthcheck for the Supervisor service. Unknown subcommands now
exit 2 instead of starting the server. Tests: `src/supervisor/`,
`tests/supervisor_integration.rs` (fake clock, real in-process Aegis over mTLS).

## Kill switch (ALI-45)

One state machine (spec 5): each trigger is an independent latch; effective
level = max over active latches; automatic transitions only go up; latches are
sticky and persisted (`kill_state.json`, fsync + atomic rename) before a trip is
acknowledged.

| Level | Effect on `SubmitSignal` | Reset authority (this implementation) |
|---|---|---|
| NORMAL | evaluated normally | n/a |
| SOFT | risk-increasing signals rejected; strictly risk-reducing ones evaluated | 1 operator |
| LOGIC | all rejected; a risk-reducing signal is HELD for human release | 2 distinct operators + root-cause reference |
| DEAD_MANS | nothing approved, nothing releasable | 1 operator + an operator heartbeat received after the latch |
| HARD | as DEAD_MANS; attestations impossible | 2 operators + 1 compliance (three people, stricter reading of "dual operator + compliance sign-off"; TODO(owner) confirm) + root cause |
| PHYSICAL | as HARD | as HARD; the root-cause reference must be the incident report (Aegis cannot verify the DORA notification) |

Fail-closed rules (tests in `src/killswitch/tests.rs`, `src/engine/tests.rs`,
`tests/app_startup.rs`):

* `KILL_LEVEL_NORMAL` is the proto zero value. A missing state, a poisoned
  lock, an unreadable state file, or a state file missing from a non-empty
  state dir resolves to **HARD**, never NORMAL. A `TriggerKillSwitch` with
  level 0 or an unknown value latches HARD.
* A restart never lowers the level. A trip that cannot be persisted still
  latches in memory and adds a HARD latch. A reset is audited, persisted, and
  only then committed; any failure refuses it.
* Operator heartbeat (`Heartbeat` RPC): 4 h interval, warning at 3 h 30 min,
  DEAD_MANS on lapse (PROPOSED reconciliation, spec 5.3). A heartbeat never
  clears a latch. After a restart the persisted last heartbeat is used, so a
  stale one trips DEAD_MANS on the first tick.
* Liveness watchdog (60 s, HARD): see "Supervisor" below (`aegis supervisor`,
  built on the pure `killswitch::watchdog::LivenessWatchdog`).
* Reset approvals: each `Authorization.credential_ref` must be the hex Ed25519
  signature, by the approver's key in the identities file, over
  `afe-reset-v1\ntrigger_id=..\napprover_id=..\nrole=..\napproved_at_ns=..\n`,
  fresh within `approval_max_age_ms`. Forged, stale, unknown, replayed for
  another latch, or duplicate approvals are refused and audited. This scheme is
  invented here because the spec leaves the credential scheme `TODO(owner)`.
* Daily drawdown (C15): the first equity report of a UTC day sets that day's
  starting NAV (until then C15 fails closed). A breach latches LOGIC once.

## Attestation contract (ALI-44)

An approved decision carries an `OrderRequest` and an `Attestation`.

Canonical text `afe-attest-v1` (exactly spec section 7; UTF-8, `\n`-terminated,
decimal integers, this field order):

```
afe-attest-v1
signal_id=<uuid>
symbol=<SYM>
side=<BUY|SELL|SELL_SHORT>
order_type=<LIMIT|MARKET|STOP|STOP_LIMIT>
qty_nanos=<int>
limit_price_nanos=<int>
stop_price_nanos=<int>
decided_at_ns=<int>
expires_at_ns=<int>
aegis_state_seq=<int>
limits_config_sha256=<hex>
key_id=<string>
```

* `payload_sha256 = SHA-256(text)`. The signature is over those 32 bytes.
  ECDSA P-256 (HSM, raw `r || s`, 64 bytes): ECDSA over the SHA-256 digest is
  what standard ECDSA-SHA256 verification of the text checks, so a verifier may
  verify either `(text, sig)` with SHA-256 or `(digest, sig)` prehashed. The DEV
  Ed25519 signer signs the 32 digest bytes as its message (verify over the
  digest, NOT the text). `Attestation` has no algorithm field (frozen proto):
  the verifier selects the algorithm by `key_id` from its key registry.
* Aegis only emits LIMIT orders: a market signal is converted to a marketable
  limit at `mid +/- collar` (spec 4.5; TODO(owner) confirm), and that price is
  what is signed. `stop_price_nanos` is 0. `algo` is `DIRECT`.
* `expires_at_ns = decided_at_ns + attestation_ttl_ms` (5 s). The order mirrors
  `hsm_signature`, `hsm_key_id`, `attestation_expires_at_ns`. The deprecated
  double fields of `OrderRequest` are left zero: consumers MUST read `_nanos`.
* **Client order id**: Aegis sets `order_id == signal_id`. The text binds
  `signal_id`, so the idempotency key is covered by the signature. Verifiers
  MUST refuse `order_id != signal_id`.
* **Side mapping (resolves the ambiguity flagged in `aegis.proto`)**: the text
  carries the SIGNAL vocabulary. `ORDER_BUY` <-> `BUY`. `ORDER_SELL` <-> `SELL`
  or `SELL_SHORT` (`OrderSide` has no short value): the verifier rebuilds the
  text with `SELL`, then `SELL_SHORT`, and the one that matches the signed
  digest tells whether it is a short; a gateway that does not allow shorts MUST
  refuse `SELL_SHORT`. A SELL attestation cannot be replayed as a short (the
  digests differ).
* Verification (reference: `signing::verify::verify_attestation`, used by the
  tests): version, mirror fields, `order_id == signal_id`, expiry
  (`now <= expires_at_ns`, `decided_at_ns` not in the future beyond 250 ms),
  rebuild text from the ORDER fields plus attestation fields, compare digest,
  verify the signature under `key_id`. **Single use** (nonce = `signal_id`) is
  NOT enforced by Aegis or this helper: the broker gateway must enforce it.
* Signers: `DevEd25519Signer` (DEV ONLY; every constructor and `config` refuse
  it in production) and `HsmSigner` over a PKCS#11 backend (feature `pkcs11`,
  `CKM_ECDSA` on an EC P-256 key looked up by label, SoftHSM2 in dev).
  TODO(owner): confirm the production HSM supports this mechanism; ADR-001's
  2-of-3 MPC threshold ECDSA is unverified and not implemented.

### NOTES: relation to the execution-motor (branch worktree-agent-ad1e6cae75084407e)

`execution_motor/proto_adapter.py` rebuilds the same text (same lines, same
order, `side` BUY|SELL from `OrderSide` 1|2, `order_type` from the enum) and
checks `sha256(text) == payload_sha256`. Exact differences to resolve before
integration:

1. **SELL_SHORT**: the motor maps `ORDER_SELL` only to `SELL`. If
   `short_selling_enabled` is ever true, an attested SELL_SHORT will not verify
   in the motor (digest mismatch, fail closed). Shorts are disabled by default;
   the motor needs the try-`SELL`-then-`SELL_SHORT` logic above before enabling.
2. **Signature message**: the motor's `AttestationVerifier.verify(payload,
   signature, key_id)` receives the TEXT. HSM ECDSA-SHA256 signatures verify
   over the text. The DEV Ed25519 signature is over the digest, so a motor
   verifier for `dev-ed25519-*` keys must verify over `payload_sha256`.
3. **order_id**: the motor treats `order_id` as an unsigned hint; Aegis sets
   it equal to `signal_id` (signed). The motor should use `signal_id` or
   enforce equality when deriving the broker client order id.
4. `ReportExecution` semantics Aegis expects (the motor's `service.py` matches):
   cumulative `filled_qty_nanos`, terminal status frees the reservation,
   `account_equity_nanos` on every report; a report with an empty `order_id` is
   an equity-only update. The first equity report of a UTC day is that day's
   starting NAV, so the motor should report equity at session open.

## Controls and decisions

Every control is a pure function returning `ControlResult` with a
`ReasonCode` (`src/controls/`). The pipeline runs C01 and C03-C07 in order
(first failure returns), then evaluates ALL of C08-C19. Any hard failure =>
REJECTED; else any soft failure (C18, C19, C01 at LOGIC for risk-reducing
orders) => HELD_FOR_HUMAN; else APPROVED. C17 alone => abstain. C02 (payload
validity) runs first in `validate.rs`. Money is `i64` nanos; products use
checked `i128`, notional and exposure round up, headroom rounds down; NaN, Inf,
negative and zero inputs from any `double` that is read are rejected.
Exposure checks are worst case: approved-but-unfilled orders count until the
execution-motor reports them terminal (or their attestation expires unreported).

Deliberate deviations (stricter than the spec): no fallback to the signal's own
regime confidence when Aegis's own regime label is missing or stale (that is a
mismatch and holds the order); the rate token is consumed on every evaluated
signal; a hold release re-runs the rate limit control.

## State

* Replay / idempotency (C07): `replay.log`, fsynced append-only JSON lines,
  loaded into memory, never evicted for capacity (retention 24 h PROPOSED). A
  corrupt log starts the service but rejects every signal. A MISSING log next
  to other state files is treated the same way (and latches HARD): dropping the
  history would let every previously seen signal id be approved again.
* Portfolio (positions, pending orders, day equity, order-size history):
  `portfolio.json`, atomic replace. Unreadable => all approvals fail closed. A
  missing snapshot next to other state files is loss or tampering, not a flat
  portfolio: start-up latches a persisted HARD. Only an entirely empty state dir
  bootstraps (in production only with `AEGIS_ALLOW_FRESH_STATE=1`); the empty
  snapshot and log are written immediately so the next start finds them.
* Atomic replaces (`kill_state.json`, `portfolio.json`, `replay.log`) fsync the
  state directory after the rename (on Unix; a no-op on Windows hosts).
* Holds are in memory (a restart loses them, which is a reject).
* Audit (`AuditSink`): `TracingSink` + fsynced `audit.wal` JSON lines. Records
  hold identifiers, enum names, thresholds and observed values only; no LLM
  text, keys or credentials. Zone C can subscribe by implementing `AuditSink`.

## Exit-criteria mapping (spec section 12)

| Criterion | Status |
|---|---|
| `aegis.proto` merged, stubs for Rust, `_nanos` fields | Rust server/client stubs generated by `build.rs`. Proto is frozen and owned by another package. |
| Every control implemented with unit tests, C-IDs in names | Done: `src/controls/tests.rs` (C01-C19), `src/validate.rs` (C02). |
| Kill-switch machine, invariants 5.4 tested, persistence verified | Done: `src/killswitch/tests.rs`, `src/engine/tests.rs`, `tests/app_startup.rs`. |
| Supervisor + liveness watchdog trips HARD in a recorded test | Done for the trip: `aegis supervisor` (see above) latches HARD as `supervisor/liveness` after >60 s of failed probes, tested on a fake clock against a real in-process Aegis. NOT done: cutting the broker egress, supervising the Supervisor, a recorded drill on real infrastructure. |
| Attestation + broker gateway; unattested/tampered order cannot reach the mock broker | Attestation, verification helper and tamper tests done. The broker gateway is not part of this crate. |
| No `f64` for money on the decision path (grep/CI check) | Holds by construction (`Nanos`). Only the legacy-double boundary and reference-data conversion use `f64`. No CI/grep check added (CI is out of scope). |
| Measured latency evidence for section 8 | Measured, see below. The 50 ms budget is NOT demonstrated for the tail with fsyncs. |
| Owner confirmed PROPOSED items | Not done (owner action). |
| Regulatory docs updated | Out of scope. |
| Kill-switch drill in paper | Not done. |

### Measured latency (release build, in the `afe-rust-dev` container on Docker Desktop, Windows 11)

* Pure control pipeline (`tests/latency_pipeline.rs`): p50 7.3 us, p99 14.6 us (20,000 runs).
* Full engine approval path, in-memory stores, Ed25519 dev signer (`tests/latency_engine.rs`): p50 113 us, p99 352 us, max 469 us (400 runs).
* Full approval path with the real file stores (3 fsyncs) (`tests/app_startup.rs`): p50 16.5 ms, max 74.6 ms (24 runs). fsync latency of this host dominates; the tail exceeds 50 ms here.
* HSM signing latency (spec budget 25 ms) was NOT measured: SoftHSM2 numbers are not representative, and no network HSM was available.

## SoftHSM2 integration test

`tests/softhsm_pkcs11.rs` (feature `pkcs11`) skips loudly unless configured.
Provisioning used in the container (Debian bookworm):

```bash
apt-get install -y softhsm2 opensc
export SOFTHSM2_CONF=/tmp/softhsm2.conf   # directories.tokendir = /tmp/tokens
softhsm2-util --init-token --free --label afe-test --pin 123456 --so-pin 654321
pkcs11-tool --module /usr/lib/softhsm/libsofthsm2.so --token-label afe-test --login --pin 123456 \
  --keypairgen --key-type EC:prime256v1 --label attest-test --id 01
printf 123456 > /tmp/pin
AEGIS_SOFTHSM_MODULE=/usr/lib/softhsm/libsofthsm2.so AEGIS_SOFTHSM_TOKEN=afe-test \
AEGIS_SOFTHSM_KEY=attest-test AEGIS_SOFTHSM_PIN_FILE=/tmp/pin \
  cargo test --features pkcs11 --test softhsm_pkcs11 -- --nocapture
```

## Not implemented / known gaps

* **The reference-data producer is not part of this crate.** Aegis now accepts
  `PushReferenceData` (see above), but nothing in the repo calls it yet: the
  sensory-array / regime-detector must push (int64 nanos, role
  `market-data-writer`). Until they do, C08 fails and every signal is rejected
  (safe). The legacy `update_snapshot` / `update_regime` helpers remain for the
  `double`-based `MarketSnapshot`.
* Reference data is in memory: a restart clears it, so C08 fails until the next
  push (safe). Pushes are not written to the audit WAL (only `tracing`).
* No broker egress cut by the Supervisor; no broker gateway; no HITL UI (spec
  components owned elsewhere). The `audit.wal` tailing `AuditSink` for Zone C is
  not implemented.
* Second approver on `ResolveHold` is self-asserted in the request (the message
  has no credential field); only `operator_id` is bound to the certificate.
  (See Known limitations below.)
* Unusual-size history (C19) is fed only by approved orders; cold start holds
  every order for a symbol until 30 orders exist.
* The dev signer key is in process memory; production needs the `pkcs11`
  feature and a real HSM module mounted into the image.
* `Cargo.lock` is committed (binary crate). `redis`, `dotenvy`, `anyhow`,
  `chrono`, `mockall`, `tokio-signal` were removed from `Cargo.toml` (unused;
  `tokio-signal` was abandoned).

## Known limitations (owner decisions left open)

These are deliberate gaps found in review, NOT fixed in this crate because each
needs an owner decision or a change outside it:

1. **Second approver on `ResolveHold` is caller-asserted.** The request has no
   credential field for the second approver, so only `operator_id` is bound to
   the mTLS identity. Closing this needs a proto change (signed approval) or a
   second credential path. TODO(owner).
2. **C11 values notional at the order's bounded limit price, sells included.**
   For a sell the limit is a floor, so the executed notional can exceed what C11
   checked. Whether sells should be valued at a worst-case (higher) price, and
   by how much, is an owner risk decision. TODO(owner).
3. **No plausibility check on pushed reference data.** `PushReferenceData`
   validates shape (positive nanos, timestamp) and the producer's role, not
   that a price is sensible versus the previous value; a wrong but well-formed
   price moves the C09 collar and the exposure marks. Any bound (max jump per
   push, cross-check against a second source) is an owner decision. TODO(owner).
4. **Reservations are released only at attestation expiry (or on an execution
   report).** There is no explicit cancel path: an approved order that is never
   sent holds its exposure until `attestation_ttl_ms` elapses. The one case
   released early is a request abandoned by its caller before the reservation is
   returned; a caller that leaves in the microseconds after the release check
   still leaves a reservation until expiry.
5. **`AEGIS_ALLOW_FRESH_STATE=1` is a standing bypass if left set.** With it
   set, wiping the whole state dir re-bootstraps without HARD. Remove it after
   the first start; the compose files under `agent-core/infrastructure` are
   outside this crate and do not set it (a production first start needs it once;
   the dev compose needs an explicit loopback `AEGIS_LISTEN_ADDR` and therefore
   a different way to publish the port).
