# refdata-bridge

Zone B service. Bridges market data from `sensory-array` (Rust) and regime
labels from `regime-detector` (Python) into Aegis's `PushReferenceData` RPC,
so control **C08** (reference-data freshness) and **C18** (regime gate) have
real data to check instead of Aegis rejecting every signal by default (see
`agent-core/zone-b/aegis/README.md`, "Reference data feed").

This package only reads `sensory-array`, `regime-detector` and Aegis; it does
not modify any of them.

## What it reads

Both channels are Redis **pub/sub**, matching the existing repo convention
(`sensory-array` and `regime-detector` already PUBLISH, nothing in this repo
polls a Redis key for this data) — see
`agent-core/zone-b/sensory-array/src/publisher.rs` and
`agent-core/zone-a/regime-detector/regime_detector/publisher.py`.

* `sensory:snapshots` (`SNAPSHOT_CHANNEL`) — JSON `MarketSnapshot` messages
  (`agent-core/zone-b/sensory-array/src/normalizer.rs`). Fields used:
  `symbol`, `ingestion_ts_ns`, `mid_price`, `adv_30d`, `is_stale`, `warmup`.
* `regime:labels` (`REGIME_CHANNEL`) — JSON regime messages
  (`agent-core/zone-a/regime-detector/regime_detector/publisher.py`). Fields
  used: `label`, `confidence`, `ts_ns`.

## What it pushes

`Aegis.PushReferenceData(PushReferenceDataRequest)` over mTLS, batched every
`BATCH_INTERVAL_S` (default 1s):

* `repeated ReferenceSnapshot snapshots` — one per symbol with a value newer
  than what Aegis last accepted for it. Prices/volumes are converted from
  the sensory-array JSON floats to fixed-point int64 nanos via `Decimal`
  half-even rounding (`refdata_bridge/mapping.py::to_nanos`), the same
  convention as `execution_motor.service.to_nanos` / `proto_adapter.from_nanos`
  in `agent-core/zone-b/execution-motor`. Never plain float multiplication.
* `RegimeLabelPacket regime` — the single most recently timestamped regime
  message seen (see "Known limitations" below), when newer than what Aegis
  last accepted.

Aegis's role for this RPC is `market-data-writer` (see
`agent-core/zone-b/aegis/README.md`, "Identities file" — the peer entry there
is `"market-data": ["market-data-writer"]`). This service's client
certificate's CN must be listed under that role in Aegis's
`AEGIS_IDENTITIES_FILE`, or every push is `PERMISSION_DENIED`.

## Fail-closed behaviour

* **Redis unreachable**: the affected subscriber logs a structured warning
  and retries with jittered backoff (`refdata_bridge/redis_source.py`); it
  never crashes the process and never silently stops.
* **Missing/invalid required field** (`symbol`, `mid_price`, `adv_30d`, a
  timestamp, an unknown regime label, confidence out of `[0, 1]`): the
  message is logged and dropped (`MappingError`), never guessed or forwarded.
* **Not confident a snapshot is fresh** (source flagged `is_stale`, still in
  `warmup`, or older/newer than this bridge's own clock allows): the
  snapshot is still pushed, but with `is_stale=True` — never silently as
  fresh.
* **Aegis rejects an item** (`PushReferenceDataResponse.rejected`): every
  rejection is logged with its `key` and `reason` (`reference_data_rejected`),
  and that item's watermark is **not** advanced, so it is retried next cycle
  instead of silently vanishing.
* **Aegis unreachable / RPC failure**: the push cycle logs `aegis_push_failed`,
  marks `/health` unhealthy, and tries again next cycle — one failed cycle
  never crashes the loop or stops the Redis subscribers.

## `/health`

`GET http://<HEALTH_HOST>:<HEALTH_PORT>/health` (default `0.0.0.0:8080`)
returns JSON:

```json
{"healthy": true, "last_success_push_ns": 1700000000000000000,
 "last_attempt_push_ns": 1700000000000000000, "last_error": null, "age_s": 0.4}
```

`healthy` is `false` (HTTP 503) before the first successful push, or once the
last successful push is older than `max(30s, 3 * BATCH_INTERVAL_S)` — a
stalled bridge (Redis down, Aegis down, or a wedged loop) is visibly
unhealthy. The Docker image's `HEALTHCHECK` polls this endpoint. A heartbeat
file at `HEARTBEAT_PATH` is also touched every cycle (same convention as
`regime-detector/regime_detector/healthcheck.py`), for a HEALTHCHECK that
does not need the HTTP port exposed.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379` | Redis connection for both subscribers. |
| `SNAPSHOT_CHANNEL` | `sensory:snapshots` | sensory-array's PUBLISH channel. |
| `REGIME_CHANNEL` | `regime:labels` | regime-detector's PUBLISH channel. |
| `AEGIS_TARGET` | `localhost:50051` | `host:port` of Aegis's gRPC listener. |
| `REFDATA_CLIENT_TLS_CA` | unset | PEM CA Aegis's server certificate chains to. |
| `REFDATA_CLIENT_TLS_CERT` | unset | This service's client certificate PEM (CN in Aegis's `market-data-writer` role). |
| `REFDATA_CLIENT_TLS_KEY` | unset | Private key PEM for `REFDATA_CLIENT_TLS_CERT`. |
| `ENVIRONMENT` | `production` | One of `production`, `staging`, `development`, `test` (anything else refuses to start). Unset means production, like execution-motor and Aegis. `production` refuses to start without `REFDATA_CLIENT_TLS_CA` (no plaintext Aegis channel in production). |
| `AFE_PROTO_DIR` | `agent-core/shared/generated` (relative to this package) | Directory holding the generated `aegis_pb2*`/`market_snapshot_pb2` modules. |
| `BATCH_INTERVAL_S` | `1.0` | Push cycle period. |
| `MAX_BATCH_SNAPSHOTS` | `64` | Max snapshots per `PushReferenceData` call. |
| `SNAPSHOT_STALE_AFTER_S` | `2.0` | A snapshot older (or from further in the future) than this, by this bridge's own clock, is marked `is_stale=true`. |
| `REGIME_STALE_AFTER_S` | `30.0` | Reserved for a future per-message regime staleness check (see "Known limitations"). |
| `AEGIS_RPC_TIMEOUT_S` | `2.0` | Per-call gRPC deadline. |
| `HEALTH_HOST` / `HEALTH_PORT` | `0.0.0.0` / `8080` | `/health` HTTP listener. |
| `HEARTBEAT_PATH` | `/tmp/refdata-bridge.heartbeat` | Touched every push cycle. |
| `LOG_LEVEL` | `info` | structlog level. |

## Running tests

```bash
cd agent-core/zone-b/refdata-bridge
pip install -r requirements-dev.txt
bash ../../shared/proto/generate.sh   # needed by the mTLS/gRPC tests
python -m pytest --cov=refdata_bridge --cov-report=term-missing
ruff check --config ../../../ruff.toml .
```

Tests use a small in-memory Redis pub/sub stub (`tests/fake_redis.py` — no
`fakeredis` dependency exists anywhere in this repo yet) and a real,
in-process loopback Aegis gRPC server with ephemeral mTLS certificates
generated via `cryptography` (`tests/fake_aegis.py`), not mocks: the mTLS
handshake, the wire messages, and Aegis's per-item rejection shape are all
exercised for real.

## Known limitations

* **`RegimeLabelPacket` has no symbol field** (`market_snapshot.proto`), so
  Aegis's reference-data store holds exactly **one global** regime label, not
  one per symbol, even though `regime-detector` publishes a label per symbol
  on `regime:labels`. This bridge therefore forwards only the single most
  recently timestamped regime message across all symbols
  (`refdata_bridge/batch.py::BatchState.record_regime`) — an assumption
  forced by the current proto contract, not a choice. If Zone A needs a
  per-symbol regime signal at Aegis, `RegimeLabelPacket` needs a `symbol`
  field first (a proto change outside this package's scope).
* **`RegimeLabelPacket.state_index` is always `0`**: the JSON
  `regime-detector` actually publishes on `regime:labels`
  (`regime_detector/publisher.py::RegimeMessage.to_json`) never carries a
  `state_index` (`symbol`/`label`/`confidence`/`ts_ns` only, plus `reason`
  for `REGIME_UNKNOWN`), so there is nothing to forward. `0` looks
  identical to a real state index `0`, and is not distinguishable from one in
  the pushed data.
* **`AEGIS_TARGET` is a single `host:port` string**, whereas
  `agent-core/zone-a/cognitive-core/sinks.py` takes `host`/`port` as separate
  values and only the TLS file paths from the environment. This package uses
  one combined variable per the task's stated naming convention; if Aegis
  client wiring is later unified across Zone A/B, that split should probably
  be adopted here too.
* **Docker image proto build context**: like `zone-a/cognitive-core`, the
  Dockerfile's build context must be `agent-core/` (not this directory) so
  it can generate stubs from `shared/proto/`. This was not runnable/verified
  in this session (no Docker available); `docker compose config` was not
  re-run against it.
* **`REGIME_STALE_AFTER_S` is currently unused**: regime freshness is
  enforced by Aegis itself (`max_regime_age_ms`, default 60s) once pushed;
  this bridge does not yet drop a regime message that arrived stale from
  Redis before building a batch (a snapshot gets that treatment via
  `SNAPSHOT_STALE_AFTER_S`/`is_stale`, but `RegimeLabelPacket` has no
  `is_stale` field to set, so the only fail-closed option today is to *not
  forward* a message this bridge judges too old — not yet implemented).
  Until then, an old regime label already rejected as stale by Aegis will be
  logged as a rejection every cycle rather than being pre-filtered locally.
* **No integration test against the real `sensory-array` / `regime-detector`
  binaries or a real Redis/Aegis** was run (no Docker/Redis/Aegis available
  in this session); all tests use the fakes described above. The JSON field
  names this bridge parses were read directly from
  `normalizer.rs`/`publisher.py` source and their own unit tests, not
  observed on the wire.
* **`AEGIS_ENV=production` also requires `AEGIS_ALLOW_FRESH_STATE`/limits
  files etc. on the Aegis side** (see its README) — this bridge's own
  `ENVIRONMENT=production` gate only concerns its own outbound TLS
  requirement, not Aegis's full production readiness.
