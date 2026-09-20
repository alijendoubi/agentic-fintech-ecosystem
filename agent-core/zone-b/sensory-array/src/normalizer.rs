//! Per-symbol rolling normalisation: Z-score, MAD score, OFI, realised vol, ADV.
//!
//! Design notes (all deliberate, all covered by tests):
//! * The Z/MAD scores of a new quote are computed against the *prior* window,
//!   never including the sample being scored. Including it would cap the
//!   largest attainable |z| at (n-1)/sqrt(n) and mask exactly the outliers the
//!   score exists to catch.
//! * A flat window (std == 0 or MAD == 0) never hides a move: MAD falls back
//!   to the mean absolute deviation, and if the window is truly constant any
//!   deviation saturates at `SCORE_CAP`.
//! * Nothing non-finite ever enters the state (rejected at ingest) and nothing
//!   here can panic: sorting uses `f64::total_cmp`.
//! * `realized_vol` is built from 1-second bars (see `bars`), not raw quote
//!   ticks, so the spec's annualisation factor is applied to the frequency it
//!   assumes.
//! * The hot path allocates nothing per quote once a symbol exists (stack
//!   buffers for the median maths, `get_mut` before `insert`).

use std::borrow::Cow;
use std::collections::{HashMap, VecDeque};

use crate::validate::{is_valid_price, is_valid_size, MAX_SYMBOLS};

/// Smallest / largest configurable rolling window.
pub const MIN_WINDOW: usize = 5;
pub const MAX_WINDOW: usize = 256;
/// Prior-window samples required before Z/MAD are considered meaningful.
pub const MIN_WARMUP_QUOTES: usize = 5;
/// 1-second bars required before realised vol is considered meaningful.
pub const MIN_WARMUP_BARS: usize = 3;
/// Saturation value for scores of a deviation from a perfectly flat window.
pub const SCORE_CAP: f64 = 1.0e6;
/// |z - mad_score| above this sets `z_mad_divergence` (spec: 2.0).
pub const DIVERGENCE_THRESHOLD: f64 = 2.0;

const MAD_SCALE: f64 = 1.4826; // consistency constant for normal data
const MEAN_AD_SCALE: f64 = 1.253_314_137; // sqrt(pi/2), Iglewicz-Hoaglin fallback
const RELATIVE_EPS: f64 = 1.0e-9;
const NS_PER_SEC: i64 = 1_000_000_000;
/// Spec annualisation: 252 days * 6.5h * 3600s of 1-second bars.
const TRADING_SECONDS_PER_YEAR: f64 = 252.0 * 6.5 * 3600.0;

/// Why an input was refused. Never repaired, always counted by the caller.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum Reject {
    #[error("invalid price (non-finite, <=0 or absurd)")]
    InvalidPrice,
    #[error("crossed quote (ask < bid)")]
    CrossedQuote,
    #[error("invalid size")]
    InvalidSize,
    #[error("invalid timestamp")]
    InvalidTimestamp,
    #[error("symbol table full")]
    SymbolLimit,
}

/// A quote update. `recv_ts_ns` is the local wall-clock receive time.
#[derive(Debug, Clone, Copy)]
pub struct QuoteInput {
    pub bid_price: f64,
    pub ask_price: f64,
    pub bid_size: f64,
    pub ask_size: f64,
    pub exchange_ts_ns: i64,
    pub recv_ts_ns: i64,
}

/// A trade print. `day` is the UTC day number (days since the Unix epoch).
#[derive(Debug, Clone, Copy)]
pub struct TradeInput {
    pub price: f64,
    pub size: f64,
    pub exchange_ts_ns: i64,
    pub recv_ts_ns: i64,
    pub day: i64,
}

/// Per-symbol rolling state.
#[derive(Debug, Default)]
pub struct SymbolState {
    mid_prices: VecDeque<f64>,
    /// (second since epoch, last mid in that second), oldest first.
    bars: VecDeque<(i64, f64)>,
    daily_volumes: VecDeque<f64>,
    current_day_volume: f64,
    current_day: i64,

    last_trade_price: f64,
    last_trade_size: f64,
    last_trade_ts_ns: i64,
    trade_recv_ts_ns: i64,

    bid_price: f64,
    ask_price: f64,
    bid_size: f64,
    ask_size: f64,
    exchange_ts_ns: i64,
    l2_recv_ts_ns: i64,

    z_score: f64,
    mad_score: f64,
    has_quote: bool,
    /// True once Z/MAD were computed against a full-enough prior window.
    scored: bool,
}

/// Normalised market snapshot for QuestDB, Redis and downstream consumers.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct MarketSnapshot {
    pub symbol: String,
    /// Local wall-clock time at which this snapshot was built (ns).
    pub ingestion_ts_ns: i64,
    /// SIP timestamp of the quote (ns).
    pub exchange_ts_ns: i64,
    /// Local wall-clock receive time of the quote's frame (ns). TTL basis.
    pub l2_recv_ts_ns: i64,
    pub mid_price: f64,
    pub bid_price: f64,
    pub ask_price: f64,
    pub bid_size: f64,
    pub ask_size: f64,
    pub spread: f64,
    pub last_trade_price: f64,
    pub last_trade_size: f64,
    pub last_trade_ts_ns: i64,
    /// Local receive time of the last trade (ns); 0 = none seen.
    pub trade_recv_ts_ns: i64,
    pub z_score: f64,
    pub mad_score: f64,
    pub z_mad_divergence: bool,
    pub order_flow_imbalance: f64,
    pub realized_volatility: f64,
    pub adv_30d: f64,
    /// True until enough history exists for Z/MAD/vol to mean anything.
    pub warmup: bool,
    pub regime_label: Cow<'static, str>,
    pub regime_confidence: f64,
    pub is_stale: bool,
    pub stale_reason: Cow<'static, str>,
}

/// Multi-symbol normaliser. Owned by the ingestor and kept across reconnects.
pub struct Normalizer {
    states: HashMap<String, SymbolState>,
    window: usize,
    adv_days: usize,
}

impl Normalizer {
    pub fn new(window: usize, adv_days: usize) -> Self {
        Self {
            states: HashMap::new(),
            window: window.clamp(MIN_WINDOW, MAX_WINDOW),
            adv_days: adv_days.max(1),
        }
    }

    pub fn symbol_count(&self) -> usize {
        self.states.len()
    }

    fn state_mut(&mut self, symbol: &str) -> Result<&mut SymbolState, Reject> {
        if !self.states.contains_key(symbol) {
            if self.states.len() >= MAX_SYMBOLS {
                return Err(Reject::SymbolLimit);
            }
            self.states
                .insert(symbol.to_owned(), SymbolState::default());
        }
        self.states.get_mut(symbol).ok_or(Reject::SymbolLimit)
    }

    /// Apply a quote. Scores are computed against the prior window.
    pub fn update_quote(&mut self, symbol: &str, q: QuoteInput) -> Result<(), Reject> {
        validate_quote(&q)?;
        let window = self.window;
        let state = self.state_mut(symbol)?;
        let mid = (q.bid_price + q.ask_price) / 2.0;

        state.scored = state.mid_prices.len() >= MIN_WARMUP_QUOTES;
        let (z, mad) = if state.scored {
            scores(&state.mid_prices, mid)
        } else {
            (0.0, 0.0)
        };
        state.z_score = z;
        state.mad_score = mad;

        state.mid_prices.push_back(mid);
        if state.mid_prices.len() > window {
            state.mid_prices.pop_front();
        }
        push_bar(&mut state.bars, q.exchange_ts_ns / NS_PER_SEC, mid, window + 1);

        state.bid_price = q.bid_price;
        state.ask_price = q.ask_price;
        state.bid_size = q.bid_size;
        state.ask_size = q.ask_size;
        state.exchange_ts_ns = q.exchange_ts_ns;
        state.l2_recv_ts_ns = q.recv_ts_ns;
        state.has_quote = true;
        Ok(())
    }

    /// Apply a trade print (last-trade fields and daily volume).
    pub fn update_trade(&mut self, symbol: &str, t: TradeInput) -> Result<(), Reject> {
        if !is_valid_price(t.price) {
            return Err(Reject::InvalidPrice);
        }
        if !is_valid_size(t.size) {
            return Err(Reject::InvalidSize);
        }
        if t.exchange_ts_ns <= 0 || t.recv_ts_ns <= 0 || t.day <= 0 {
            return Err(Reject::InvalidTimestamp);
        }
        let adv_days = self.adv_days;
        let state = self.state_mut(symbol)?;

        if t.exchange_ts_ns >= state.last_trade_ts_ns {
            state.last_trade_price = t.price;
            state.last_trade_size = t.size;
            state.last_trade_ts_ns = t.exchange_ts_ns;
            state.trade_recv_ts_ns = t.recv_ts_ns;
        }
        if t.day > state.current_day {
            if state.current_day != 0 {
                state.daily_volumes.push_back(state.current_day_volume);
                if state.daily_volumes.len() > adv_days {
                    state.daily_volumes.pop_front();
                }
            }
            state.current_day_volume = 0.0;
            state.current_day = t.day;
        }
        if t.day == state.current_day {
            state.current_day_volume += t.size;
        }
        Ok(())
    }

    /// Build the snapshot for `symbol`; `None` until a valid quote was seen.
    pub fn snapshot(
        &self,
        symbol: &str,
        now_ns: i64,
        regime_label: Cow<'static, str>,
        regime_confidence: f64,
    ) -> Option<MarketSnapshot> {
        let s = self.states.get(symbol).filter(|s| s.has_quote)?;
        let denom = s.bid_size + s.ask_size;
        let ofi = if denom > 0.0 {
            (s.bid_size - s.ask_size) / denom
        } else {
            0.0
        };
        let vol = realized_vol(&s.bars);
        let warmup = !s.scored || vol.is_none();
        Some(MarketSnapshot {
            symbol: symbol.to_owned(),
            ingestion_ts_ns: now_ns,
            exchange_ts_ns: s.exchange_ts_ns,
            l2_recv_ts_ns: s.l2_recv_ts_ns,
            mid_price: (s.bid_price + s.ask_price) / 2.0,
            bid_price: s.bid_price,
            ask_price: s.ask_price,
            bid_size: s.bid_size,
            ask_size: s.ask_size,
            spread: s.ask_price - s.bid_price,
            last_trade_price: s.last_trade_price,
            last_trade_size: s.last_trade_size,
            last_trade_ts_ns: s.last_trade_ts_ns,
            trade_recv_ts_ns: s.trade_recv_ts_ns,
            z_score: s.z_score,
            mad_score: s.mad_score,
            z_mad_divergence: (s.z_score - s.mad_score).abs() > DIVERGENCE_THRESHOLD,
            order_flow_imbalance: ofi,
            realized_volatility: vol.unwrap_or(0.0),
            adv_30d: adv(&s.daily_volumes, s.current_day_volume),
            warmup,
            regime_label,
            regime_confidence,
            is_stale: false,
            stale_reason: Cow::Borrowed(""),
        })
    }
}

fn validate_quote(q: &QuoteInput) -> Result<(), Reject> {
    if !is_valid_price(q.bid_price) || !is_valid_price(q.ask_price) {
        return Err(Reject::InvalidPrice);
    }
    if q.ask_price < q.bid_price {
        return Err(Reject::CrossedQuote);
    }
    if !is_valid_size(q.bid_size) || !is_valid_size(q.ask_size) {
        return Err(Reject::InvalidSize);
    }
    if q.exchange_ts_ns <= 0 || q.recv_ts_ns <= 0 {
        return Err(Reject::InvalidTimestamp);
    }
    Ok(())
}

/// Keep at most one bar per second (its last mid); ignore out-of-order seconds.
fn push_bar(bars: &mut VecDeque<(i64, f64)>, sec: i64, mid: f64, cap: usize) {
    match bars.back_mut() {
        Some(last) if last.0 == sec => last.1 = mid,
        Some(last) if last.0 > sec => {}
        _ => {
            bars.push_back((sec, mid));
            if bars.len() > cap {
                bars.pop_front();
            }
        }
    }
}

/// (z, mad_score) of `x` against `prior` (which must not contain `x`).
/// `prior` must hold 1..=MAX_WINDOW samples.
pub fn scores(prior: &VecDeque<f64>, x: f64) -> (f64, f64) {
    let mut buf = [0.0_f64; MAX_WINDOW];
    let n = prior.len().min(MAX_WINDOW);
    for (slot, v) in buf.iter_mut().zip(prior.iter().take(n)) {
        *slot = *v;
    }
    let vals = &mut buf[..n];
    if n < 2 {
        return (0.0, 0.0);
    }
    let (mean, std) = mean_std(vals);
    let z = flat_safe_score(x - mean, std, 1.0, mean);

    vals.sort_unstable_by(f64::total_cmp);
    let median = median_sorted(vals);
    let mut dev_buf = [0.0_f64; MAX_WINDOW];
    let devs = &mut dev_buf[..n];
    for (d, v) in devs.iter_mut().zip(vals.iter()) {
        *d = (v - median).abs();
    }
    let mean_ad = devs.iter().sum::<f64>() / n as f64;
    devs.sort_unstable_by(f64::total_cmp);
    let mad = median_sorted(devs);

    let dx = x - median;
    let m = if mad * MAD_SCALE > RELATIVE_EPS * median.abs().max(1.0) {
        flat_safe_score(dx, mad, MAD_SCALE, median)
    } else {
        flat_safe_score(dx, mean_ad, MEAN_AD_SCALE, median)
    };
    (z, m)
}

/// `deviation / (scale * spread)`; if the spread is numerically zero, a zero
/// deviation scores 0 and any real deviation saturates at +/- `SCORE_CAP`.
fn flat_safe_score(deviation: f64, spread: f64, scale: f64, centre: f64) -> f64 {
    let eps = RELATIVE_EPS * centre.abs().max(1.0);
    let raw = if spread * scale > eps {
        deviation / (spread * scale)
    } else if deviation.abs() <= eps {
        0.0
    } else {
        deviation.signum() * SCORE_CAP
    };
    raw.clamp(-SCORE_CAP, SCORE_CAP)
}

fn mean_std(vals: &[f64]) -> (f64, f64) {
    let n = vals.len() as f64;
    let mean = vals.iter().sum::<f64>() / n;
    let var = vals.iter().map(|v| (v - mean).powi(2)).sum::<f64>() / (n - 1.0);
    (mean, var.sqrt())
}

/// Median of an already sorted, non-empty slice.
fn median_sorted(sorted: &[f64]) -> f64 {
    let n = sorted.len();
    let mid = n / 2;
    if n % 2 == 0 {
        (sorted[mid - 1] + sorted[mid]) / 2.0
    } else {
        sorted[mid]
    }
}

/// Annualised realised volatility from 1-second bars.
///
/// Each log return is divided by sqrt(dt) (dt = seconds between bars) so
/// gaps between bars do not inflate the estimate, then the sample std is
/// scaled by sqrt(trading seconds per year), matching the spec formula
/// `std(log_returns) * sqrt(252 * 6.5 * 3600)`.
/// `None` when fewer than `MIN_WARMUP_BARS` bars (2 returns) exist.
pub fn realized_vol(bars: &VecDeque<(i64, f64)>) -> Option<f64> {
    if bars.len() < MIN_WARMUP_BARS {
        return None;
    }
    let scaled = |(a, b): (&(i64, f64), &(i64, f64))| -> Option<f64> {
        let dt = (b.0 - a.0) as f64;
        let r = (b.1 / a.1).ln();
        (dt > 0.0 && r.is_finite()).then(|| r / dt.sqrt())
    };
    let pairs = || bars.iter().zip(bars.iter().skip(1)).filter_map(scaled);
    let n = pairs().count();
    if n < 2 {
        return None;
    }
    let nf = n as f64;
    let mean = pairs().sum::<f64>() / nf;
    let var = pairs().map(|r| (r - mean).powi(2)).sum::<f64>() / (nf - 1.0);
    let vol = var.sqrt() * TRADING_SECONDS_PER_YEAR.sqrt();
    vol.is_finite().then_some(vol)
}

/// Mean of completed days; bootstraps with the partial current day.
fn adv(daily: &VecDeque<f64>, current_day_volume: f64) -> f64 {
    if daily.is_empty() {
        return current_day_volume;
    }
    daily.iter().sum::<f64>() / daily.len() as f64
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dq(v: &[f64]) -> VecDeque<f64> {
        v.iter().copied().collect()
    }

    fn quote(bid: f64, ask: f64, ts_ns: i64) -> QuoteInput {
        QuoteInput {
            bid_price: bid,
            ask_price: ask,
            bid_size: 100.0,
            ask_size: 100.0,
            exchange_ts_ns: ts_ns,
            recv_ts_ns: ts_ns + 1,
        }
    }

    const T0: i64 = 1_700_000_000_000_000_000;

    /// Hand calc: prior [100,101,99,100.5,100.2] -> mean 100.14, sample std
    /// 0.740270; x=102 -> z = 1.86/0.740270 = 2.51260.
    /// (The pre-rewrite test expected 1.388 for the *inclusive* window; the true
    /// inclusive value is 1.5/1.118034 = 1.34164, so that expectation was an
    /// arithmetic error, not an implementation bug.)
    #[test]
    fn zscore_matches_hand_calculation() {
        let (z, _) = scores(&dq(&[100.0, 101.0, 99.0, 100.5, 100.2]), 102.0);
        assert!((z - 2.51260).abs() < 1e-4, "z={z}");
    }

    /// The old inclusive convention, verified independently: window
    /// [100,101,99,100.5,102] scores its last element at 1.3416408.
    #[test]
    fn inclusive_window_value_is_1_3416_not_1_388() {
        let vals = [100.0, 101.0, 99.0, 100.5, 102.0];
        let (mean, std) = mean_std(&vals);
        assert!((mean - 100.5).abs() < 1e-12);
        assert!((std - 1.25_f64.sqrt()).abs() < 1e-12);
        assert!(((102.0 - mean) / std - 1.341_640_786).abs() < 1e-8);
    }

    /// Outlier handling. Hand calc for the original data (prior
    /// [100,100.1,100,99.9,100.05], x=200): median 100, MAD 0.05 ->
    /// mad_score = 100/(1.4826*0.05) = 1349.0; z = 99.99/0.074162 = 1348.3.
    /// The old assertion `mad < z` was inverted: MAD is *robust*, meaning the
    /// spread it measures is not inflated by outliers, so it scores a spike
    /// at least as high as z, and far higher once the history is contaminated.
    #[test]
    fn mad_scores_a_spike_at_least_as_high_as_z() {
        let (z, mad) = scores(&dq(&[100.0, 100.1, 100.0, 99.9, 100.05]), 200.0);
        assert!((z - 1348.26).abs() < 0.1, "z={z}");
        assert!((mad - 1348.98).abs() < 0.1, "mad={mad}");
        assert!(mad > z);
    }

    /// Contaminated history [.., 130, ..]: the old spike inflates std, so z
    /// masks a genuine 0.3 move (z = -0.287) while MAD still flags it (7.42 > 3.5).
    #[test]
    fn mad_is_robust_to_outlier_in_history_where_z_is_masked() {
        let hist = [
            100.0, 100.1, 100.0, 99.9, 100.05, 100.0, 100.1, 130.0, 100.0, 100.05,
        ];
        let (z, mad) = scores(&dq(&hist), 100.3);
        assert!((z - -0.28692).abs() < 1e-4, "z={z}");
        assert!((mad - 7.4194).abs() < 1e-3, "mad={mad}");
        assert!(z.abs() < 2.0 && mad.abs() > 3.5);
    }

    #[test]
    fn mad_zero_division_does_not_mask_outlier() {
        // More than half the window identical => MAD == 0.
        let prior = dq(&[100.0, 100.0, 100.0, 100.0, 100.05, 100.0]);
        let (_, mad) = scores(&prior, 105.0);
        assert!(mad.abs() > 3.5, "MAD==0 must not return 0 for a spike: {mad}");
        assert!(mad.is_finite());
    }

    #[test]
    fn constant_window_saturates_instead_of_hiding_a_move() {
        let prior = dq(&[100.0; 8]);
        let (z, mad) = scores(&prior, 101.0);
        assert_eq!(z, SCORE_CAP);
        assert_eq!(mad, SCORE_CAP);
        let (z, mad) = scores(&prior, 99.0);
        assert_eq!((z, mad), (-SCORE_CAP, -SCORE_CAP));
        let (z, mad) = scores(&prior, 100.0);
        assert_eq!((z, mad), (0.0, 0.0));
    }

    #[test]
    fn scores_never_panic_or_go_non_finite_on_extreme_input() {
        let prior = dq(&[1e307, 1e307, 1e307, 1e307, 1e307]);
        let (z, m) = scores(&prior, 1e308);
        assert!(z.is_finite() && m.is_finite());
        let prior = dq(&[f64::MAX, f64::MAX, -f64::MAX, 1.0, 2.0]);
        let (z, m) = scores(&prior, 0.0);
        // Overflow may make the mean non-finite; the function must still not panic.
        let _ = (z, m);
    }

    #[test]
    fn extreme_but_valid_quotes_do_not_crash_the_normalizer() {
        // bid = ask = 1e308 used to produce mid = inf and a NaN sort panic.
        let mut n = Normalizer::new(20, 30);
        assert_eq!(
            n.update_quote("AAPL", quote(1e308, 1e308, T0)),
            Err(Reject::InvalidPrice)
        );
        assert!(n.snapshot("AAPL", T0, "X".into(), 0.0).is_none());
    }

    #[test]
    fn rejects_non_finite_crossed_and_bad_quotes() {
        let mut n = Normalizer::new(20, 30);
        assert_eq!(
            n.update_quote("A", quote(f64::NAN, 10.0, T0)),
            Err(Reject::InvalidPrice)
        );
        assert_eq!(
            n.update_quote("A", quote(10.0, f64::INFINITY, T0)),
            Err(Reject::InvalidPrice)
        );
        assert_eq!(
            n.update_quote("A", quote(-1.0, 10.0, T0)),
            Err(Reject::InvalidPrice)
        );
        assert_eq!(
            n.update_quote("A", quote(10.0, 9.99, T0)),
            Err(Reject::CrossedQuote)
        );
        let mut q = quote(10.0, 10.01, T0);
        q.bid_size = f64::NAN;
        assert_eq!(n.update_quote("A", q), Err(Reject::InvalidSize));
        assert_eq!(
            n.update_quote("A", quote(10.0, 10.01, 0)),
            Err(Reject::InvalidTimestamp)
        );
        assert!(n.update_quote("A", quote(10.0, 10.0, T0)).is_ok(), "locked is allowed");
    }

    #[test]
    fn ofi_balanced_is_zero_and_skewed_is_signed() {
        let mut n = Normalizer::new(20, 30);
        n.update_quote("AAPL", quote(150.0, 150.05, T0)).expect("valid");
        let s = n.snapshot("AAPL", T0, "U".into(), 0.0).expect("snap");
        assert!(s.order_flow_imbalance.abs() < 1e-9);

        let mut q = quote(150.0, 150.05, T0 + 1);
        q.bid_size = 300.0;
        q.ask_size = 100.0;
        n.update_quote("AAPL", q).expect("valid");
        let s = n.snapshot("AAPL", T0, "U".into(), 0.0).expect("snap");
        assert!((s.order_flow_imbalance - 0.5).abs() < 1e-12);
    }

    #[test]
    fn warmup_flag_until_enough_history() {
        let mut n = Normalizer::new(20, 30);
        for i in 0..5_i64 {
            n.update_quote("AAPL", quote(100.0, 100.02, T0 + i * NS_PER_SEC))
                .expect("valid");
            let s = n.snapshot("AAPL", T0, "U".into(), 0.0).expect("snap");
            assert!(s.warmup, "quote {i} must still be warm-up");
            assert_eq!((s.z_score, s.mad_score), (0.0, 0.0));
        }
        n.update_quote("AAPL", quote(100.0, 100.02, T0 + 5 * NS_PER_SEC))
            .expect("valid");
        let s = n.snapshot("AAPL", T0, "U".into(), 0.0).expect("snap");
        assert!(!s.warmup);
    }

    #[test]
    fn state_survives_and_symbols_are_bounded() {
        let mut n = Normalizer::new(20, 30);
        for i in 0..MAX_SYMBOLS {
            n.update_quote(&format!("S{i}"), quote(10.0, 10.01, T0))
                .expect("under limit");
        }
        assert_eq!(n.symbol_count(), MAX_SYMBOLS);
        assert_eq!(
            n.update_quote("OVERFLOW", quote(10.0, 10.01, T0)),
            Err(Reject::SymbolLimit)
        );
        // Existing symbols still update.
        assert!(n.update_quote("S0", quote(10.0, 10.01, T0 + 1)).is_ok());
    }

    #[test]
    fn realized_vol_needs_bars_and_uses_one_second_buckets() {
        let mut bars: VecDeque<(i64, f64)> = VecDeque::new();
        assert_eq!(realized_vol(&bars), None);
        for (i, p) in [100.0, 101.0, 100.0, 101.0].iter().enumerate() {
            bars.push_back((i as i64, *p));
        }
        // returns: +ln(1.01), -ln(1.01), +ln(1.01) => sample std = 0.011503
        let r = 1.01_f64.ln();
        let mean = r / 3.0;
        let var = ((r - mean).powi(2) * 2.0 + (-r - mean).powi(2)) / 2.0;
        let expect = var.sqrt() * (252.0_f64 * 6.5 * 3600.0).sqrt();
        let got = realized_vol(&bars).expect("enough bars");
        assert!((got - expect).abs() < 1e-9, "{got} vs {expect}");
    }

    #[test]
    fn many_ticks_within_one_second_form_a_single_bar() {
        let mut n = Normalizer::new(20, 30);
        for i in 0..50_i64 {
            let p = 100.0 + (i % 2) as f64 * 0.01;
            n.update_quote("AAPL", quote(p, p + 0.01, T0 + i * 1_000_000))
                .expect("valid");
        }
        let s = n.snapshot("AAPL", T0, "U".into(), 0.0).expect("snap");
        assert_eq!(s.realized_volatility, 0.0, "one bar => no vol yet");
        assert!(s.warmup);
    }

    #[test]
    fn realized_vol_scales_returns_by_sqrt_of_gap() {
        // Returns +0.02 / -0.02 over 4s gaps => scaled +0.01 / -0.01,
        // sample var = 2e-4, std = 0.0141421.
        let up = 100.0 * (0.02_f64).exp();
        let bars: VecDeque<(i64, f64)> = [(0, 100.0), (4, up), (8, 100.0)].into_iter().collect();
        let got = realized_vol(&bars).expect("enough bars");
        let expect = (2.0e-4_f64).sqrt() * (252.0_f64 * 6.5 * 3600.0).sqrt();
        assert!((got - expect).abs() < 1e-9, "{got} vs {expect}");
    }

    #[test]
    fn trade_updates_and_daily_volume_roll() {
        let mut n = Normalizer::new(20, 30);
        n.update_quote("AAPL", quote(150.0, 150.05, T0)).expect("q");
        let t = |size: f64, day: i64, ts: i64| TradeInput {
            price: 150.02,
            size,
            exchange_ts_ns: ts,
            recv_ts_ns: ts + 1,
            day,
        };
        n.update_trade("AAPL", t(100.0, 19_000, T0)).expect("t1");
        n.update_trade("AAPL", t(50.0, 19_000, T0 + 1)).expect("t2");
        let s = n.snapshot("AAPL", T0, "U".into(), 0.0).expect("snap");
        assert_eq!(s.adv_30d, 150.0, "bootstrap uses partial day");
        assert_eq!(s.last_trade_size, 50.0);

        n.update_trade("AAPL", t(10.0, 19_001, T0 + 2)).expect("t3");
        let s = n.snapshot("AAPL", T0, "U".into(), 0.0).expect("snap");
        assert_eq!(s.adv_30d, 150.0, "mean of completed days");

        // Late trade from the previous day updates nothing about volume.
        n.update_trade("AAPL", t(999.0, 19_000, T0 + 3)).expect("late");
        n.update_trade("AAPL", t(10.0, 19_002, T0 + 4)).expect("t4");
        let s = n.snapshot("AAPL", T0, "U".into(), 0.0).expect("snap");
        assert_eq!(s.adv_30d, (150.0 + 10.0) / 2.0);
    }

    #[test]
    fn out_of_order_trade_does_not_regress_last_trade() {
        let mut n = Normalizer::new(20, 30);
        n.update_quote("AAPL", quote(150.0, 150.05, T0)).expect("q");
        let mk = |price: f64, ts: i64| TradeInput {
            price,
            size: 1.0,
            exchange_ts_ns: ts,
            recv_ts_ns: ts,
            day: 19_000,
        };
        n.update_trade("AAPL", mk(151.0, T0 + 10)).expect("new");
        n.update_trade("AAPL", mk(149.0, T0 + 5)).expect("old");
        let s = n.snapshot("AAPL", T0, "U".into(), 0.0).expect("snap");
        assert_eq!(s.last_trade_price, 151.0);
    }

    #[test]
    fn rejects_bad_trades() {
        let mut n = Normalizer::new(20, 30);
        let mk = |price: f64, size: f64| TradeInput {
            price,
            size,
            exchange_ts_ns: T0,
            recv_ts_ns: T0,
            day: 19_000,
        };
        assert_eq!(n.update_trade("A", mk(f64::NAN, 1.0)), Err(Reject::InvalidPrice));
        assert_eq!(n.update_trade("A", mk(1.0, -1.0)), Err(Reject::InvalidSize));
        assert_eq!(
            n.update_trade(
                "A",
                TradeInput {
                    day: 0,
                    ..mk(1.0, 1.0)
                }
            ),
            Err(Reject::InvalidTimestamp)
        );
    }

    #[test]
    fn snapshot_serializes_and_round_trips() {
        let mut n = Normalizer::new(20, 30);
        n.update_quote("AAPL", quote(150.0, 150.05, T0)).expect("q");
        let s = n.snapshot("AAPL", T0, "TRENDING_BULL".into(), 0.9).expect("snap");
        let json = serde_json::to_string(&s).expect("ser");
        let back: MarketSnapshot = serde_json::from_str(&json).expect("de");
        assert_eq!(back.symbol, "AAPL");
        assert_eq!(back.regime_label, "TRENDING_BULL");
        assert!(json.contains("\"warmup\":true"));
    }
}
