# OPTIMIZER SEARCH RANGES — Quick Reference

**Generated:** 2026-03-13  
**Data Source:** 1008 candles, 1008 signals, 6 completed trades  
**Confidence Level:** Medium (small trade sample; indicator distributions solid)

---

## SEARCH RANGES FOR NEXT OPTIMIZATION RUN

### Parameter 1: MA_LEN (EMA length for premium band smoothing)
```
Current:  84
MIN:      50
MAX:      150
Step:     5 or 10
```
**Rationale:**
- Current 84 is in reasonable range (±40% tolerance)
- 3-min ADX p50 = 25.13 suggests EMA should be 60–100 for stable bands
- Test range 50–150 to find optimal responsiveness
- Lower values (50–80) = more responsive, higher signal frequency
- Higher values (100–150) = smoother bands, lower false signal rate

---

### Parameter 2: BAND_MULT (volatility multiplier for premium bands)
```
Current:  0.4 – 0.9
MIN:      0.35
MAX:      1.2
Step:     0.05 or 0.10
```
**Rationale:**
- Band level distribution shows signals across levels 1–7 (appropriate spread)
- Current range spans 50% of suggested range
- Extend upper bound to 1.2 to test wider bands
- Tighter bands (0.35–0.6) = more signals, more noise
- Wider bands (0.8–1.2) = fewer signals, potentially higher quality

---

### Parameter 3: ADX_MIN_THRESHOLD (minimum ADX for entry gate)
```
Current:  25
MIN:      20
MAX:      28
Step:     1 or 2
```
**Rationale:**
- Current ADX >= 25 filters 47% of 3-min signals (29 of 55 pass)
- ADX median: 15-min = 23.33, 3-min = 25.13, 5-min = 20.51
- At ADX >= 20: ~76–100% of signals pass (possibly too permissive)
- At ADX >= 25: ~50–75% of signals pass (current; balanced)
- At ADX >= 28: ~42–58% of signals pass (conservative)
- **Recommendation:** Test 20–28; interval-specific tuning may help

---

### Parameter 4: RSI_MAX_THRESHOLD (maximum RSI for entry gate; RSI < threshold blocks)
```
Current:  40
MIN:      38
MAX:      50
Step:     1 or 2
```
**Rationale:**
- Current RSI < 40 allows 76–100% of signals (very permissive)
- RSI mean across intervals: 48–52 (neutral zone)
- At RSI < 40: Captures nearly all mean-reversion entries
- At RSI < 45: Captures 71–82% of signals (looser, slightly more overbought)
- At RSI < 50: Captures 55–82% (includes neutral territory)
- **CRITICAL:** Trade #2 loss had RSI=66.46 (overbought) — suggest tightening to RSI < 35 if momentum avoidance needed
- **Recommendation:** Keep at RSI < 40 baseline; test 38–45 range for fine-tuning

---

### Parameter 5: TP_PCT (Take Profit target as % of entry price)
```
Current observed:  0.0028 – 0.0383 (0.28% – 3.83%)
MIN:      0.002
MAX:      0.010
Step:     0.0005 or 0.001
```
**Rationale:**
- ATR% median: 3-min = 0.1910%, 5-min = 0.2058%, 15-min = 0.4089%
- Current TP targets (0.28–3.83%) span wide range
- 0.3–0.5% TP is realistic on 3-min (achievable within 1–2 ATR)
- 0.5–1.0% TP is realistic on 15-min
- **LIVE_TP_SCALE = 0.75** already scaling down server TP by 25%
- **Recommendation:** Test 0.002–0.010 (0.2%–1.0%); focus optimizer on 0.003–0.006 (0.3%–0.6%)

---

## SUMMARY TABLE

| Param | Current | Min | Max | Step | Priority |
|-------|---------|-----|-----|------|----------|
| MA_LEN | 84 | 50 | 150 | 10 | HIGH |
| BAND_MULT | 0.4–0.9 | 0.35 | 1.2 | 0.05 | HIGH |
| ADX_MIN | 25 | 20 | 28 | 2 | MEDIUM |
| RSI_MAX | 40 | 38 | 50 | 2 | LOW* |
| TP_PCT | 0.0028–0.0383 | 0.002 | 0.010 | 0.0005 | HIGH |

**\* LOW PRIORITY:** RSI < 40 is appropriate; unlikely to move far from current value.

---

## INTERVAL-SPECIFIC NOTES

### 15-min Interval (162 candles, 12 signals)
- **ADX profile:** Mean 25.26, p50 = 23.33, p90 = 34.38 (trending)
- **RSI profile:** Mean 51.33, p50 = 50.12 (neutral)
- **Band level:** 50% Level 1 (conservative entries)
- **Recommendation:** Can support ADX >= 25–28, wider TP targets (0.5–1.0%)

### 3-min Interval (528 candles, 55 signals)
- **ADX profile:** Mean 27.94, p50 = 25.13, p90 = 45.33 (most trendy)
- **RSI profile:** Mean 51.98, p50 = 53.43 (neutral-overbought)
- **Band level:** Spread across 1–7 (diverse band multiples)
- **ATR%:** 0.1910% median (tight ranges)
- **Recommendation:** Test ADX >= 20–25, tighter TP targets (0.3–0.5%), smaller position sizes

### 5-min Interval (318 candles, 11 signals)
- **ADX profile:** Mean 22.83, p50 = 20.51, p90 = 35.64 (choppy)
- **RSI profile:** Mean 48.48, p50 = 48.74 (neutral)
- **Band level:** 45% Level 3 (wider bands already active)
- **ATR%:** 0.2058% median (medium ranges)
- **Recommendation:** May need looser ADX gate (ADX >= 20), medium TP targets (0.4–0.6%)

---

## IMPLEMENTATION NOTES

1. **Optimizer can handle this in one run:**
   - MA_LEN: 50–150 (step 10) = 11 values
   - BAND_MULT: 0.35–1.2 (step 0.05) = 18 values
   - ADX_MIN: 20–28 (step 2) = 5 values
   - RSI_MAX: 38–50 (step 2) = 7 values
   - TP_PCT: 0.002–0.010 (step 0.0005) = 17 values
   - **Total combinations:** ~11 × 18 × 5 × 7 × 17 = 119,700 possible trials

2. **Recommended approach:**
   - Run with INIT_TRIALS = 5000–10000 to sample space
   - Score by: Monte Carlo Sharpe ratio (if >50 trades), else % wins or avg_pnl
   - Store all runs; compare against previous baseline

3. **Post-optimization:**
   - Backtest best params on 7–30 day historical period
   - A/B test against current (MA=84, BAND=0.4–0.9) in paper trading
   - Deploy to live only after backtest Sharpe > 1.0

---

## DATA QUALITY WARNINGS

⚠️ **Only 6 completed live trades** — optimizer output will be based on backtest, not live PnL  
⚠️ **No historical optimization runs recorded** — unable to compare against previous best params  
⚠️ **One interval (3-min) dominates trading** — 15-min and 5-min tuning may need separate runs  
⚠️ **RSI gate possible false alarm** — Trade #2 passed RSI < 40 with RSI=66.46; verify gate logic

---

## RELATED FILES

- Full report: `/Users/partyproper/Documents/Mean Reversion Trader/CANDLE_ANALYTICS_REPORT.md`
- Database: `/Users/partyproper/Documents/Mean Reversion Trader/data/trading.db`
- Config: `engine/utils/constants.py`

