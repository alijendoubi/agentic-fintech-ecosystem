# Shared protobuf contracts

Single source of truth for every cross-component message and gRPC service in AFE
(package `afe.shared`, proto3). Files here are imported by bare name
(`import "trade_signal.proto";`), so this directory is the include root for every
generator (Python stubs, Rust `tonic-build`, `buf`).

## Files

| File | Contents |
|---|---|
| `market_snapshot.proto` | `MarketSnapshot`, `RegimeLabel`, `RegimeLabelPacket` |
| `trade_signal.proto` | `TradeSignal`, `SignalSide`, `SignalStatus` |
| `order_request.proto` | `OrderRequest`, `OrderSide`, `OrderType`, `ExecAlgo`, `OrderStatus` |
| `compliance_manifest.proto` | `ComplianceManifest`, `PTCCheckResult`, `PTCType`, `HITLOverrideRecord`, `PostTradeMetrics` |
| `aegis.proto` | `service Aegis` and all its messages (Phase 3, spec `docs/specs/phase_3_aegis_execution.md`, Appendix A) |
| `buf.yaml` | buf lint + breaking-change configuration |
| `generate.sh` | Python stub generation |
| `check-breaking.sh` | breaking-change check against a git ref |

## Generating code

### Python (message, gRPC and `.pyi` stubs)

```bash
pip install grpcio-tools
agent-core/shared/proto/generate.sh          # Linux, macOS, Windows Git Bash
PYTHON=/path/to/python agent-core/shared/proto/generate.sh   # force an interpreter
```

Output goes to `agent-core/shared/generated/` (gitignored). Modules are generated
flat, so put that directory on the path:

```python
import sys
sys.path.insert(0, "agent-core/shared/generated")
import trade_signal_pb2, aegis_pb2, aegis_pb2_grpc
```

### Rust

Use `tonic-build` 0.12 / `prost` 0.13 in the crate's `build.rs`, with this
directory as the include path, and `protoc` installed (the `afe-rust-dev` image
has it):

```rust
tonic_build::configure()
    .build_server(true)   // aegis crate: server; sensory-array/execution-motor: client
    .build_client(true)
    .compile_protos(&["../../shared/proto/aegis.proto" /* + the others you use */],
                    &["../../shared/proto"])?;
```

```rust
pub mod shared { tonic::include_proto!("afe.shared"); }
```

The whole directory was verified to compile (server + client) with tonic-build
0.12 / prost 0.13 during ALI-22. Deprecated fields become
`#[deprecated]` in Rust; use `#[allow(deprecated)]` only at a documented boundary.

### Lint and breaking-change checks (buf)

```bash
cd agent-core/shared/proto
buf lint                                   # or: docker run --rm -v "$PWD:/workspace" -w /workspace bufbuild/buf lint
BASE_REF=origin/main ./check-breaking.sh   # compares the working tree with the protos at BASE_REF
```

`buf.yaml` uses the `STANDARD` lint category with a small, commented list of
waivers (package layout, legacy/spec-fixed enum names, the spec-fixed Aegis RPC
shape) and the `FILE` breaking category. New protos must satisfy every other
rule.

## Compatibility rules

1. **Never renumber and never reuse a field number**, even for a deleted field.
2. **Never change a field's type or name in place.** The wire types differ
   (`double` is fixed64, `int64` is varint); a type change silently corrupts data.
   Add a new field with a new number instead. Example: money moved from `double`
   to `int64` fixed-point via new `*_nanos` fields.
3. **Deprecate, then remove.** Step 1: mark `[deprecated = true]` and document the
   replacement in a comment (both fields coexist). Step 2: once every producer
   and consumer has migrated and one release has shipped, delete the field and add
   `reserved <number>; reserved "<name>";` in the same change. Removal is a
   deliberate breaking change and needs a `buf breaking` exception noted in the PR.
4. **Enums**: never renumber or repurpose values; add new values with new numbers.
   Consumers must tolerate unknown values (proto3 keeps the number).
5. **Adding** fields, messages, enum values and RPCs is allowed and non-breaking.
6. Money, prices and share quantities on the Aegis decision path are `int64`
   nanos (1e-9 units), never `double`. Non-money statistics (omega, z-scores,
   volatility) may stay `double`.
7. Do not rely on proto3 default values for safety decisions. In particular
   `KillSwitchLevel` value 0 is `KILL_LEVEL_NORMAL`: a missing state must be
   treated as HARD, never as NORMAL (see the header of `aegis.proto`).
8. CI must run `buf lint` and `check-breaking.sh` (see `.github/workflows/ci.yml`).

## Services and RPCs

| Service / RPC | Request -> Response | Server (owner) | Callers |
|---|---|---|---|
| `Aegis.SubmitSignal` | `TradeSignal` -> `AegisDecision` | `zone-b/aegis` | `zone-a/cognitive-core` |
| `Aegis.ResolveHold` | `ResolveHoldRequest` -> `AegisDecision` | `zone-b/aegis` | HITL interface (Zone C) |
| `Aegis.TriggerKillSwitch` | `TriggerKillSwitchRequest` -> `KillSwitchState` | `zone-b/aegis` | operator, `zone-c/audit-logger` KL monitor, workstation script |
| `Aegis.ResetKillSwitch` | `ResetKillSwitchRequest` -> `ResetKillSwitchResponse` | `zone-b/aegis` | operator(s) |
| `Aegis.Heartbeat` | `HeartbeatRequest` -> `KillSwitchState` | `zone-b/aegis` | operator |
| `Aegis.GetKillSwitchState` | `Empty` -> `KillSwitchState` | `zone-b/aegis` | any component |
| `Aegis.WatchKillSwitchState` | `Empty` -> stream `KillSwitchState` | `zone-b/aegis` | `zone-b/execution-motor` (cancel on L2+) |
| `Aegis.GetAegisState` | `Empty` -> `AegisState` | `zone-b/aegis` | operator, monitoring |
| `Aegis.ReportExecution` | `ExecutionReport` -> `Ack` | `zone-b/aegis` | `zone-b/execution-motor` |
| `Aegis.PushReferenceData` | `PushReferenceDataRequest` -> `PushReferenceDataResponse` (additive, PKG-E2) | `zone-b/aegis` | market-data feed (cert role `market-data-writer`) |

## Messages and enums

"Producer" is the component that fills the message; "Consumers" read it.

| Message / enum | File | Producer (owner) | Consumers |
|---|---|---|---|
| `MarketSnapshot` | market_snapshot | `zone-b/sensory-array` | `zone-a/cognitive-core`, `zone-b/aegis`, `zone-c/compliance-manifest` |
| `RegimeLabel` (enum) | market_snapshot | `zone-a/regime-detector` (HMM), `zone-b/sensory-array` | cognitive-core, aegis |
| `RegimeLabelPacket` | market_snapshot | `zone-a/regime-detector` | `zone-b/sensory-array`, cognitive-core |
| `TradeSignal` | trade_signal | `zone-a/cognitive-core` (Judge node) | `zone-b/aegis`, `zone-c/compliance-manifest` |
| `SignalSide`, `SignalStatus` (enums) | trade_signal | cognitive-core (side), aegis (status) | aegis, compliance-manifest |
| `OrderRequest` | order_request | `zone-b/aegis` (attested) | `zone-b/execution-motor`, `zone-c/compliance-manifest` |
| `OrderSide`, `OrderType`, `ExecAlgo`, `OrderStatus` (enums) | order_request | aegis, execution-motor | execution-motor, aegis |
| `ComplianceManifest` | compliance_manifest | `zone-c/compliance-manifest`, `zone-c/audit-logger` | audit / regulator export |
| `PTCCheckResult`, `PTCType` | compliance_manifest | `zone-b/aegis` (via manifest pipeline) | compliance-manifest |
| `HITLOverrideRecord` | compliance_manifest | HITL interface (Zone C) | compliance-manifest |
| `PostTradeMetrics` | compliance_manifest | `zone-b/execution-motor` | compliance-manifest |
| `AegisDecision`, `ControlResult`, `Attestation` | aegis | `zone-b/aegis` | cognitive-core, HITL interface, execution-motor / broker gateway, compliance-manifest |
| `DecisionStatus`, `ReasonCode` (enums) | aegis | `zone-b/aegis` | all Aegis callers |
| `ResolveHoldRequest` | aegis | HITL interface (Zone C) | `zone-b/aegis` |
| `KillSwitchLevel` (enum), `LatchedTrigger`, `KillSwitchState` | aegis | `zone-b/aegis` | operator UI, execution-motor, audit-logger |
| `TriggerKillSwitchRequest`, `ResetKillSwitchRequest`, `ResetKillSwitchResponse`, `Authorization`, `HeartbeatRequest` | aegis | operator tooling / monitors | `zone-b/aegis` |
| `AegisState` | aegis | `zone-b/aegis` | operator, monitoring |
| `ExecutionReport`, `Ack`, `Empty` | aegis | `zone-b/execution-motor` (report) | `zone-b/aegis` |

Producer/consumer assignments follow the Phase 3 spec RPC table and the phase
specs and the component layout under `agent-core/`. They are intent, not verified against code: no service or module imports these types yet, so treat unclear rows (for example `RegimeLabelPacket`) as open for the owning engineer to confirm.

## Fixed-point money fields (ALI-22)

New fields, all `int64` with unit 1e-9 (a value of `1_500_000_000` is 1.5):

| Message | New field | Number | Replaces (deprecated) |
|---|---|---|---|
| `TradeSignal` | `quantity_nanos` | 22 | `quantity` (5) |
| `TradeSignal` | `price_limit_nanos` | 23 | `price_limit` (19) |
| `TradeSignal` | `estimated_total_cost_nanos` | 24 | `estimated_total_cost` (15), not yet deprecated |
| `TradeSignal` | `strategy_id` (string) | 25 | new |
| `TradeSignal` | `estimated_spread_cost_nanos` | 26 | `estimated_spread_cost` (12), not yet deprecated |
| `TradeSignal` | `estimated_market_impact_nanos` | 27 | `estimated_market_impact` (13), not yet deprecated |
| `TradeSignal` | `estimated_venue_fees_nanos` | 28 | `estimated_venue_fees` (14), not yet deprecated |
| `OrderRequest` | `quantity_nanos` | 18 | `quantity` (7) |
| `OrderRequest` | `limit_price_nanos` | 19 | `limit_price` (8) |
| `OrderRequest` | `stop_price_nanos` | 20 | `stop_price` (9) |
| `OrderRequest` | `attestation_expires_at_ns` | 21 | new |
