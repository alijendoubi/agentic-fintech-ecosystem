//! Fixtures shared by unit and integration tests. Not used by production code.

use crate::normalizer::MarketSnapshot;

/// A valid, fresh-looking snapshot for `symbol` stamped at `ts_ns`.
pub fn sample_snapshot(symbol: &str, ts_ns: i64) -> MarketSnapshot {
    MarketSnapshot {
        symbol: symbol.to_string(),
        ingestion_ts_ns: ts_ns,
        exchange_ts_ns: ts_ns - 1_000,
        l2_recv_ts_ns: ts_ns - 500,
        mid_price: 150.025,
        bid_price: 150.0,
        ask_price: 150.05,
        bid_size: 200.0,
        ask_size: 300.0,
        spread: 0.05,
        last_trade_price: 150.02,
        last_trade_size: 100.0,
        last_trade_ts_ns: ts_ns - 2_000,
        trade_recv_ts_ns: ts_ns - 1_500,
        z_score: 1.23,
        mad_score: 0.98,
        z_mad_divergence: false,
        order_flow_imbalance: -0.2,
        realized_volatility: 0.25,
        adv_30d: 50_000_000.0,
        warmup: false,
        regime_label: "TRENDING_BULL".into(),
        regime_confidence: 0.87,
        is_stale: false,
        stale_reason: "".into(),
    }
}
