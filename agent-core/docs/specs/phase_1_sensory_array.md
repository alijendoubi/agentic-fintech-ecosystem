# Phase 1 Spec — Sensory Array + Regime Detector

_Zone B (Rust) + Zone A (Python) | Latency budget: <5ms hot path_

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
  main.rs           — tokio runtime, spawn tasks, graceful shutdown (SIGTERM/SIGINT)
  config.rs         — env config via dotenvy; validated at startup
  error.rs          — SensoryError enum (WsError, QdbError, ParseError, ...)
  ingestor.rs       — WebSocket connect, auth, subscribe, route messages, reconnect
  normalizer.rs     — per-symbol rolling state: Z-score, MAD, OFI, realized_vol, ADV
  ttl.rs            — TTL freshness checker; sets is_stale + stale_reason
  questdb_writer.rs — async ILP TCP writer; buffer + flush cycle
```

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

### Reconnect Policy
- Exponential backoff: 1s → 2s → 4s → 8s → 16s (max)
- Jitter: ±20% of delay
- After 5 consecutive failures: emit CRITICAL tracing event

---

## 2. regime-detector (Python)

### Model: GaussianHMM, 5 states

```
States: TRENDING_BULL, TRENDING_BEAR, HIGH_VOL_CHOP, LOW_VOL_CHOP, CRISIS
Features (per symbol, from QuestDB rolling 200-bar window):
  - log_return          (1-bar)
  - realized_vol        (20-bar annualized)
  - bid_ask_spread      (normalized)
  - order_flow_imbalance

Training:
  - Bootstrap: 30 minutes of streaming data OR load from MODEL_PATH if exists
  - Retrain every 4 hours on rolling 200-bar window (online update via score/fit)
  - Persist to MODEL_PATH via joblib

Inference (1s tick):
  - For each symbol with >20 bars: predict(feature_vector[-1:])
  - posterior = model.predict_proba(X[-5:]).mean(axis=0)  // smooth over 5 bars
  - label = argmax(posterior), confidence = max(posterior)
  - Publish to Redis: regime:labels channel
    → JSON: {"symbol": "AAPL", "label": "TRENDING_BULL", "confidence": 0.87, "ts_ns": ...}
```

### Module Map

```
hmm.py          — HMM training, inference, persistence, Redis publish loop
config.py       — env config
questdb_client.py — polling query for features
redis_client.py — publisher

main entrypoint: hmm.py (runs async loop)
```

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
- `test_hmm_5_states` — model has n_components=5
- `test_feature_shape` — feature matrix shape == (N, 4)
- `test_regime_label_published` — mock Redis, assert publish called with valid JSON
- `test_confidence_in_range` — posterior sums to 1.0
