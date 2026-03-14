# Candle Analytics Analysis — Complete Documentation

**Generated:** 2026-03-13  
**Data Period:** 1008 candles across 5 days  
**Database:** `/Users/partyproper/Documents/Mean Reversion Trader/data/trading.db`

---

## Quick Navigation

### For Decision-Makers (5-10 min read)
**START HERE:** `/Users/partyproper/Documents/Mean Reversion Trader/ANALYSIS_EXECUTIVE_SUMMARY.txt`

- 5 key findings (ADX, RSI, Volume, Bands, TP)
- 3 urgent action items this week
- 4-week roadmap to optimization
- Risk assessment

---

### For Configuration (15 min read)
**USE THIS:** `/Users/partyproper/Documents/Mean Reversion Trader/OPTIMIZER_SEARCH_RANGES.md`

Exact parameter ranges for next optimization run:
- MA_LEN: 50–150 (current: 84)
- BAND_MULT: 0.35–1.2 (current: 0.4–0.9)
- ADX_MIN: 20–28 (current: 25)
- RSI_MAX: 38–50 (current: 40)
- TP_PCT: 0.002–0.010 (current: 0.0028–0.0383)

Includes:
- Detailed rationale for each range
- Interval-specific recommendations (15-min, 3-min, 5-min)
- Implementation notes (trial count, scoring)
- Data quality warnings

---

### For Analysis (30 min read)
**FULL REPORT:** `/Users/partyproper/Documents/Mean Reversion Trader/CANDLE_ANALYTICS_REPORT.md`

Comprehensive technical analysis with 9 sections:
1. ADX Distribution Analysis (percentiles p10–p90 per interval)
2. RSI Distribution Analysis (percentiles p10–p90 per interval)
3. ADX Threshold Sensitivity (signal pass/block at ADX 20, 22, 25, 28, 30, 32)
4. RSI Threshold Sensitivity (signal pass/block at RSI 40, 45, 50, 55, 60)
5. Trade Outcomes by ADX/RSI at Entry (6 completed trades analyzed)
6. Volume Ratio & ATR% Distributions (volatility profiles per interval)
7. Band Level Distribution (entry signal characteristics)
8. TP Distance Analysis (actual move vs target; reveals exit timing issues)
9. Parameter Optimization History (empty; no previous runs recorded)

Every section includes raw data, key findings, and recommendations.

---

### For SQL Reference
**QUERY REFERENCE:** `/Users/partyproper/Documents/Mean Reversion Trader/ANALYSIS_QUERIES.sql`

All 9 SQL queries used in analysis:
1. ADX percentile distribution (window functions)
2. RSI percentile distribution (window functions)
3. ADX threshold sensitivity (conditional aggregation)
4. RSI threshold sensitivity (conditional aggregation)
5. Trade outcomes with indicators (join trade + candle_analytics)
6. TP distance analysis (actual move calculation)
7. Volume ratio and ATR% distributions (window functions + percentiles)
8. Band level distribution (proportion analysis)
9. Row counts diagnostic (table health check)

All queries can be copy-pasted directly into sqlite3 CLI.

---

## Key Findings Summary

### Positive Indicators

✓ **ADX distribution is healthy** for mean-reversion
  - 3-min median ADX = 25.13 (trending periods available)
  - Current ADX >= 25 gate is reasonable

✓ **RSI distribution is ideal** (neutral zone dominates)
  - All intervals: RSI mean ~48–52 (classic ranging territory)
  - Good for mean-reversion strategy

✓ **Volume and ATR profiles are realistic**
  - TP targets of 0.3–0.5% are achievable on 3-min
  - 0.5–1.0% achievable on 15-min

✓ **Band level distribution is appropriate**
  - Signals spread across levels 1–7 (good variety)

### Issues Requiring Action

⚠️ **CRITICAL: RSI Gate May Not Be Active**
  - Trade #2 entered at RSI=66.46 (clearly overbought)
  - Current gate RSI < 40 should have blocked this
  - **Action:** Verify RSI gate logic in live_trader.py

⚠️ **CRITICAL: TP Targets Not Being Hit**
  - Only 1 of 6 trades moved in correct direction
  - Several BAND_ENTRY exits show 0.0% move
  - **Action:** Debug BAND_EXIT timing and fill accuracy

⚠️ **Insufficient Data**
  - Only 6 trades (minimum 50 for statistical confidence)
  - Only 5 days of backtest data (recommend 14–30 days)
  - **Action:** Collect 2–4 weeks of live trading before re-optimizing

---

## Parameter Recommendations

### Priority 1: HIGH (Test these immediately)
```
MA_LEN:    50 – 150  (±40% from current 84)
BAND_MULT: 0.35 – 1.2 (extend upper bound to test wider bands)
TP_PCT:    0.002 – 0.010 (broader range to fine-tune hit rate)
```

### Priority 2: MEDIUM (Fine-tune after Priority 1)
```
ADX_MIN:   20 – 28   (critical sensitivity on 3-min; test 20–25 first)
```

### Priority 3: LOW (Unlikely to change significantly)
```
RSI_MAX:   38 – 50   (current 40 is sound; test ±2 only)
```

---

## Interval-Specific Insights

### 3-min (528 candles, 55 signals) — MOST ACTIVE
- ADX mean: 27.94, median: 25.13 (supports ADX >= 20–25)
- Tight ATR% (0.19% median) requires small TP targets (0.3–0.5%)
- Optimize here first

### 5-min (318 candles, 11 signals) — Medium Activity
- ADX mean: 22.83, median: 20.51 (needs looser ADX >= 20)
- ATR% slightly higher (0.21% median), TP targets 0.4–0.6%

### 15-min (162 candles, 12 signals) — Low Activity
- ADX mean: 25.26, median: 23.33 (trending)
- Large ATR% (0.41% median), supports wider TP (0.5–1.0%)
- May not have enough data for independent optimization

---

## Implementation Roadmap

### Week 1: FIX & VERIFY
- [ ] Verify RSI gate logic (Trade #2 anomaly)
- [ ] Debug 0% move exits (BAND_EXIT timing)
- [ ] Check fill accuracy in live vs paper
- [ ] Add signal rejection logging

### Week 2–3: COLLECT DATA
- [ ] Run live trading for 10–14 more days
- [ ] Target 50+ completed trades
- [ ] Monitor for repeated issues

### Week 4: OPTIMIZE & BACKTEST
- [ ] Run optimizer with OPTIMIZER_SEARCH_RANGES settings (5,000–10,000 trials)
- [ ] Backtest best params on 30-day historical period
- [ ] A/B test in paper trading
- [ ] Deploy to live only if backtest Sharpe > 1.0

---

## Data Quality Checklist

| Item | Status | Notes |
|------|--------|-------|
| Candles | ✓ Good | 1008 rows, complete |
| Candle Analytics | ✓ Good | 1008 rows, all indicators populated |
| Signals | ✓ Good | 1008 rows, raw_band_level present |
| Trades | ⚠️ Early | Only 6 rows; need 50+ |
| Orders | ✗ Empty | Not populated |
| Events | ✗ Empty | Not populated |
| Optimization History | ✗ Empty | No previous runs recorded |

---

## Database Connection

```bash
sqlite3 "/Users/partyproper/Documents/Mean Reversion Trader/data/trading.db"

-- Check data freshness
SELECT MAX(ts_utc) FROM candles;
SELECT COUNT(*) FROM trades;
SELECT * FROM candle_analytics LIMIT 1;
```

---

## Files in This Analysis

| File | Size | Purpose |
|------|------|---------|
| ANALYSIS_EXECUTIVE_SUMMARY.txt | 7.6 KB | 10-min action summary |
| OPTIMIZER_SEARCH_RANGES.md | 5.9 KB | Parameter configuration |
| CANDLE_ANALYTICS_REPORT.md | 16 KB | Full technical analysis |
| ANALYSIS_QUERIES.sql | 8.4 KB | SQL queries (copy-paste ready) |
| ANALYSIS_README.md | This file | Navigation guide |

---

## Questions & Troubleshooting

**Q: Should I run the optimizer now?**
A: No. Fix the RSI gate and BAND_EXIT issues first, then collect 50+ trades before re-optimizing.

**Q: Which parameter should I tune first?**
A: MA_LEN and BAND_MULT (high impact). Test ADX_MIN second. RSI_MAX last.

**Q: What Sharpe ratio should I target?**
A: At least 1.0 in backtesting (50+ trades). 1.5+ is good.

**Q: Should I optimize all 3 intervals together?**
A: Yes, in one run. But monitor 3-min separately since it dominates trading.

**Q: Is 6 trades enough to validate anything?**
A: No. It's an early-phase snapshot. Collect 50+ before making deployment decisions.

---

## Support & References

- Main code: `/Users/partyproper/Documents/Mean Reversion Trader/`
- Optimizer: `engine/optimize/optimizer.py`
- Live trader: `engine/trading/live_trader.py`
- Paper trader: `engine/trading/paper_trader.py`
- Constants: `engine/utils/constants.py`

---

Generated: 2026-03-13
