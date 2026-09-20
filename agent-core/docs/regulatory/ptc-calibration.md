# Pre-Trade Control Calibration Methodology

> ## WARNING: THE FIGURES IN THIS DOCUMENT ARE UNSUBSTANTIATED. DO NOT RELY ON THEM.
> As of 2026-09-19 there is **no backtest engine, no historical dataset and no seed-scenario database** in this repository (`agent-core/backtesting/` is an empty package; there is no `engine.py` or `wfa.py`; no vector-DB scenario data exists; Aegis is a placeholder). The "current calibration" values and "results" below (1.5% collar "from 3-year P99", 1.8% "P99.9 daily loss", the "500 seed scenarios" win rates of 38/48/57/66%, the "45% higher maximum drawdown" for regime mismatch) **cannot have been produced from anything in this repo** and have no provenance. They are retained only so that the intended methodology and the values now configured in `docker-compose.yml` remain visible. They must not be cited to a regulator, used to justify a limit, or treated as calibrated.
>
> **Known contradiction (unresolved):** the *Regime Mismatch* soft block here (mismatch **AND** HMM confidence **> 0.70**) contradicts `mifid-ii-rts6-self-assessment-template.md` §2.2 (HMM confidence **< 0.6 OR** mismatch). They differ in boolean structure and in direction. A proposed single definition is in `docs/specs/phase_3_aegis_execution.md` §4.4. Further inconsistency: order-size limit here uses 20-day ADV, but `MarketSnapshot` and the Phase 1 spec provide `adv_30d`.
>
> **Procedure that will replace these figures:** see "Replacement procedure" at the end of this document and `docs/specs/phase_4_backtesting_compliance.md` §7. This banner is to be removed only when each section is regenerated from a signed calibration report.
>
> Status: **DRAFT / methodology description / requires qualified legal review** for every reference to RTS 6 or ESMA guidance below (article and guideline citations here have not been verified).

## Claims vs implementation

| Claim in this document | Implemented / evidenced? | Tracking |
|---|---|---|
| Price collar 1.5% derived from 3-year P99 | No evidence; value exists only as `PRICE_COLLAR_PCT=1.5` in compose; Aegis not implemented | `phase_4_backtesting_compliance.md` §7; `phase_3_aegis_execution.md` C09 |
| Daily drawdown 2.0% from 99.9th pct of daily loss (1.8%) | No evidence; config value only | Phase 4 §7; Phase 3 C15 |
| Order size limit 0.5% of 20-day ADV, recomputed daily | Not implemented; ADV window inconsistent with `adv_30d` | Phase 3 C12, §3 |
| Unusual-order-size soft block (P95 of 30 days) | Not implemented; no order history exists | Phase 3 C19 |
| Regime mismatch threshold and 45% drawdown effect | Not implemented; contradicts RTS6 template | Phase 3 §4.4 |
| 500-scenario win rates by omega bucket | No scenario database; not reproducible | Phase 4 §7 |
| "Independently set by the investment firm" | TODO(owner): no evidence of who sets thresholds | n/a |

**MiFID II RTS 6 Reference:** Article 17(1), ESMA Guidelines on Systems and Controls (citation unverified; requires qualified legal review)
**Reviewed:** TODO(owner): date of review (never reviewed: no calibration data exists)

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
**Next review:** TODO(owner): set after the first real calibration report exists

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

**Threshold:** X shares per order, where X = 0.5% of 20-day ADV (computed value, not a placeholder; UNSUBSTANTIATED, and the ADV window conflicts with `adv_30d`, see banner)

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
**Next review:** When Vector DB is updated with 100+ new scenarios (note: `zone-a/vector-db/` currently holds only `__init__.py`)

---

## Replacement procedure (PROPOSED; replaces the unsubstantiated figures above)

Full requirements are in `docs/specs/phase_4_backtesting_compliance.md` §7. Summary:

1. Obtain and version a historical dataset; record a dataset manifest (source, date range, file hashes, universe). TODO(owner): data source/licence/depth.
2. Build the backtest engine and regime-aware WFA (Phase 4 §4-§5); enforce the holdout.
3. For each control, run its calibration script; each emits a **calibration report** with dataset hash, code commit, seed, method, computed value **with confidence interval**, the value chosen, safety margin and the person who chose it.
4. Only a control with a signed report may be configured in Aegis. Configure the value from the report, not from this document.
5. Regenerate the sections above from the reports, resolve the regime-mismatch contradiction and the ADV-window inconsistency, then remove the banner.
6. Note the statistical caveats: the 99.9th percentile over ~5 years of daily data rests on about one observation (report a bootstrap CI or use extreme-value methods); 500 scenarios split across four omega buckets give wide binomial intervals; an LLM evaluated on periods inside its training data can be contaminated by look-ahead.

Regulatory review of the calibration approach against ESMA/RTS 6 expectations **requires qualified legal review**.
