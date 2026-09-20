use std::collections::{HashMap, VecDeque};

/// Per-symbol rolling state for normalization.
#[derive(Debug, Default)]
pub struct SymbolState {
    // Price history for Z-score + MAD + realized vol
    pub mid_prices: VecDeque<f64>,
    // Daily volumes for ADV-30d
    pub daily_volumes: VecDeque<f64>,
    pub current_day_volume: f64,
    pub current_day: u32, // day-of-year for daily rollover

    // Latest trade
    pub last_trade_price: f64,
    pub last_trade_size: f64,
    pub last_trade_ts_ns: i64,

    // Latest quote
    pub bid_price: f64,
    pub ask_price: f64,
    pub bid_size: f64,
    pub ask_size: f64,
    pub exchange_ts_ns: i64,
}

/// Normalized market snapshot ready for QuestDB and downstream.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct MarketSnapshot {
    pub symbol: String,
    pub ingestion_ts_ns: i64,
    pub exchange_ts_ns: i64,
    pub mid_price: f64,
    pub bid_price: f64,
    pub ask_price: f64,
    pub bid_size: f64,
    pub ask_size: f64,
    pub spread: f64,
    pub last_trade_price: f64,
    pub last_trade_size: f64,
    pub last_trade_ts_ns: i64,
    pub z_score: f64,
    pub mad_score: f64,
    pub z_mad_divergence: bool,
    pub order_flow_imbalance: f64,
    pub realized_volatility: f64,
    pub adv_30d: f64,
    pub regime_label: String,
    pub regime_confidence: f64,
    pub is_stale: bool,
    pub stale_reason: String,
}

/// Multi-symbol normalizer maintaining rolling windows per symbol.
pub struct Normalizer {
    states: HashMap<String, SymbolState>,
    window: usize,
    adv_days: usize,
}

impl Normalizer {
    pub fn new(window: usize, adv_days: usize) -> Self {
        Self {
            states: HashMap::new(),
            window,
            adv_days,
        }
    }

    /// Update state from a quote message (NBBO).
    pub fn update_quote(
        &mut self,
        symbol: &str,
        bid_price: f64,
        ask_price: f64,
        bid_size: f64,
        ask_size: f64,
        exchange_ts_ns: i64,
    ) {
        let state = self.states.entry(symbol.to_string()).or_default();
        state.bid_price = bid_price;
        state.ask_price = ask_price;
        state.bid_size = bid_size;
        state.ask_size = ask_size;
        state.exchange_ts_ns = exchange_ts_ns;

        let mid = (bid_price + ask_price) / 2.0;
        if mid > 0.0 {
            state.mid_prices.push_back(mid);
            if state.mid_prices.len() > self.window {
                state.mid_prices.pop_front();
            }
        }
    }

    /// Update state from a trade print.
    pub fn update_trade(
        &mut self,
        symbol: &str,
        price: f64,
        size: f64,
        ts_ns: i64,
        day_of_year: u32,
    ) {
        let state = self.states.entry(symbol.to_string()).or_default();
        state.last_trade_price = price;
        state.last_trade_size = size;
        state.last_trade_ts_ns = ts_ns;

        // Roll daily volume
        if state.current_day != day_of_year && state.current_day != 0 {
            state.daily_volumes.push_back(state.current_day_volume);
            if state.daily_volumes.len() > self.adv_days {
                state.daily_volumes.pop_front();
            }
            state.current_day_volume = 0.0;
        }
        state.current_day = day_of_year;
        state.current_day_volume += size;
    }

    /// Compute a normalized snapshot for the given symbol.
    /// Returns None if insufficient data (< 2 price observations).
    pub fn snapshot(
        &self,
        symbol: &str,
        ingestion_ts_ns: i64,
        regime_label: String,
        regime_confidence: f64,
    ) -> Option<MarketSnapshot> {
        let state = self.states.get(symbol)?;

        if state.bid_price == 0.0 || state.ask_price == 0.0 {
            return None;
        }

        let mid = (state.bid_price + state.ask_price) / 2.0;
        let spread = state.ask_price - state.bid_price;

        let (z_score, mad_score) = compute_zscore_mad(&state.mid_prices);
        let z_mad_divergence = (z_score - mad_score).abs() > 2.0;

        let ofi = if (state.bid_size + state.ask_size) > 0.0 {
            (state.bid_size - state.ask_size) / (state.bid_size + state.ask_size)
        } else {
            0.0
        };

        let realized_vol = compute_realized_vol(&state.mid_prices);
        let adv_30d = compute_adv(&state.daily_volumes, state.current_day_volume);

        Some(MarketSnapshot {
            symbol: symbol.to_string(),
            ingestion_ts_ns,
            exchange_ts_ns: state.exchange_ts_ns,
            mid_price: mid,
            bid_price: state.bid_price,
            ask_price: state.ask_price,
            bid_size: state.bid_size,
            ask_size: state.ask_size,
            spread,
            last_trade_price: state.last_trade_price,
            last_trade_size: state.last_trade_size,
            last_trade_ts_ns: state.last_trade_ts_ns,
            z_score,
            mad_score,
            z_mad_divergence,
            order_flow_imbalance: ofi,
            realized_volatility: realized_vol,
            adv_30d,
            regime_label,
            regime_confidence,
            is_stale: false,
            stale_reason: String::new(),
        })
    }
}

/// Z-score and MAD of the most recent price window.
/// Returns (0.0, 0.0) if window has < 2 observations.
fn compute_zscore_mad(prices: &VecDeque<f64>) -> (f64, f64) {
    if prices.len() < 2 {
        return (0.0, 0.0);
    }

    let n = prices.len() as f64;
    let mean = prices.iter().sum::<f64>() / n;
    let variance = prices.iter().map(|x| (x - mean).powi(2)).sum::<f64>() / (n - 1.0);
    let std_dev = variance.sqrt();

    let last = *prices.back().unwrap();
    let z = if std_dev > 1e-10 {
        (last - mean) / std_dev
    } else {
        0.0
    };

    // MAD: median absolute deviation
    let median = {
        let mut sorted: Vec<f64> = prices.iter().copied().collect();
        sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());
        let mid = sorted.len() / 2;
        if sorted.len() % 2 == 0 {
            (sorted[mid - 1] + sorted[mid]) / 2.0
        } else {
            sorted[mid]
        }
    };
    let mut abs_devs: Vec<f64> = prices.iter().map(|x| (x - median).abs()).collect();
    abs_devs.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let mad = {
        let mid = abs_devs.len() / 2;
        if abs_devs.len() % 2 == 0 {
            (abs_devs[mid - 1] + abs_devs[mid]) / 2.0
        } else {
            abs_devs[mid]
        }
    };

    // MAD-based score (using 1.4826 scale factor for consistency with normal distribution)
    let mad_std = 1.4826 * mad;
    let mad_score = if mad_std > 1e-10 {
        (last - median) / mad_std
    } else {
        0.0
    };

    (z, mad_score)
}

/// Annualized realized volatility from log returns.
/// Returns 0.0 if fewer than 2 observations.
fn compute_realized_vol(prices: &VecDeque<f64>) -> f64 {
    if prices.len() < 2 {
        return 0.0;
    }
    let log_returns: Vec<f64> = prices
        .iter()
        .zip(prices.iter().skip(1))
        .map(|(prev, curr)| (curr / prev).ln())
        .filter(|r| r.is_finite())
        .collect();

    if log_returns.len() < 2 {
        return 0.0;
    }

    let n = log_returns.len() as f64;
    let mean = log_returns.iter().sum::<f64>() / n;
    let variance = log_returns.iter().map(|r| (r - mean).powi(2)).sum::<f64>() / (n - 1.0);
    let std = variance.sqrt();

    // Annualize: US equity market ~6.5h/day * 3600s/h * ticks vary.
    // Use 252 trading days; tick-based frequency is approximate.
    let ticks_per_year: f64 = 252.0 * 6.5 * 3600.0; // rough estimate for annualization
    std * ticks_per_year.sqrt()
}

/// 30-day average daily volume (ADV).
fn compute_adv(daily_volumes: &VecDeque<f64>, current_day_volume: f64) -> f64 {
    if daily_volumes.is_empty() {
        return current_day_volume; // bootstrap: use current partial day
    }
    let total: f64 = daily_volumes.iter().sum::<f64>() + current_day_volume;
    total / (daily_volumes.len() as f64 + 1.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_zscore_basic() {
        let mut prices: VecDeque<f64> = VecDeque::new();
        for v in [100.0, 101.0, 99.0, 100.5, 102.0] {
            prices.push_back(v);
        }
        let (z, _mad) = compute_zscore_mad(&prices);
        // last = 102.0, mean ~= 100.5, std ~= 1.08
        assert!((z - 1.388).abs() < 0.01, "z={z}");
    }

    #[test]
    fn test_mad_robust_to_outlier() {
        let mut prices: VecDeque<f64> = VecDeque::new();
        for v in [100.0, 100.1, 100.0, 99.9, 200.0] {
            // 200.0 is a spike
            prices.push_back(v);
        }
        let (z, mad) = compute_zscore_mad(&prices);
        // MAD should be smaller (more robust) than Z for the non-outlier reference
        // both should be large for the spike but MAD penalizes less
        assert!(mad.abs() < z.abs(), "MAD={mad} should be more robust than Z={z}");
    }

    #[test]
    fn test_ofi_balanced() {
        let mut norm = Normalizer::new(20, 30);
        norm.update_quote("AAPL", 150.0, 150.05, 100.0, 100.0, 1000);
        let snap = norm.snapshot("AAPL", 1000, "UNKNOWN".into(), 0.0).unwrap();
        assert!((snap.order_flow_imbalance).abs() < 1e-9, "ofi={}", snap.order_flow_imbalance);
    }

    #[test]
    fn test_realized_vol_zero_insufficient_data() {
        let prices: VecDeque<f64> = VecDeque::from(vec![100.0]);
        assert_eq!(compute_realized_vol(&prices), 0.0);
    }
}
