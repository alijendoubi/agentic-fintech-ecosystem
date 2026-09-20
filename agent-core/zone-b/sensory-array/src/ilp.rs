//! InfluxDB Line Protocol encoding for QuestDB (`market_data` table).
//!
//! Schema follows docs/specs/phase_1_sensory_array.md:
//!
//! ```text
//! market_data,symbol=<TAG> ingestion_ts=<ns>i,exchange_ts=<ns>i,mid_price=..,
//!   bid_price=..,ask_price=..,bid_size=..,ask_size=..,spread=..,
//!   last_trade_price=..,last_trade_size=..,z_score=..,mad_score=..,
//!   z_mad_divergence=<t|f>,ofi=..,realized_vol=..,adv_30d=..,is_stale=<t|f>
//!   <ingestion_ts_ns>
//! ```
//!
//! Three additions beyond the spec, all documented:
//! * `regime_confidence`: HMM posterior attached to the row (kept from v1).
//! * `warmup` (bool): Z/MAD/vol not yet meaningful.
//! * `order_flow_imbalance`: transitional alias of `ofi`. The Zone A regime
//!   detector (`zone-a/regime-detector/hmm.py`) still queries that column name;
//!   drop the alias once it reads `ofi`.
//!
//! The ticker is an ILP *tag* and originates from an untrusted feed. It is
//! (1) validated against a strict grammar and (2) escaped; a line is never
//! emitted for an invalid ticker, so a hostile ticker cannot inject lines.
//! Non-finite floats would be invalid ILP and are rejected outright.

use std::borrow::Cow;
use std::fmt::Write as _;

use crate::normalizer::MarketSnapshot;
use crate::validate::is_valid_ticker;

pub const TABLE: &str = "market_data";

#[derive(Debug, PartialEq, Eq, thiserror::Error)]
pub enum IlpError {
    #[error("invalid ticker for ILP tag")]
    InvalidSymbol,
    #[error("non-finite value in field '{0}'")]
    NonFinite(&'static str),
    #[error("invalid timestamp")]
    InvalidTimestamp,
    #[error("formatting failure")]
    Format,
}

impl From<std::fmt::Error> for IlpError {
    fn from(_: std::fmt::Error) -> Self {
        IlpError::Format
    }
}

/// Escape an ILP tag value: space, comma, equals, CR, LF and backslash are
/// each prefixed with a backslash (QuestDB ILP escaping rules).
pub fn escape_tag(s: &str) -> Cow<'_, str> {
    if !s.chars().any(needs_escape) {
        return Cow::Borrowed(s);
    }
    let mut out = String::with_capacity(s.len() + 4);
    for c in s.chars() {
        if needs_escape(c) {
            out.push('\\');
        }
        out.push(c);
    }
    Cow::Owned(out)
}

fn needs_escape(c: char) -> bool {
    matches!(c, ' ' | ',' | '=' | '\n' | '\r' | '\\')
}

/// Build one newline-terminated ILP line.
pub fn build_ilp_line(snap: &MarketSnapshot) -> Result<String, IlpError> {
    if !is_valid_ticker(&snap.symbol) {
        return Err(IlpError::InvalidSymbol);
    }
    if snap.ingestion_ts_ns <= 0 || snap.exchange_ts_ns <= 0 {
        return Err(IlpError::InvalidTimestamp);
    }
    let floats: [(&'static str, f64); 14] = [
        ("mid_price", snap.mid_price),
        ("bid_price", snap.bid_price),
        ("ask_price", snap.ask_price),
        ("bid_size", snap.bid_size),
        ("ask_size", snap.ask_size),
        ("spread", snap.spread),
        ("last_trade_price", snap.last_trade_price),
        ("last_trade_size", snap.last_trade_size),
        ("z_score", snap.z_score),
        ("mad_score", snap.mad_score),
        ("ofi", snap.order_flow_imbalance),
        ("realized_vol", snap.realized_volatility),
        ("adv_30d", snap.adv_30d),
        ("regime_confidence", snap.regime_confidence),
    ];
    if let Some((name, _)) = floats.iter().find(|(_, v)| !v.is_finite()) {
        return Err(IlpError::NonFinite(name));
    }
    write_line(snap)
}

fn tf(b: bool) -> &'static str {
    if b {
        "t"
    } else {
        "f"
    }
}

fn write_line(s: &MarketSnapshot) -> Result<String, IlpError> {
    let mut l = String::with_capacity(420);
    write!(l, "{TABLE},symbol={} ", escape_tag(&s.symbol))?;
    write!(
        l,
        "ingestion_ts={}i,exchange_ts={}i,",
        s.ingestion_ts_ns, s.exchange_ts_ns
    )?;
    write!(
        l,
        "mid_price={:.6},bid_price={:.6},ask_price={:.6},bid_size={:.2},ask_size={:.2},spread={:.6},",
        s.mid_price, s.bid_price, s.ask_price, s.bid_size, s.ask_size, s.spread
    )?;
    write!(
        l,
        "last_trade_price={:.6},last_trade_size={:.2},z_score={:.6},mad_score={:.6},z_mad_divergence={},",
        s.last_trade_price,
        s.last_trade_size,
        s.z_score,
        s.mad_score,
        tf(s.z_mad_divergence)
    )?;
    // `order_flow_imbalance` is the transitional alias (see module docs).
    write!(
        l,
        "ofi={:.6},order_flow_imbalance={:.6},realized_vol={:.6},adv_30d={:.2},",
        s.order_flow_imbalance, s.order_flow_imbalance, s.realized_volatility, s.adv_30d
    )?;
    writeln!(
        l,
        "regime_confidence={:.4},warmup={},is_stale={} {}",
        s.regime_confidence,
        tf(s.warmup),
        tf(s.is_stale),
        s.ingestion_ts_ns
    )?;
    Ok(l)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_snap() -> MarketSnapshot {
        MarketSnapshot {
            symbol: "AAPL".into(),
            ingestion_ts_ns: 1_700_000_000_000_000_123,
            exchange_ts_ns: 1_700_000_000_000_000_000,
            l2_recv_ts_ns: 1_700_000_000_000_000_100,
            mid_price: 150.025,
            bid_price: 150.0,
            ask_price: 150.05,
            bid_size: 200.0,
            ask_size: 300.0,
            spread: 0.05,
            last_trade_price: 150.02,
            last_trade_size: 100.0,
            last_trade_ts_ns: 1_700_000_000_000_000_000,
            trade_recv_ts_ns: 1_700_000_000_000_000_050,
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

    #[test]
    fn line_matches_spec_schema_exactly() {
        let line = build_ilp_line(&make_snap()).expect("valid");
        let expected = "market_data,symbol=AAPL \
ingestion_ts=1700000000000000123i,exchange_ts=1700000000000000000i,\
mid_price=150.025000,bid_price=150.000000,ask_price=150.050000,bid_size=200.00,ask_size=300.00,spread=0.050000,\
last_trade_price=150.020000,last_trade_size=100.00,z_score=1.230000,mad_score=0.980000,z_mad_divergence=f,\
ofi=-0.200000,order_flow_imbalance=-0.200000,realized_vol=0.250000,adv_30d=50000000.00,\
regime_confidence=0.8700,warmup=f,is_stale=f 1700000000000000123\n";
        assert_eq!(line, expected);
    }

    #[test]
    fn has_all_spec_field_names() {
        let line = build_ilp_line(&make_snap()).expect("valid");
        for f in [
            "ingestion_ts=",
            "exchange_ts=",
            "mid_price=",
            "bid_price=",
            "ask_price=",
            "bid_size=",
            "ask_size=",
            "spread=",
            "last_trade_price=",
            "last_trade_size=",
            "z_score=",
            "mad_score=",
            "z_mad_divergence=",
            "ofi=",
            "realized_vol=",
            "adv_30d=",
            "is_stale=",
        ] {
            assert!(line.contains(f), "missing {f}");
        }
    }

    #[test]
    fn stale_and_divergence_flags() {
        let mut s = make_snap();
        s.is_stale = true;
        s.z_mad_divergence = true;
        let line = build_ilp_line(&s).expect("valid");
        assert!(line.contains("is_stale=t"));
        assert!(line.contains("z_mad_divergence=t"));
    }

    #[test]
    fn timestamp_is_ingestion_ts_and_line_is_terminated() {
        let line = build_ilp_line(&make_snap()).expect("valid");
        assert!(line.ends_with(" 1700000000000000123\n"));
        assert_eq!(line.matches('\n').count(), 1);
    }

    #[test]
    fn escape_tag_covers_every_special_character() {
        assert_eq!(escape_tag("AAPL"), "AAPL");
        assert!(matches!(escape_tag("AAPL"), Cow::Borrowed(_)));
        assert_eq!(escape_tag("A B"), "A\\ B");
        assert_eq!(escape_tag("A,B"), "A\\,B");
        assert_eq!(escape_tag("A=B"), "A\\=B");
        assert_eq!(escape_tag("A\\B"), "A\\\\B");
        assert_eq!(escape_tag("A\nB"), "A\\\nB");
        assert_eq!(escape_tag("A\rB"), "A\\\rB");
    }

    #[test]
    fn hostile_tickers_cannot_inject_lines() {
        for evil in [
            "AAPL\nmarket_data,symbol=EVIL mid_price=1 1",
            "AAPL,symbol=EVIL",
            "AAPL mid_price=1",
            "AAPL\r\n",
            "A=B",
            "AAPL\\",
            "",
            "aapl",
        ] {
            let mut s = make_snap();
            s.symbol = evil.to_string();
            assert_eq!(build_ilp_line(&s), Err(IlpError::InvalidSymbol), "{evil:?}");
        }
    }

    #[test]
    fn non_finite_values_are_rejected_not_written() {
        for (i, v) in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY]
            .into_iter()
            .enumerate()
        {
            let mut s = make_snap();
            match i % 3 {
                0 => s.z_score = v,
                1 => s.mid_price = v,
                _ => s.regime_confidence = v,
            }
            assert!(
                matches!(build_ilp_line(&s), Err(IlpError::NonFinite(_))),
                "{v}"
            );
        }
        let mut s = make_snap();
        s.realized_volatility = f64::NAN;
        assert_eq!(build_ilp_line(&s), Err(IlpError::NonFinite("realized_vol")));
    }

    #[test]
    fn invalid_timestamps_are_rejected() {
        let mut s = make_snap();
        s.ingestion_ts_ns = 0;
        assert_eq!(build_ilp_line(&s), Err(IlpError::InvalidTimestamp));
        let mut s = make_snap();
        s.exchange_ts_ns = -1;
        assert_eq!(build_ilp_line(&s), Err(IlpError::InvalidTimestamp));
    }
}
