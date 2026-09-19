# Phase 3 Spec — Aegis (Pre-Trade Controls, Kill Switches, Signing) + Execution Motor

_Zone B (Rust: `aegis`; Python: `execution-motor`) | Aegis latency budget: < 50 ms | Tracks: ALI-42_

> **Status: DRAFT specification. Nothing in this document is implemented.**
> `zone-b/aegis/src/main.rs` is a 3-line placeholder that prints a message. `zone-b/execution-motor/` contains only `__init__.py` and `requirements.txt`. The protos in `shared/proto/` define messages only (no `service`/`rpc`).
>
> Label conventions used below:
> - **[EXISTING]** value already stated in another repo document or config (cited).
> - **PROPOSED** value or design choice introduced by this spec. It is a draft for the owner to decide, not a decision. Rationale is given next to each.
> - **TODO(owner)** a real-world fact, number or decision that cannot be derived from the repo.
> - Statements about regulation are labelled **requires qualified legal review**. This spec does not assert that any control satisfies any regulation.

---

## 1. Scope and goals

Build the only component permitted to authorise an order (Aegis) and the minimum execution path that consumes its authorisation.

In scope:
1. Deterministic pre-trade controls (PTCs), hard and soft (§4).
2. A single kill-switch hierarchy state machine (§5).
3. gRPC contract for Zone A -> Aegis, operator/HITL -> Aegis, and Execution Motor <-> Aegis (§6, Appendix A).
4. Signed attestation of approved decisions and the HSM/broker-authentication design (§7, ADR-004).
5. Latency budget, failure semantics, audit emission, test plan, exit criteria (§8-§13).
6. Minimum Execution Motor scope, paper trading only (§10).

Out of scope (see other specs): the backtest that calibrates thresholds (`phase_4_backtesting_compliance.md`), the audit logger / manifest pipeline / HITL UI internals (`phase_4_backtesting_compliance.md`), production key ceremony, IaC and canary (`phase_5_canary_production.md`).

Non-goals: Aegis does not generate signals, does not call LLMs, does not hold market-data credentials, and contains no ML.

## 2. Architecture and trust boundaries

```
Zone A (untrusted for authorisation)        Zone B                                        Zone C
cognitive-core --SubmitSignal(TradeSignal)--> AEGIS --AegisDecision(+attestation)--> execution-motor
   (no broker creds, no signing)               |  \                                        |
regime-detector -> Redis regime:labels ------->|   +-- audit records (WAL, then ship) ---> audit-logger (Zone C)
sensory-array  -> Redis sensory:snapshots ---->|                                           v
HITL interface (Zone C) --ResolveHold / Kill RPCs--> AEGIS                          [broker gateway] --> broker
Supervisor (independent process) --liveness probe--> AEGIS ; enacts Hard Switch (network egress cut)
```

Principles:
- **Aegis trusts nothing from Zone A except the *request*.** Every number that a control compares against (reference price, ADV, positions, NAV, regime) is taken from Aegis-owned or Zone-B-owned state, never from fields the LLM pipeline can influence. The signal's own `regime`/`regime_confidence` are cross-checked against Aegis's own regime subscription (PROPOSED; see §4.4).
- **No attestation, no order.** The execution path refuses any order that does not carry a valid, unexpired, single-use Aegis attestation that matches the order exactly (§7).
- **Fail closed everywhere** (§9).
- **PROPOSED new components** (neither exists today; both are needed for the kill-switch design to be real):
  - *Supervisor*: a minimal process, deployed separately from Aegis, that probes Aegis liveness and can enact the Hard Switch. Language/placement: TODO(owner).
  - *Broker gateway*: the only holder of broker credentials, see §7 and ADR-004. May be a module inside Aegis or a separate container; decision in ADR-004.

## 3. Money and numeric handling

Rule: **no floating point (`f64`/`double`) for money, prices or share quantities anywhere on the Aegis decision path.**

Current state: `trade_signal.proto` and `order_request.proto` use `double` for `quantity`, `price_limit`, `limit_price`, `stop_price`, and all `estimated_*` cost fields. `Cargo.toml` for Aegis has no decimal crate.

PROPOSED representation:
- **Fixed-point integers, scale 1e-9 ("nanos")**, transported as `int64` in fields suffixed `_nanos`. Rationale: exact, cheap, no allocation; sub-penny prices (up to 4 decimals for sub-dollar US equities) and fractional shares fit; simple to compare. The `int64` range at 1e-9 is about +/- 9.22e9 units, ample for per-order and per-symbol values; portfolio-level aggregates (gross exposure, NAV) are held in `i128` inside Aegis.
- Arithmetic: all products (price x quantity) computed in `i128` with **checked** operations; overflow => REJECT with `REASON_INTERNAL_ERROR`. Percentage limits are expressed in basis points (`u32`) or integer ratios, never `f64`.
- **Rounding direction is conservative**: notional and exposure round *up*; remaining headroom rounds *down*. Exact formulas and their boundary tests are part of the §11 test plan (exactly-at-limit passes, one nano beyond fails).
- Python side (`execution-motor`, `cognitive-core`): use `decimal.Decimal` at the boundary; never `float` for money. Conversion helpers live in one shared module (location TODO(owner)).
- Non-money statistical values (`omega`, `p_success`, `regime_confidence`, z-scores, volatility) may stay `double`, but Aegis must reject NaN/Inf/out-of-range for every double it reads.
- **Migration**: because the wire type changes (fixed64 -> varint), do not change field types in place. Add new `_nanos` fields with new field numbers, mark the old `double` fields `[deprecated = true]`, and have Aegis *ignore* the deprecated doubles once producers are migrated. Until Zone A produces `_nanos`, Aegis may convert an incoming `double` only at the boundary: reject NaN/Inf/<= 0, round half-up to the nearest nano, and count a `LEGACY_DOUBLE_ACCEPTED` metric. Snippets: Appendix A.2.
- Cross-doc note: `docs/regulatory/ptc-calibration.md` computes the order-size limit from "20-day ADV" while `MarketSnapshot` and `phase_1_sensory_array.md` provide `adv_30d`. One of them must change; this spec assumes `adv_30d` (the field that exists) until the owner decides. See §14.

## 4. Pre-trade controls

### 4.1 Control list

`Kind`: **H** = hard block (reject; cannot be released by a human), **S** = soft block (hold for human unless stated). Numbers marked **[EXISTING]** are stated in `docs/regulatory/*` or `docker-compose.yml`; all such numbers are **UNSUBSTANTIATED** (see the banner in `docs/regulatory/ptc-calibration.md`) and are provisional defaults until Phase 4 calibration produces real numbers from real data.

| ID | Control | Kind | Rule | Threshold / source | Reason code |
|---|---|---|---|---|---|
| C01 | Kill-switch gate | H | Effective kill level > NORMAL blocks per §5.2 | n/a | `REASON_KILL_SWITCH_ACTIVE` |
| C02 | Payload validity | H | Required fields present, enums known, doubles finite, quantity > 0, side != UNKNOWN, `signal_id` is a valid UUID | n/a | `REASON_INVALID_SIGNAL` |
| C03 | Symbol allowlist | H | Symbol must be in the configured strategy universe | universe list TODO(owner) | `REASON_SYMBOL_NOT_ALLOWED` |
| C04 | Short-sale gate | H | `SELL_SHORT` rejected unless explicitly enabled | PROPOSED default: disabled. Enabling shorts carries broker/regulatory requirements: requires qualified legal review, TODO(owner) | `REASON_SHORT_SELLING_DISABLED` |
| C05 | Trading-session gate | H | Reject outside the configured session | session/calendar source TODO(owner) | `REASON_OUTSIDE_TRADING_SESSION` |
| C06 | Signal expiry | H | `now_ns > valid_until_ns` => reject. `valid_until_ns == 0` (unset) => reject. `created_at_ns > now_ns + clock_skew` => reject. `now_ns - created_at_ns > max_signal_age` => reject | `clock_skew` PROPOSED 250 ms; `max_signal_age` PROPOSED 5 s (rationale: the debate budget is 2.3-2.9 s, see §8/§14, plus margin) | `REASON_SIGNAL_EXPIRED` / `REASON_SIGNAL_FROM_FUTURE` |
| C07 | Duplicate / replay | H | See §4.3 | retention >= `max_signal_age` + skew, PROPOSED 24 h | `REASON_DUPLICATE_SIGNAL`, `REASON_REPLAY_PAYLOAD_MISMATCH` |
| C08 | Reference-price freshness | H | Aegis's own last snapshot for the symbol must have `is_stale == false` and be younger than `max_ref_age`; else reject | `max_ref_age` PROPOSED 1 s. (The sensory-array TTLs [EXISTING] L2 1 ms / trade print 5 ms are per-message freshness and are a different thing) | `REASON_STALE_REFERENCE_PRICE` |
| C09 | Price collar | H | Order limit price within +/- collar of Aegis-side mid. Market orders: see §4.5 | [EXISTING] 1.5% (`PRICE_COLLAR_PCT=1.5` in compose; `ptc-calibration.md`). UNSUBSTANTIATED | `REASON_PRICE_COLLAR` |
| C10 | Max order quantity | H | `quantity <= max_order_qty` | value TODO(owner) | `REASON_MAX_ORDER_QUANTITY` |
| C11 | Max order notional | H | `price x quantity <= max_order_notional` (uses the collar-bounded price) | value TODO(owner) | `REASON_MAX_ORDER_NOTIONAL` |
| C12 | Order size vs ADV | H | `quantity <= 0.5% x adv_30d` | [EXISTING] 0.5% ADV (`ptc-calibration.md`). UNSUBSTANTIATED. Doc says 20-day ADV, snapshot provides 30-day: see §3/§14 | `REASON_ORDER_SIZE_ADV` |
| C13 | Per-symbol position limit | H | `abs(position_after) <= max_position_symbol` | value TODO(owner) | `REASON_POSITION_LIMIT_SYMBOL` |
| C14 | Gross exposure limit | H | `sum(abs(position x mark))` after the order `<= max_gross` | value TODO(owner) | `REASON_GROSS_EXPOSURE_LIMIT` |
| C15 | Daily drawdown | H | Block new risk when realised + unrealised loss >= limit x starting daily NAV. Also latches the Logic Switch (§5) | [EXISTING] 2.0% (`MAX_DAILY_DRAWDOWN_PCT=2.0`). UNSUBSTANTIATED | `REASON_DAILY_DRAWDOWN` |
| C16 | Order/message rate | H | Token bucket per symbol and global; excess rejected | rates TODO(owner); must be justified in Phase 4 | `REASON_RATE_LIMIT` |
| C17 | Low-confidence gate | H (abstain) | `omega < omega_min` => reject as abstain, no human hold | [EXISTING] 0.55 (`LOW_CONFIDENCE_THRESHOLD`, `OMEGA_THRESHOLD`, RTS6 template "Automatic abstain (no HITL required)"). UNSUBSTANTIATED | `REASON_LOW_OMEGA` |
| C18 | Regime gate | S | See §4.4 | provisional; conflicting values in existing docs | `REASON_REGIME_LOW_CONFIDENCE`, `REASON_REGIME_MISMATCH` |
| C19 | Unusual order size | S | Order size in the top 5% of the trailing 30-day order-size distribution for the symbol => hold | [EXISTING] P95 / 60 s response window (RTS6 template). Cold start (fewer than N historical orders, N PROPOSED 30): hold every order for that symbol | `REASON_UNUSUAL_ORDER_SIZE` |

Notes:
- C17 also exists in Zone A (`SIGNAL_ABSTAIN`). Aegis re-checks it because Aegis must not depend on Zone A behaving.
- The three env-configured limits in compose (`MAX_DAILY_DRAWDOWN_PCT`, `PRICE_COLLAR_PCT`, `LOW_CONFIDENCE_THRESHOLD`) carry defaults there. PROPOSED: Aegis **refuses to start** if any limit in this table lacks an explicit value, and exposes the SHA-256 of the active limits config via `GetAegisState` so the manifest can record what was in force. (Changing Aegis hard-block thresholds is out of scope for SHARP and requires PTC re-calibration, per `docs/processes/sharp-promotion.md`.)

### 4.2 Evaluation order and decision precedence

1. C01 (kill state) and C02-C07 (validity) first; on failure return immediately (nothing meaningful remains to evaluate).
2. Then evaluate **all** of C08-C19 even if one fails, so the audit record lists every violated control (the budget permits this, see §8).
3. Precedence: any H failure => `REJECTED`; else any S failure => `HELD_FOR_HUMAN`; else `APPROVED`. C17 maps to `REJECTED` (abstain), not a hold.
4. Mapping to the existing `SignalStatus` enum: APPROVED -> `SIGNAL_APPROVED`; REJECTED by hard block/kill/invalid -> `SIGNAL_REJECTED_HARD_BLOCK`; REJECTED by C17 -> `SIGNAL_ABSTAIN`; C06 -> `SIGNAL_EXPIRED`; HELD -> `SIGNAL_SOFT_BLOCK_PENDING`.
5. Every decision, including rejections, is appended to the durable audit WAL before the RPC returns (§9).

### 4.3 Duplicate and replay protection (C07)

- Aegis keeps a **durable** set of seen `signal_id` values with a SHA-256 of the canonical request payload and the decision returned.
- Same `signal_id`, same payload hash: return the **original decision** (idempotent); never re-approve, never issue a second attestation. Same `signal_id`, different payload: `REASON_REPLAY_PAYLOAD_MISMATCH`, and raise a security event.
- Storage: NOT the shared Redis as configured. `docker-compose.yml` runs Redis with `--maxmemory-policy allkeys-lru`, which can silently evict replay records and defeat this control. PROPOSED: an Aegis-local durable store (e.g. embedded KV/SQLite on the Aegis volume) or a dedicated Redis with `noeviction` and AOF. Decision TODO(owner).
- Retention >= `max_signal_age` + skew (PROPOSED 24 h; longer-term audit records live in Zone C).
- Attestations are single-use too (nonce = `signal_id`), enforced by the broker gateway (§7).

### 4.4 Regime gate (C18): resolving the existing contradiction

Existing documents disagree:
- RTS6 template §2.2: soft block when *HMM confidence < 0.6 OR strategy-trained regime != current regime*.
- `ptc-calibration.md`: soft block when *trained regime != current regime AND HMM confidence > 0.70*.

These are opposite in both boolean structure and confidence direction (the second deliberately ignores low-confidence regimes; the first flags them). Neither has calibration evidence.

PROPOSED single definition (fail-safe direction; numbers provisional pending Phase 4):
- Let `R_s` = the set of regimes the strategy is validated for (from the strategy spec; TODO(owner): no strategy specification exists in the repo). Let `c` = HMM posterior confidence from Aegis's own regime subscription (fallback: the signal field, flagged in the log).
- Fire `REASON_REGIME_LOW_CONFIDENCE` if `c < C_MIN`; fire `REASON_REGIME_MISMATCH` if `regime not in R_s`. Either => HELD_FOR_HUMAN. `C_MIN` PROPOSED provisional 0.60 (the RTS6 template value; it holds more orders, i.e. errs safe).
- `REGIME_UNKNOWN`, or a signal regime that differs from Aegis's own latest regime label, is treated as mismatch.
- Phase 4 calibration (`phase_4_backtesting_compliance.md` §6) must set `C_MIN` from data. Both `ptc-calibration.md` and the RTS6 template must then be updated to the final definition. Until then both documents carry a contradiction note.

### 4.5 Market orders

`OrderRequest.order_type` allows MARKET, which has no price to collar. PROPOSED: Aegis never authorises an unbounded market order; it converts it to a marketable limit at `mid +/- collar` (buy: mid + collar; sell: mid - collar), or rejects if no fresh reference price exists. The converted price is what is attested. Owner decision: TODO(owner).

### 4.6 Held signals (HELD_FOR_HUMAN)

- Aegis stores the held signal under a `hold_id`; `hold_expires_at_ns = min(valid_until_ns, now + hold_window)`. `hold_window` PROPOSED: 60 s for C19 [EXISTING, RTS6 template]; for C18 the operator-decision window is bounded by `valid_until_ns`.
- On expiry with no decision: **reject** (fail closed).
- `ResolveHold` re-runs *all hard controls* at release time against current state; a human can release a soft block but can never override a hard block. The released order is attested like any other.
- Operator identity and (PROPOSED) second-approver requirement for release are recorded in `HITLOverrideRecord` (existing proto). Whether a second approver or a cooling period is mandatory: TODO(owner). Authentication of operators to Aegis is unspecified today: TODO(owner) (PROPOSED options: mTLS client certificates per operator identity, or signed tokens issued by the HITL service).

## 5. Kill-switch hierarchy

### 5.1 Model

One state machine, five levels, ordered by severity. Existing documents name five switches (RTS6 template §3; README "heartbeat"; `dora-ict-risk-management-framework.md` §3.1; compose `KL_*` and `HEARTBEAT_INTERVAL_SECONDS`) but define no state machine and give inconsistent timings.

```
NORMAL(0) < SOFT(1) < LOGIC(2) < DEAD_MANS(3) < HARD(4) < PHYSICAL(5)
```

- Each trigger is an independent **latch** with its own `trigger_id`. **Effective level = max level over all active latches.** Resetting one latch does not clear the others.
- Latches are **sticky**: they survive Aegis restarts (persisted durably before the trip is acknowledged). A restart never lowers the level. If the state store cannot be read at start-up, Aegis starts at **HARD** (fail closed).
- Automatic transitions only go **up**. Going down requires an authenticated reset by the authority in the table.
- Every transition writes an audit record (who/what/when/why, previous and new effective level, monotonically increasing `state_seq`).

### 5.2 Levels

Trigger wording and the reset-authority column are **[EXISTING]** from the RTS6 template §3 unless marked otherwise; effect definitions and all timings are **PROPOSED**.

| Level | Trigger | Effect (cumulative with lower levels) | Reset authority | Timing (PROPOSED unless marked) |
|---|---|---|---|---|
| **1 SOFT** | Operator command; "L3 Ambiguity" [EXISTING wording; "L3" is undefined in the repo: TODO(owner)]; KL divergence >= 0.3 [EXISTING `KL_SOFT_SWITCH_THRESHOLD`, uncalibrated] | Aegis rejects new *risk-increasing* signals (`REASON_KILL_SWITCH_ACTIVE`). Risk-reducing orders (strictly reduce absolute position in the symbol) are still evaluated normally. Open orders untouched | Single operator authorisation [EXISTING] | Effective for the next `SubmitSignal` within 1 ms of the latch (in-process atomic read at C01) |
| **2 LOGIC** | Daily drawdown >= 2.0% [EXISTING]; KL divergence >= 0.5 [EXISTING `KL_LOGIC_SWITCH_THRESHOLD`, uncalibrated]; automated cascade from SOFT | Reject all new signals except human-released risk-reducing orders. **Cancel all open orders** (execution-motor reacts to the state stream). No automatic flattening | Dual operator + root-cause sign-off [EXISTING] | Latch within 1 ms; cancel-all issued within 1 s of latch. Drawdown evaluated on every equity update and at least every 1 s while orders are open |
| **3 DEAD MAN'S** | **Operator heartbeat** missed (§5.3) | As LOGIC. Additionally the system assumes no human is available: no human-released orders until a positive heartbeat. Whether to auto-flatten positions is an open owner decision (TODO(owner)); PROPOSED default: do NOT auto-flatten (no flatten procedure is designed; an unattended forced exit has its own market risk) | Positive heartbeat + check-in [EXISTING] | Heartbeat interval **4 h** [EXISTING: RTS6 template ">4h"; compose `HEARTBEAT_INTERVAL_SECONDS=14400`]. Warning to operator at 3 h 30 min (PROPOSED); trip at 4 h |
| **4 HARD** | **Aegis liveness watchdog** unresponsive > 60 s [EXISTING figure, from DORA doc §3.1, re-attributed to its correct mechanism, see §5.3]; Supervisor detects unrecoverable HSM/state failure; manual "infrastructure firewall drop" [EXISTING wording] | Aegis stops signing (HSM session closed / key disabled). Supervisor cuts network egress to the broker. All Aegis RPCs except state query and reset return `KILLED`. Open broker orders can NOT be cancelled through the system once egress is cut: manual cancellation at the broker is required (see runbook `kill-switch-drill.md`; procedure TODO(owner)) | Dual operator + compliance sign-off [EXISTING] | Probe every 5 s; trip after 60 s of failed probes [EXISTING 60 s]; egress cut within 5 s of trip |
| **5 PHYSICAL** | BusKill USB tether disconnected [EXISTING wording] | As HARD. Also the operator workstation is locked by the BusKill action | Full incident report + DORA notification [EXISTING] | Immediate. What the BusKill action executes is unspecified in existing docs. PROPOSED: the workstation script issues an authenticated `TriggerKillSwitch(PHYSICAL)` to Aegis before locking; if that call cannot be made, the heartbeat lapse (L3) is the backstop. Note this protects against operator/workstation separation, not against a remote attacker. TODO(owner): confirm intended behaviour |

### 5.3 The 60 s vs 4 h conflict: both figures are correct, for different mechanisms

Existing documents attach both figures to the "Dead Man's Switch":
- RTS6 template: Dead Man's Switch trigger = "Operator heartbeat missed > 4h". Compose: `HEARTBEAT_INTERVAL_SECONDS=14400` on the Aegis service.
- DORA doc §3.1: "Zone B Aegis crash -> Dead Man's Switch activates; Hard Switch if unresponsive > 60s".

PROPOSED resolution: they are two different heartbeats.
1. **Operator heartbeat (4 h)** proves a *human* is present. Its loss trips **Dead Man's (L3)**. Direction: operator -> Aegis (`Heartbeat` RPC).
2. **Component liveness watchdog (60 s)** proves *Aegis is alive*. Its loss trips **Hard (L4)**. Direction: Supervisor -> Aegis probe. It must be an independent process; otherwise a hung Aegis cannot report itself dead.

A crashed Aegis already blocks new orders (no attestation is ever produced), so no Dead Man trip is needed for that scenario; the watchdog exists to guarantee the broker link is then cut.

Documents that must change to match (after the owner decides):
- `docs/regulatory/dora-ict-risk-management-framework.md` §3.1 row "Zone B Aegis crash": replace "Dead Man's Switch activates" with "liveness watchdog trips Hard Switch after 60 s". A PROPOSED reconciliation note has been added to that row.
- `docs/regulatory/mifid-ii-rts6-self-assessment-template.md` §3: the Dead Man's row should say "operator heartbeat", and a row/note for the liveness watchdog should be added. A note has been added.
- Compose: `HEARTBEAT_INTERVAL_SECONDS` on `aegis` is the operator heartbeat; rename to `OPERATOR_HEARTBEAT_INTERVAL_SECONDS` and add `LIVENESS_TRIP_SECONDS` (compose owner; not changed by this docs package).
- Whether a 4 h operator heartbeat is adequate for a 6.5-hour trading session is a risk decision: TODO(owner).

### 5.4 State-machine invariants (test targets)

1. Effective level is a pure function `max(active latches)`.
2. No automatic transition reduces a latch.
3. A restart never lowers the level; unreadable state => HARD.
4. A reset without the required number of distinct authorised identities is refused and audited.
5. While effective level >= LOGIC, `SubmitSignal` never returns `APPROVED` for a risk-increasing order. While effective level >= DEAD_MANS it returns `APPROVED` for no order at all; only state queries, heartbeats and resets work.
6. At HARD/PHYSICAL no attestation can be produced (signing disabled).

## 6. gRPC API

The Aegis service is defined in Appendix A.1, ready to copy into `agent-core/shared/proto/aegis.proto`. The existing `generate.sh` compiles every `*.proto` in the directory, so no script change is needed for Python stubs. `zone-b/aegis` needs a `build.rs` with `build_server(true)` (only `sensory-array` has a `build.rs` today, client-only) and `protoc` available.

| RPC | Caller | Purpose |
|---|---|---|
| `SubmitSignal(TradeSignal) returns (AegisDecision)` | cognitive-core | Evaluate a signal. Deadline set by caller; Aegis budget < 50 ms (§8). Caller treats deadline-exceeded/unavailable as REJECT |
| `ResolveHold(ResolveHoldRequest) returns (AegisDecision)` | HITL interface | Human decision on a held signal; hard controls re-run |
| `TriggerKillSwitch(TriggerKillSwitchRequest) returns (KillSwitchState)` | operator; automated monitors (audit-logger KL monitor, drawdown); workstation script | Latch a trigger |
| `ResetKillSwitch(ResetKillSwitchRequest) returns (ResetKillSwitchResponse)` | operator(s) | Clear one latch given the required approvals |
| `Heartbeat(HeartbeatRequest) returns (KillSwitchState)` | operator | Operator heartbeat (§5.3) |
| `GetKillSwitchState(Empty)`, `WatchKillSwitchState(Empty) returns (stream KillSwitchState)` | anyone / execution-motor | Poll or stream state; execution-motor must cancel on L2+ |
| `GetAegisState(Empty) returns (AegisState)` | operator, monitoring | Limits-config hash, HSM/audit health, build version |
| `ReportExecution(ExecutionReport) returns (Ack)` | execution-motor | Fills and account equity, the source for positions, drawdown and NAV (PROPOSED addition; without it C13-C15 have no data) |

Transport: PROPOSED mTLS between zones (TODO(owner): certificate issuance). Compose currently publishes `50051:50051` for `aegis` to the host; the port should only be reachable on `zone-ab-grpc`. PROPOSED: remove the host port publish (compose owner).

## 7. HSM signing and broker authentication

Problem (detailed in `docs/adr/ADR-004-hsm-signing-vs-broker-auth.md`): ADR-001 says the HSM signs orders (2-of-3 MPC threshold ECDSA via PKCS#11). The working broker choice, Alpaca, authenticates with an API key and secret (compose: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`), i.e. a bearer credential, not a per-request signature. An HSM cannot produce a bearer credential's authentication. So what the HSM signs, and how "only Aegis can authorise an order" is enforced, is unresolved.

Options (full analysis in ADR-004):
- **A. Attestation only**: the HSM signs an Aegis attestation; execution-motor verifies it and holds broker credentials. A compromised execution-motor can bypass Aegis using the broker credential.
- **B. Aegis holds broker credentials and places orders itself.** Pulls SOR/algorithm logic into Aegis: bad for Aegis simplicity and latency.
- **C. Attestation + broker gateway (PROPOSED recommendation).** The HSM signs an attestation over canonical order bytes. A small *broker gateway* is the only component holding broker credentials; it verifies signature, expiry, single-use and exact match to the order, then adds broker auth and forwards. execution-motor holds no broker credentials.
- **D. A different broker/auth model.** The broker decision is open (TODO(owner): the DORA doc says the production broker is "TBD").

Attestation payload (PROPOSED canonical form v1). Do not sign "protobuf bytes" (serialisation is not canonical across implementations). Sign SHA-256 of this exact UTF-8 text, one `key=value` per line, fixed order, `\n`-terminated, integers in decimal:

```
afe-attest-v1
signal_id=<uuid>
symbol=<SYM>
side=<BUY|SELL|SELL_SHORT>
order_type=<LIMIT|...>
qty_nanos=<int>
limit_price_nanos=<int>
stop_price_nanos=<int>
decided_at_ns=<int>
expires_at_ns=<int>
aegis_state_seq=<int>
limits_config_sha256=<hex>
key_id=<string>
```

- `expires_at_ns` PROPOSED = `decided_at_ns` + 5 s: an attestation is a short-lived, single-use authorisation, not a standing key.
- Mechanism: ECDSA P-256 with SHA-256 (PROPOSED) via PKCS#11 (`cryptoki` is already in `Cargo.toml`). TODO(owner): confirm the mechanism is supported by the chosen production HSM.
- ADR-001's "2-of-3 MPC threshold ECDSA" is not a capability that a plain PKCS#11 HSM library is described as providing, and no design for it exists in the repo. Treat it as unverified until the owner supplies a design or amends ADR-001 (a status note has been appended there). ADR-001 also places a signing shard in the Zone C audit store while README/compose describe Zone C as air-gapped from signing keys (§14).

## 8. Latency budget (Aegis: < 50 ms)

[EXISTING] README: "Aegis PTC validation < 50 ms" (within a 2,600 ms end-to-end budget). PROPOSED allocation (targets; none has been measured):

| Step | p99 target |
|---|---|
| gRPC receive + decode + validity (C02-C07) | 2 ms |
| Kill-state read + in-memory state reads (positions, limits, latest snapshot from in-process cache) | 2 ms |
| Controls C08-C19 (integer arithmetic) | 3 ms |
| Attestation canonicalisation + SHA-256 | 1 ms |
| HSM sign | <= 25 ms (**UNMEASURED**; SoftHSM2 numbers are not representative of a network HSM: TODO(owner) measure production HSM round-trip before relying on this) |
| Durable audit WAL append (fsync policy TBD) | <= 8 ms |
| Response encode/send | 1 ms |
| **Total** | **< 50 ms** (about 8 ms headroom) |

Rules: no synchronous Redis/QuestDB round trip in the hot path (state comes from in-process caches fed by subscriptions; C08 fails closed if the cache is stale). If measurement shows the sum exceeds 50 ms, change the budget or the HSM approach; do not silently relax checks.

The wider README figures are inconsistent and unvalidated (§14): "Execution Motor -> Exchange ACK < 10 ms" has no supporting evidence for the working broker, and Blue+Red = 1,500 ms in README vs 800 + 800 = 1,600 ms sequential in ADR-002/`graph.py`.

## 9. Failure semantics: fail closed

Rule: **on any error, uncertainty, timeout or missing data, Aegis does not approve.** The absence of an attestation is the safe default everywhere.

| Condition | Behaviour |
|---|---|
| Panic / internal error / arithmetic overflow in any control | REJECT `REASON_INTERNAL_ERROR`; process restarted by supervision; never approve on error |
| Unknown enum value, NaN/Inf, missing required field | REJECT `REASON_INVALID_SIGNAL` |
| Reference price / ADV / position / NAV data missing or stale | REJECT (`REASON_STALE_REFERENCE_PRICE` / `REASON_STATE_UNAVAILABLE`) |
| Redis (regime/snapshots) unavailable | Caches age out -> C08/C18 fail -> REJECT / HOLD |
| HSM unavailable, session lost, PIN locked | No attestation possible -> REJECT `REASON_HSM_UNAVAILABLE`; after 60 s of continuous failure (PROPOSED) the Supervisor trips HARD |
| Kill/replay state store unreadable | Start at HARD; replay-protection failure => REJECT |
| Audit WAL cannot be written | REJECT `REASON_AUDIT_UNAVAILABLE` (no un-audited decision) |
| Shipping the WAL to Zone C is delayed | Continue, alert, bounded backlog (PROPOSED: 1,000 records or 60 s) then latch SOFT |
| Clock skew / non-monotonic clock | C06 rejects; alert. Time-source policy (NTP/PTP, monotonic vs wall) TODO(owner) |
| Limit config missing/invalid at start | Refuse to start |
| Client deadline exceeded / connection lost | The client (execution-motor) treats it as REJECT; a late approval is discarded because its attestation expires |
| Aegis process dead | No attestation => no orders. Supervisor trips HARD after 60 s |

## 10. Execution Motor (minimum scope)

Status: `zone-b/execution-motor/` is an empty package. README/CLAUDE.md previously referenced `sor.py`, which does not exist.

Phase 3 minimum, **paper trading only** (the compose default `ALPACA_BASE_URL` is the paper endpoint):
1. Accept only `AegisDecision(APPROVED)` with a valid, unexpired attestation; refuse otherwise.
2. Submit the order through the broker gateway (§7) using `order_id` as an idempotent client order id (broker support for client order ids: TODO(owner) confirm).
3. React to `WatchKillSwitchState`: on L2+ cancel all open orders within 1 s (PROPOSED).
4. Report fills and account equity to Aegis (`ReportExecution`), reconcile positions with the broker at start-up and periodically (interval PROPOSED 60 s), and halt (latch SOFT) on unexplained divergence.
5. Smart order routing, venue toxicity, POV and Iceberg (`ExecAlgo`) are **deferred**: with a single broker, the available routing choices depend on that broker's API (TODO(owner)). Phase 3 supports DIRECT limit orders only.
6. Decimal handling per §3; no floats for money.

## 11. Test plan

Rust (`cargo test`; property-testing crate TODO(owner)):
- Per control: below/at/above threshold, one-nano boundary, negative/zero/NaN/overflow inputs.
- **Property**: for arbitrary inputs, `APPROVED` implies every H control passed and no latch blocks; approval never occurs when any control errored.
- Kill-switch state machine: latch/max semantics, monotonic escalation, reset authority per level, persistence across restart, unreadable state => HARD.
- Replay: idempotent decision, payload mismatch, retention expiry; eviction cannot occur (durable-store test).
- Attestation: canonical-form golden vectors (cross-language, verified by a Python verifier in the gateway tests), expiry, single use, tamper detection (any field changed after signing is rejected).
- Regime gate truth table (both legacy definitions listed alongside the chosen one).
- Fuzz the gRPC decode path.

Integration:
- cognitive-core (mock) -> Aegis -> execution-motor -> mock broker end to end; orders without an attestation never reach the mock broker.
- Chaos: kill Redis, remove the SoftHSM token, corrupt the state store, stall Aegis (Supervisor trips HARD within 60 s plus margin), clock jump.
- Latency: p50/p99 measured against §8 with SoftHSM2 and with an injected HSM delay; recorded in the phase report. **Do not claim < 50 ms without measured evidence.**
- Drills using the DRAFT runbooks in `docs/runbooks/` (kill-switch drill in the paper environment).

## 12. Exit criteria (Phase 3 done when all are true)

- [ ] `aegis.proto` merged; stubs generated for Rust and Python; `_nanos` fields present in TradeSignal/OrderRequest.
- [ ] Every control in §4.1 implemented with unit tests; C-IDs referenced in test names.
- [ ] Kill-switch state machine implemented with the invariants in §5.4 tested; persistence verified.
- [ ] Supervisor + liveness watchdog exist and trip HARD in a recorded test.
- [ ] Attestation + broker gateway implemented; a test shows an unattested or tampered order cannot reach the (mock) broker.
- [ ] Decision path uses no `f64` for money (grep/CI check in place).
- [ ] Measured latency evidence for §8 recorded in the repo.
- [ ] Owner has confirmed the PROPOSED items listed in §13.
- [ ] Regulatory docs updated to the final kill-switch and regime definitions; their "Claims vs implementation" tables changed from "not implemented" to "implemented" only with linked test evidence.
- [ ] One kill-switch drill executed in the paper environment with evidence recorded per `docs/runbooks/kill-switch-drill.md`.

## 13. Decisions the owner must confirm (all currently PROPOSED)

1. Fixed-point nanos representation and the migration approach (§3).
2. Kill-switch model: latches + max level; two distinct heartbeats (§5).
3. All timing values in §5.2 and §8; regime `C_MIN` and the gate definition (§4.4).
4. Broker credential architecture: gateway option C (§7 / ADR-004).
5. Market orders converted to bounded marketable limits (§4.5); shorting disabled (C04).
6. Replay-state storage location (§4.3).
7. Whether Dead Man's auto-flattens (§5.2) and what BusKill triggers.
8. Missing real-world values: universe, max order quantity/notional, position/gross limits, rate limits, trading calendar, operator authentication method.

## 14. Cross-document conflicts (identified; not resolved by this spec)

| Conflict | Where | Note |
|---|---|---|
| Dead Man 4 h vs 60 s | RTS6 template vs DORA doc | Reconciled as two mechanisms in §5.3; documents flagged |
| Regime gate `<0.6 OR mismatch` vs `mismatch AND >0.70` | RTS6 template vs ptc-calibration.md | §4.4 |
| 20-day ADV vs `adv_30d` | ptc-calibration.md vs MarketSnapshot / phase 1 spec | §3 |
| Blue+Red 1,500 ms vs 800+800 ms sequential; ADR-002 budgets sum to 2,300 ms, plus Aegis 50 ms, ack 10 ms and a 500 ms handoff, exceeds the 2,600 ms README total | README vs ADR-002 / `graph.py` | README carries a note |
| ADR-001 signing shard in Zone C vs "Zone C air-gapped from signing keys" | ADR-001 vs compose comment/README | Open |
| HSM signs orders vs broker uses API key + secret | ADR-001 vs compose | ADR-004 |
| Zone A holds zero keys vs compose injects LLM keys | README/ADR-001/ADR-002 vs compose / `llm_clients.py` | ADR-003 |
| Redis `allkeys-lru` vs durable replay/state needs | compose vs §4.3 | §4.3 |
| Aegis publishes `50051` to the host | compose | §6 |
| "One-way" `zone-bc-audit` network vs a shared network containing postgres-audit, aegis and execution-motor | compose vs README/DORA doc "Zone A/B cannot write to Zone C" | Open; noted in claims tables |

---

## Appendix A — Proto snippets (copy verbatim)

Notes for implementers: existing files use `package afe.shared;`. Enum value names are scoped to the package in protobuf, so all new enum values are prefixed to avoid collisions with existing values such as `BUY`, `HARD_BLOCK`, `ORDER_REJECTED`. The spec's `APPROVED / REJECTED / HELD_FOR_HUMAN` are the `DECISION_*` values below.

### A.1 New file `agent-core/shared/proto/aegis.proto`

```proto
syntax = "proto3";

package afe.shared;

import "trade_signal.proto";
import "order_request.proto";

// Money/price/quantity fields are fixed-point int64 in units of 1e-9 ("nanos").
// Never use float/double for money. See docs/specs/phase_3_aegis_execution.md section 3.

service Aegis {
  // Evaluate a signal against all pre-trade controls. Fail closed on any error.
  rpc SubmitSignal(TradeSignal) returns (AegisDecision);
  // Human decision on a held signal. All hard controls are re-run at release time.
  rpc ResolveHold(ResolveHoldRequest) returns (AegisDecision);

  rpc TriggerKillSwitch(TriggerKillSwitchRequest) returns (KillSwitchState);
  rpc ResetKillSwitch(ResetKillSwitchRequest) returns (ResetKillSwitchResponse);
  rpc Heartbeat(HeartbeatRequest) returns (KillSwitchState);       // operator heartbeat
  rpc GetKillSwitchState(Empty) returns (KillSwitchState);
  rpc WatchKillSwitchState(Empty) returns (stream KillSwitchState);

  rpc GetAegisState(Empty) returns (AegisState);
  // PROPOSED: source of truth for positions, drawdown and NAV.
  rpc ReportExecution(ExecutionReport) returns (Ack);
}

message Empty {}

message Ack {
  bool ok = 1;
  string detail = 2;
}

enum DecisionStatus {
  DECISION_UNSPECIFIED = 0;
  DECISION_APPROVED = 1;
  DECISION_REJECTED = 2;
  DECISION_HELD_FOR_HUMAN = 3;
}

enum ReasonCode {
  REASON_UNSPECIFIED = 0;
  REASON_KILL_SWITCH_ACTIVE = 1;
  REASON_INVALID_SIGNAL = 2;
  REASON_SYMBOL_NOT_ALLOWED = 3;
  REASON_SHORT_SELLING_DISABLED = 4;
  REASON_OUTSIDE_TRADING_SESSION = 5;
  REASON_SIGNAL_EXPIRED = 6;
  REASON_SIGNAL_FROM_FUTURE = 7;
  REASON_DUPLICATE_SIGNAL = 8;
  REASON_REPLAY_PAYLOAD_MISMATCH = 9;
  REASON_STALE_REFERENCE_PRICE = 10;
  REASON_PRICE_COLLAR = 11;
  REASON_MAX_ORDER_QUANTITY = 12;
  REASON_MAX_ORDER_NOTIONAL = 13;
  REASON_ORDER_SIZE_ADV = 14;
  REASON_POSITION_LIMIT_SYMBOL = 15;
  REASON_GROSS_EXPOSURE_LIMIT = 16;
  REASON_DAILY_DRAWDOWN = 17;
  REASON_RATE_LIMIT = 18;
  REASON_LOW_OMEGA = 19;
  REASON_REGIME_LOW_CONFIDENCE = 20;
  REASON_REGIME_MISMATCH = 21;
  REASON_UNUSUAL_ORDER_SIZE = 22;
  REASON_STATE_UNAVAILABLE = 23;
  REASON_HSM_UNAVAILABLE = 24;
  REASON_AUDIT_UNAVAILABLE = 25;
  REASON_INTERNAL_ERROR = 26;
  REASON_HOLD_EXPIRED = 27;
  REASON_HOLD_REJECTED_BY_OPERATOR = 28;
}

// One evaluated control. threshold/observed are decimal strings (no doubles for money).
message ControlResult {
  string control_id = 1;        // "C01".."C19" per the Phase 3 spec
  bool is_hard = 2;
  bool passed = 3;
  ReasonCode reason = 4;        // REASON_UNSPECIFIED when passed
  string threshold = 5;
  string observed = 6;
  string detail = 7;
}

// Short-lived, single-use authorisation over the exact order. See spec section 7.
message Attestation {
  string canonical_version = 1;   // "afe-attest-v1"
  bytes payload_sha256 = 2;       // SHA-256 of the canonical text
  bytes signature = 3;            // HSM signature over payload_sha256
  string key_id = 4;
  int64 decided_at_ns = 5;
  int64 expires_at_ns = 6;        // PROPOSED decided_at_ns + 5 s
  uint64 aegis_state_seq = 7;
  string limits_config_sha256 = 8;
}

message AegisDecision {
  string signal_id = 1;
  DecisionStatus decision = 2;
  SignalStatus signal_status = 3;         // mapping per spec section 4.2
  repeated ControlResult results = 4;     // every evaluated control, passed or not
  repeated ReasonCode reasons = 5;        // reasons of failed controls (convenience)
  Attestation attestation = 6;            // set only when APPROVED
  OrderRequest order = 7;                 // set only when APPROVED; hsm_signature/hsm_key_id mirror the attestation
  string hold_id = 8;                     // set only when HELD_FOR_HUMAN
  int64 hold_expires_at_ns = 9;
  int64 decided_at_ns = 10;
  uint64 aegis_state_seq = 11;
  string aegis_version = 12;
}

message ResolveHoldRequest {
  string hold_id = 1;
  string operator_id = 2;
  string second_approver_id = 3;          // required or not: TODO(owner)
  bool approve = 4;
  string note = 5;
  double reverse_guardrail_distress_score = 6;   // classifier output, not money
  bool cooling_period_enforced = 7;
}

// ---- Kill switch ----

enum KillSwitchLevel {
  KILL_LEVEL_NORMAL = 0;
  KILL_LEVEL_SOFT = 1;
  KILL_LEVEL_LOGIC = 2;
  KILL_LEVEL_DEAD_MANS = 3;
  KILL_LEVEL_HARD = 4;
  KILL_LEVEL_PHYSICAL = 5;
}

message LatchedTrigger {
  string trigger_id = 1;
  KillSwitchLevel level = 2;
  string reason = 3;
  string actor_id = 4;            // operator id or automated source, e.g. "audit-logger/kl-monitor"
  int64 latched_at_ns = 5;
}

message KillSwitchState {
  KillSwitchLevel effective_level = 1;     // max over latched triggers
  repeated LatchedTrigger latches = 2;
  uint64 state_seq = 3;                    // monotonically increasing
  int64 updated_at_ns = 4;
  int64 last_operator_heartbeat_ns = 5;
  int64 operator_heartbeat_deadline_ns = 6;
}

message TriggerKillSwitchRequest {
  KillSwitchLevel level = 1;
  string reason = 2;
  string actor_id = 3;
  string evidence_ref = 4;
}

message Authorization {
  string approver_id = 1;
  string role = 2;                         // e.g. operator, compliance
  int64 approved_at_ns = 3;
  string credential_ref = 4;               // authentication evidence; scheme TODO(owner)
}

message ResetKillSwitchRequest {
  string trigger_id = 1;
  repeated Authorization approvals = 2;    // count/roles required per level: spec section 5.2
  string root_cause_ref = 3;
  string note = 4;
}

message ResetKillSwitchResponse {
  bool accepted = 1;
  string refusal_reason = 2;
  KillSwitchState state = 3;
}

message HeartbeatRequest {
  string operator_id = 1;
  int64 client_ts_ns = 2;
}

// ---- State / execution feedback ----

message AegisState {
  KillSwitchState kill = 1;
  string limits_config_sha256 = 2;
  bool hsm_ok = 3;
  bool audit_sink_ok = 4;
  bool reference_data_fresh = 5;
  int32 open_holds = 6;
  string build_version = 7;
}

message ExecutionReport {
  string order_id = 1;
  string signal_id = 2;
  string symbol = 3;
  OrderSide side = 4;
  OrderStatus status = 5;
  int64 filled_qty_nanos = 6;
  int64 avg_fill_price_nanos = 7;
  int64 account_equity_nanos = 8;          // for drawdown control C15
  int64 reported_at_ns = 9;
}
```

### A.2 Additions to existing files (new field numbers; do not change existing types in place)

`agent-core/shared/proto/trade_signal.proto`, add inside `message TradeSignal`:

```proto
  // Fixed-point (1e-9) replacements for the double money fields above.
  int64 quantity_nanos = 22;
  int64 price_limit_nanos = 23;           // 0 = market (Aegis will bound it, spec 4.5)
  int64 estimated_total_cost_nanos = 24;
  string strategy_id = 25;                // e.g. "AFE-STRATEGY-001"; used by the regime gate C18
```

and change `double quantity = 5;` to `double quantity = 5 [deprecated = true];` and `double price_limit = 19;` to `double price_limit = 19 [deprecated = true];`. Add `_nanos` counterparts for `estimated_spread_cost`, `estimated_market_impact` and `estimated_venue_fees` if the manifest needs them.

`agent-core/shared/proto/order_request.proto`, add inside `message OrderRequest`:

```proto
  int64 quantity_nanos = 18;
  int64 limit_price_nanos = 19;
  int64 stop_price_nanos = 20;
  int64 attestation_expires_at_ns = 21;
```

and mark `quantity = 7`, `limit_price = 8`, `stop_price = 9` as `[deprecated = true]`.
