use crate::error::{Result, SensoryError};
use crate::normalizer::MarketSnapshot;
use tokio::io::AsyncWriteExt;
use tokio::net::TcpStream;
use tracing::{debug, error, info, warn};

/// Async QuestDB writer using InfluxDB Line Protocol (ILP) over TCP.
///
/// ILP format:
/// ```
/// market_data,symbol=AAPL mid_price=150.02,bid_price=150.00,...,is_stale=false 1234567890000000000\n
/// ```
pub struct QuestDbWriter {
    host: String,
    port: u16,
    stream: Option<TcpStream>,
}

impl QuestDbWriter {
    pub fn new(host: String, port: u16) -> Self {
        Self {
            host,
            port,
            stream: None,
        }
    }

    /// Ensure TCP connection is open; reconnect if needed.
    async fn ensure_connected(&mut self) -> Result<()> {
        if self.stream.is_some() {
            return Ok(());
        }
        let addr = format!("{}:{}", self.host, self.port);
        info!("Connecting to QuestDB ILP at {}", addr);
        let stream = TcpStream::connect(&addr)
            .await
            .map_err(|e| SensoryError::QuestDb(format!("TCP connect to {addr} failed: {e}")))?;
        self.stream = Some(stream);
        info!("QuestDB ILP connected");
        Ok(())
    }

    /// Write a single snapshot as an ILP line.
    pub async fn write(&mut self, snap: &MarketSnapshot) -> Result<()> {
        self.ensure_connected().await?;

        let line = build_ilp_line(snap);
        debug!(symbol = %snap.symbol, "ILP write");

        if let Some(stream) = &mut self.stream {
            if let Err(e) = stream.write_all(line.as_bytes()).await {
                warn!("QuestDB write failed ({}), reconnecting: {}", snap.symbol, e);
                self.stream = None;
                // retry once after reconnect
                self.ensure_connected().await?;
                if let Some(stream) = &mut self.stream {
                    stream
                        .write_all(line.as_bytes())
                        .await
                        .map_err(|e| SensoryError::QuestDb(format!("Retry write failed: {e}")))?;
                }
            }
        }
        Ok(())
    }

    /// Flush the underlying TCP stream.
    pub async fn flush(&mut self) -> Result<()> {
        if let Some(stream) = &mut self.stream {
            stream
                .flush()
                .await
                .map_err(|e| SensoryError::QuestDb(format!("Flush failed: {e}")))?;
        }
        Ok(())
    }

    pub async fn close(&mut self) {
        if let Some(stream) = self.stream.take() {
            let _ = stream.into_std();
        }
    }
}

/// Build an ILP line from a MarketSnapshot.
///
/// Schema: market_data (table)
///   Tags:   symbol
///   Fields: all numeric + boolean fields
///   Timestamp: ingestion_ts_ns (nanoseconds)
pub fn build_ilp_line(snap: &MarketSnapshot) -> String {
    // Escape commas and spaces in tag values (symbol is alphanumeric, so safe)
    let symbol = snap.symbol.replace(' ', "\\ ").replace(',', "\\,");

    // Boolean → ILP uses "t" / "f"
    let z_mad_div = if snap.z_mad_divergence { "t" } else { "f" };
    let stale = if snap.is_stale { "t" } else { "f" };

    format!(
        "market_data,symbol={symbol} \
        mid_price={mid:.6},\
        bid_price={bid:.6},\
        ask_price={ask:.6},\
        bid_size={bs:.2},\
        ask_size={as_:.2},\
        spread={spread:.6},\
        last_trade_price={ltp:.6},\
        last_trade_size={lts:.2},\
        z_score={z:.6},\
        mad_score={mad:.6},\
        z_mad_divergence={zdiv},\
        order_flow_imbalance={ofi:.6},\
        realized_volatility={rvol:.6},\
        adv_30d={adv:.2},\
        regime_confidence={rc:.4},\
        is_stale={stale},\
        exchange_ts={ets}i \
        {ts}\n",
        mid = snap.mid_price,
        bid = snap.bid_price,
        ask = snap.ask_price,
        bs = snap.bid_size,
        as_ = snap.ask_size,
        spread = snap.spread,
        ltp = snap.last_trade_price,
        lts = snap.last_trade_size,
        z = snap.z_score,
        mad = snap.mad_score,
        zdiv = z_mad_div,
        ofi = snap.order_flow_imbalance,
        rvol = snap.realized_volatility,
        adv = snap.adv_30d,
        rc = snap.regime_confidence,
        stale = stale,
        ets = snap.exchange_ts_ns,
        ts = snap.ingestion_ts_ns,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::normalizer::MarketSnapshot;

    fn make_snap() -> MarketSnapshot {
        MarketSnapshot {
            symbol: "AAPL".into(),
            ingestion_ts_ns: 1_700_000_000_000_000_000,
            exchange_ts_ns: 1_700_000_000_000_000_000,
            mid_price: 150.025,
            bid_price: 150.0,
            ask_price: 150.05,
            bid_size: 200.0,
            ask_size: 300.0,
            spread: 0.05,
            last_trade_price: 150.02,
            last_trade_size: 100.0,
            last_trade_ts_ns: 1_700_000_000_000_000_000,
            z_score: 1.23,
            mad_score: 0.98,
            z_mad_divergence: false,
            order_flow_imbalance: -0.2,
            realized_volatility: 0.25,
            adv_30d: 50_000_000.0,
            regime_label: "TRENDING_BULL".into(),
            regime_confidence: 0.87,
            is_stale: false,
            stale_reason: String::new(),
        }
    }

    #[test]
    fn test_ilp_line_format() {
        let snap = make_snap();
        let line = build_ilp_line(&snap);
        assert!(line.starts_with("market_data,symbol=AAPL "));
        assert!(line.contains("mid_price=150.025000"));
        assert!(line.contains("is_stale=f"));
        assert!(line.ends_with('\n'));
    }

    #[test]
    fn test_ilp_line_stale() {
        let mut snap = make_snap();
        snap.is_stale = true;
        snap.stale_reason = "L2_TTL_BREACH".into();
        let line = build_ilp_line(&snap);
        assert!(line.contains("is_stale=t"));
    }

    #[test]
    fn test_ilp_timestamp_present() {
        let snap = make_snap();
        let line = build_ilp_line(&snap);
        // Line should end with: <space><timestamp_ns>\n
        assert!(line.contains("1700000000000000000\n"));
    }
}
