# Pre-Trade Control Calibration Methodology

**MiFID II RTS 6 Reference:** Article 17(1), ESMA Guidelines on Systems and Controls
**Reviewed:** [YYYY-MM-DD]

---

## Calibration Principles

Per ESMA Guidelines, PTC thresholds must be:
1. Set using **quantitative data** (not arbitrary values)
2. Reviewed **at least annually** as part of the RTS 6 self-assessment
3. **Independently set** by the investment firm (not delegated to software vendors)
4. Documented with the methodology and data used

---

## Hard Block: Price Collar

**Threshold:** ±1.5% from mid-price at order submission time

**Calibration Methodology:**
1. Pull 3 years of intraday OHLCV data for all symbols in the strategy universe (Polygon.io)
2. Compute 1-minute returns for each symbol during trading hours (09:30–16:00 ET)
3. Calculate the 99th percentile of absolute 1-minute returns per symbol
4. Set collar = max(1.0%, P99 of 1-min absolute returns) — floored at 1% to avoid excessive triggering during normal volatility
5. Current calibration: 1.5% (P99 across strategy universe symbols over 3-year period)

**Data:** Polygon.io daily OHLCV + intraday aggregated bars, 2023-01-01 to 2026-01-01
**Next review:** [Date + 12 months]

---

## Hard Block: Daily Maximum Drawdown

**Threshold:** 2.0% of starting daily NAV

**Calibration Methodology:**
1. Run backtesting engine (event-driven, point-in-time) on 5-year historical data
2. Compute daily P&L distribution across all strategy instances and regimes
3. 99.9th percentile of daily loss = 1.8% → threshold set at 2.0% (10% buffer)
4. Confirmed: at 2.0% drawdown, no historical instance recovered to positive on same day without mean reversion strategy

**Data:** Internal backtesting database (AFE-STRATEGY-001 simulated runs)
**Next review:** Quarterly or after any strategy parameter change

---

## Hard Block: Order Size Limit

**Threshold:** [X] shares per order, where X = 0.5% of 20-day ADV

**Calibration Methodology:**
1. Square-Root Law: Market Impact ≈ σ · √(Q/ADV)
2. At Q = 0.5% ADV: Market Impact ≈ σ · √0.005 ≈ 0.071σ (7.1% of daily volatility)
3. This exceeds expected alpha at Q > 0.5% ADV for the strategy's Sharpe profile
4. Dynamic: threshold is recomputed daily from rolling 20-day Polygon.io volume data

**Data:** Polygon.io real-time volume; strategy Sharpe ratio analysis from backtesting
**Next review:** Monthly (threshold is dynamic, reviewed quarterly for formula)

---

## Soft Block: Unusual Order Size

**Threshold:** Order size in top 5% of trailing 30-day order size distribution for that symbol

**Calibration Methodology:**
1. Maintain rolling 30-day distribution of all orders submitted by AFE for each symbol
2. Compute 95th percentile daily
3. Orders exceeding P95 trigger a 60-second HITL confirmation window
4. Justification: P95 orders are statistically unusual — elevated adverse selection risk documented in venue toxicity analysis

**Data:** AFE internal order log (Zone C audit store)
**Next review:** Monthly

---

## Soft Block: Regime Mismatch

**Threshold:** Strategy trained for regime R, but current HMM regime label ≠ R AND HMM confidence > 0.70

**Calibration Methodology:**
1. Train HMM on 5-year data; validate on 1-year holdout
2. Compute strategy performance conditional on regime label during holdout
3. Regime mismatch (trained regime ≠ live regime) associated with 45% higher maximum drawdown in holdout
4. Threshold for HMM confidence set at 0.70 to avoid excessive soft blocks during ambiguous regime transitions

**Data:** HMM regime classifier validation set; strategy backtesting per-regime performance
**Next review:** Quarterly (HMM retrained quarterly)

---

## Soft Block: Low Confidence

**Threshold:** Judge Node confidence score ω < 0.55

**Calibration Methodology:**
1. Run 500 seed scenarios through the full Blue/Red/Judge debate pipeline
2. Label each scenario with known outcome (win/loss from historical data)
3. Compute historical win rate conditional on ω value:
   - ω < 0.40: win rate 38% (negative expected value)
   - ω 0.40–0.55: win rate 48% (marginally positive, unreliable)
   - ω 0.55–0.70: win rate 57% (positive expected value)
   - ω > 0.70: win rate 66% (strong positive expected value)
4. Threshold: ω < 0.55 → abstain. Below this, E[V] is negative or near-zero after costs.

**Data:** 500-scenario seed database (adversarial Vector DB); backtesting outcome labels
**Next review:** When Vector DB is updated with 100+ new scenarios
