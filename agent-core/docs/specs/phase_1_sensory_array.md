# Phase 1 Spec — Sensory Array + Regime Detector

_Zone B (Rust) + Zone A (Python) | Latency budget: <5ms hot path (unmeasured target)_

> **Implementation status (2026-09-21).** Both services are implemented; this spec was updated to the real module maps
> and behaviour. `sensory-array` is a self-contained crate: **no `protoc` / protobuf codegen is needed** to build or
> test it (Aegis is the Rust crate that needs `protoc`). `regime-detector` tests pass locally (134); sensory-array
> has never run against the real Polygon feed and no latency figure has been measured. Commands and compose usage:
> `agent-core/README.md`. Anything below written as a target (latency budgets, TTL values) is unvalidated.

---

## Scope

Build the two real-time data services that feed every downstream decision:

| Service | Zone | Language | Output |
|---|---|---|---|
| `sensory-array` | B | Rust | QuestDB rows + Redis pub/sub |
| `regime-detector` | A | Python | Redis `regime:labels` channel |

---

## 1. sensory-array (Rust)

### Input
- **Polygon.io WebSocket** `wss://socket.polygon.io/stocks`
- Subscriptions: `Q.*` (NBBO quotes), `T.*` (trade prints)
- Auth: `{"action":"auth","params":"<POLYGON_API_KEY>"}`

### Module Map

```
src/
  main.rs           - tokio runtime, wiring, graceful shutdown; `--healthcheck` flag is the container HEALTHCHECK
  lib.rs            - crate root (binary + integration tests share the library)
  config.rs         - env config (dotenvy), validated at startup; fails closed on bad/missing values
  error.rs          - SensoryError
  polygon.rs        - Polygon message parsing (Quote / Trade / Status / Auth)
  ingestor.rs       - WebSocket connect, auth, subscribe, route messages, reconnect with backoff + jitter
  validate.rs       - symbol grammar and input validation (ticker is untrusted; never reaches ILP unescaped)
  normalizer.rs     - per-symbol rolling state: Z-score, MAD, OFI, realized_vol, ADV
  ttl.rs            - TTL freshness checker; sets is_stale + stale_reason
  queue.rs          - bounded queues between ingest and sinks
  ilp.rs            - QuestDB ILP line encoding (escaping, non-finite rejection)
  ilp_secure.rs     - optional TLS + ECDSA-authenticated ILP (cargo feature `ilp-secure`, off by default)
  questdb_writer.rs - async ILP TCP writer; buffer + flush cycle
  publisher.rs      - Redis PUBLISH on `sensory:snapshots` (ConnectionManager, auto-reconnect)
  regime.rs         - Redis subscriber on `regime:labels`; freshness-bounded regime cache
  health.rs         - heartbeat file used by the container health check
  metrics.rs, runtime.rs, testutil.rs
tests/
  ingestor_reconnect.rs, live_sinks.rs
```

### Configuration (environment)

| Variable | Notes |
|---|---|
| `POLYGON_API_KEY` | required; never baked into the image |
| `POLYGON_WS_URL` | default `wss://socket.polygon.io/stocks`; must be `wss://` |
| `POLYGON_SYMBOLS` | optional symbol list |
| `QUESTDB_ILP_HOST` / `QUESTDB_ILP_PORT` | ILP target (compose: `questdb` / `9009`) |
| `QUESTDB_ILP_TLS`, `QUESTDB_ILP_AUTH_KEY_ID`, `QUESTDB_ILP_AUTH_TOKEN`, `QUESTDB_ILP_TLS_CA_FILE` | secure ILP; needs a build with `--features ilp-secure` |
| `REDIS_URL` (or `REDIS_HOST` + `REDIS_PORT`) | compose sets `redis://redis:6379` |
| `FRESHNESS_L2_MS`, `FRESHNESS_PRINT_MS` | TTL thresholds (spec defaults 1 / 5 ms; unvalidated) |
| `ROLLING_WINDOW`, `ADV_WINDOW_DAYS`, `REGIME_MAX_AGE_S` | normalizer / regime windows |
| `MAX_RECONNECT_ATTEMPTS`, `RECONNECT_BASE_MS`, `RECONNECT_MAX_MS`, `WS_*_TIMEOUT_MS` | reconnect policy |
| `HEALTH_FILE`, `LOG_LEVEL` | health heartbeat path, log filter |

`src/config.rs` is authoritative for the full list, ranges and defaults.

### Data Flow

```
Polygon WS msg
  → parse JSON (Quote | Trade | Status | Auth)
  → QuoteMsg → normalizer.update(symbol, quote) → SymbolState
  → TradeMsg → normalizer.update_trade(symbol, trade) → update last_trade
  → SymbolState → ttl.check(state) → MarketSnapshot
  → questdb_writer.enqueue(snapshot)
  → redis_publisher.publish("sensory:snapshots", snapshot_json)

regime-label subscriber (side-task):
  → subscribe Redis "regime:labels"
  → update Arc<RwLock<HashMap<String, RegimeLabel>>> shared state
  → normalizer attaches latest regime label to each snapshot
```

### Normalizer: Rolling Windows per Symbol

```
struct RollingWindow {
  prices: VecDeque<f64>,   // max 20 for realized_vol
  daily_volumes: VecDeque<f64>, // max 30 for ADV
  current_day_volume: f64,
  last_trade_price: f64,
  last_trade_size: f64,
}

Z-Score:  (x - mean(prices)) / std(prices)
MAD:      median(|x - median(prices)|)
Divergence: |z_score - mad_score| > 2.0
OFI:      (bid_size - ask_size) / (bid_size + ask_size)
RealVol:  std(log_returns_20) * sqrt(252 * 6.5 * 3600)  // annualized
ADV-30d:  mean(daily_volumes[30])
```

### TTL Rules

| Signal | Max Staleness | stale_reason |
|---|---|---|
| L2 Order Book | 1ms | "L2_TTL_BREACH" |
| Trade Print | 5ms | "TRADE_TTL_BREACH" |

### QuestDB ILP Schema

```
market_data,symbol=<TAG>
  ingestion_ts=<ns>,exchange_ts=<i>,
  mid_price=<f>,bid_price=<f>,ask_price=<f>,
  bid_size=<f>,ask_size=<f>,spread=<f>,
  last_trade_price=<f>,last_trade_size=<f>,
  z_score=<f>,mad_score=<f>,z_mad_divergence=<b>,
  ofi=<f>,realized_vol=<f>,adv_30d=<f>,
  is_stale=<b>
  <ingestion_ts_ns>
```

Additions in the implementation (see `src/ilp.rs`): `regime_confidence`, `warmup`, and `order_flow_imbalance`, a
transitional alias of `ofi` that the regime-detector still queries (drop the alias once it reads `ofi`). The designated
timestamp column is QuestDB's ILP default `timestamp`; the regime-detector reads it via `QUESTDB_TS_COLUMN`.

### Reconnect Policy
- Exponential backoff: 1s → 2s → 4s → 8s → 16s (max)
- Jitter: ±20% of delay
- After 5 consecutive failures: emit CRITICAL tracing event

---

## 2. regime-detector (Python)

### Model: GaussianHMM, 5 states

```
States: TRENDING_BULL, TRENDING_BEAR, HIGH_VOL_CHOP, LOW_VOL_CHOP, CRISIS   (REGIME_UNKNOWN is the fail-closed value)
Features (per symbol, from QuestDB, rolling FEATURE_WINDOW bars, default 200):
  - log_return, realized_vol (20-bar annualized), spread (normalized), order_flow_imbalance

Training / persistence (as implemented; differs from the original draft):
  - Trains on QuestDB history; retrains every RETRAIN_INTERVAL_S (default 4 h).
  - Models are stored as HMAC-SHA256-wrapped numpy .npz files (no pickle/joblib) in MODEL_DIR.
    They are only saved and loaded when MODEL_HMAC_KEY (>= 32 chars) is set; otherwise models live in memory only.
    The legacy MODEL_PATH variable is only read as a fallback for MODEL_DIR's parent and is otherwise unused.

Inference (INFERENCE_INTERVAL_S, default 1 s):
  - For each valid symbol (^[A-Z.\-]{1,10}$) with >= MIN_BARS_FOR_INFERENCE bars: posterior smoothed over recent bars,
    label = argmax, confidence = max; stale data (MAX_DATA_AGE_S) or low confidence (MIN_CONFIDENCE) publishes REGIME_UNKNOWN.
  - Publishes JSON on Redis channel `regime:labels`:
    {"symbol": "AAPL", "label": "TRENDING_BULL", "confidence": 0.87, "ts_ns": ...}   (+ "reason" for REGIME_UNKNOWN)
```

### Module Map

```
hmm.py                       - container entrypoint shim: `python hmm.py` -> regime_detector.main.run()
regime_detector/
  config.py                  - env config, validated once into an immutable Settings (bad value => refuse to start)
  main.py, service.py        - process wiring and the inference/retrain loop
  questdb_client.py          - read-only QuestDB REST (/exec) client; symbols validated before reaching SQL
  features.py, model.py      - feature engineering and the HMM
  labels.py                  - RegimeLabel vocabulary (test-guarded against market_snapshot.proto)
  model_store.py             - HMAC-verified persistence
  publisher.py               - Redis publisher (client injected)
  healthcheck.py             - container HEALTHCHECK: heartbeat file freshness
tests/                       - 134 tests (pytest); `pip install -r requirements-dev.txt && python -m pytest`
```

Environment: `QUESTDB_HOST`, `QUESTDB_PORT` (HTTP, 9000), `REDIS_URL`, `MODEL_DIR`, `MODEL_HMAC_KEY`, `INFERENCE_INTERVAL_S`,
`RETRAIN_INTERVAL_S`, `FEATURE_WINDOW`, `MIN_BARS_FOR_INFERENCE`, `HEARTBEAT_PATH`, `LOG_LEVEL` (see `regime_detector/config.py`).
Note: the sensory-array's Redis pub/sub and the regime-detector live on the zone A<->B network in compose.

---

## Tests (TDD anchors)

### sensory-array (Rust)
- `normalizer_zscore_basic` — known values, assert z within 1e-6
- `normalizer_mad_heavy_tailed` — outlier spike, MAD more robust than Z
- `normalizer_ofi_balanced` — equal bid/ask → OFI near 0
- `ttl_stale_detection` — inject old timestamp, assert is_stale=true
- `questdb_ilp_format` — assert ILP line string format correct
- `ingestor_reconnect` — mock WS drop, assert reconnect fires after backoff

### regime-detector (Python)
- `test_hmm_5_states` — model has n_components=5 (see `tests/test_model.py`)
- `test_feature_shape` — feature matrix shape == (N, 4)
- `test_regime_label_published` — mock Redis, assert publish called with valid JSON
- `test_confidence_in_range` — posterior sums to 1.0

Implemented test locations: sensory-array unit tests live beside the code (`normalizer.rs`, `ttl.rs`, `ilp.rs`,
`questdb_writer.rs`, ...) plus `tests/ingestor_reconnect.rs` and `tests/live_sinks.rs`; regime-detector tests are in
`zone-a/regime-detector/tests/`. The anchors above are the original intent; the files are authoritative.
