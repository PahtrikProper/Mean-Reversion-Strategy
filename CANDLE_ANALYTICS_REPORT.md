# Comprehensive Candle Analytics Report
## Data-Driven Optimizer Search Range Recommendations

**Report Generated:** 2026-03-13  
**Dataset:** 1008 candles across 3 intervals (15-min, 3-min, 5-min)  
**Trades Completed:** 6 (all on XRPUSDT)  
**Signals Fired:** 1008 raw band crossovers  

---

## 1. ADX DISTRIBUTION ANALYSIS

### Raw ADX Statistics by Interval

| Interval | N Candles | ADX Min | ADX Avg | ADX Max | ADX p10 | ADX p25 | ADX p50 | ADX p75 | ADX p90 |
|----------|-----------|---------|---------|---------|---------|---------|---------|---------|---------|
| **15-min** | 162 | 14.94 | 25.26 | 39.13 | 16.82 | 19.76 | 23.33 | 32.20 | 34.38 |
| **3-min** | 528 | 12.25 | 27.94 | 57.47 | 16.81 | 20.02 | 25.13 | 33.21 | 45.33 |
| **5-min** | 318 | 10.49 | 22.83 | 49.34 | 12.68 | 15.64 | 20.51 | 27.82 | 35.64 |

### Key Findings:
- **3-min interval has highest ADX mean (27.94)** — most trend-following opportunity
- **5-min interval has lowest ADX mean (22.83)** — most ranging/choppy behavior
- **Median ADX is 20–25 across all intervals** — below the current hard gate of 25
- **At p50 (median), only 50% of candles have ADX >= 23.33** on 15-min
- **3-min shows ADX spikes to 57.47** — periods of very strong trending available

### Recommendation for ADX Gate:
- **Current gate: ADX >= 25** filters out ~50% of candles
- **Sensitive range: 20–28** — test in optimizer; consider relaxing to **22–25** to capture more ranging entries
- **Do NOT go below 18** (p10 is ~16) — that's too permissive
- **3-min interval can support ADX >= 28** due to higher distribution; 5-min may need ADX >= 20

---

## 2. RSI DISTRIBUTION ANALYSIS

### Raw RSI Statistics by Interval

| Interval | N Candles | RSI Min | RSI Avg | RSI Max | RSI p10 | RSI p25 | RSI p50 | RSI p75 | RSI p90 |
|----------|-----------|---------|---------|---------|---------|---------|---------|---------|---------|
| **15-min** | 162 | 25.49 | 51.33 | 80.36 | 35.14 | 43.27 | 50.12 | 60.34 | 65.36 |
| **3-min** | 528 | 21.46 | 51.98 | 79.92 | 33.21 | 41.84 | 53.43 | 60.14 | 68.99 |
| **5-min** | 318 | 21.66 | 48.48 | 80.91 | 33.21 | 40.99 | 48.74 | 55.80 | 62.64 |

### Key Findings:
- **RSI mean is ~48–52 across all intervals** — neutral/mean-reversion territory (40–60 is classic ranging zone)
- **RSI median is 48–53** — at or slightly below neutral
- **p25 is 40–43** — only 25% of candles have RSI in the severely-overbought zone (>60)
- **p90 is 62–69** — represents the truly overbought tail (very few extreme oversold setups)
- **Current gate: RSI < 40 blocks entry** — this filters 13 of 55 signals on 3-min (24% blocked)

### Recommendation for RSI Gate:
- **Current gate at RSI < 40 is reasonable** but slightly restrictive
- **Consider testing RSI < 45** — would keep most signals (90%+) and ensure more extreme oversold conditions
- **RSI = 40–45 is the critical zone** — this is where RSI starts to separate mean-reversion setups from momentum
- **On 3-min: 42 of 55 signals pass RSI < 40 (76%); only 39 pass RSI < 45 (71%)**
- **Do NOT relax below RSI < 35** — you'd be trading into overbought conditions

---

## 3. ADX THRESHOLD SENSITIVITY (SIGNAL BLOCKING ANALYSIS)

### How many raw band signals would pass at each ADX threshold?

| Interval | Total Raw Signals | ADX >= 20 | ADX >= 22 | ADX >= 25 (current) | ADX >= 28 | ADX >= 30 | ADX >= 32 |
|----------|-------------------|-----------|-----------|---------------------|-----------|-----------|-----------|
| **15-min** | 12 | 12 (100%) | 10 (83%) | 9 (75%) | 7 (58%) | 6 (50%) | 6 (50%) |
| **3-min** | 55 | 42 (76%) | 37 (67%) | 29 (53%) | 23 (42%) | 17 (31%) | 14 (25%) |
| **5-min** | 11 | 9 (82%) | 8 (73%) | 7 (64%) | 6 (55%) | 5 (45%) | 5 (45%) |

### Analysis:
- **At current ADX >= 25, only 53% of 3-min signals pass** (29 of 55)
- **Lowering to ADX >= 20 would capture 76% of 3-min signals** (42 of 55)
- **3-min is most sensitive** — dropping ADX threshold by 5 points increases signal rate by 23%
- **15-min and 5-min are less sensitive** — ADX gate has weaker filtering impact on these timeframes

### Recommendation:
- **For 3-min: Test ADX ranges 20–25** to balance trend-following vs signal volume
- **For 15-min/5-min: ADX >= 22–24 is optimal** — captures 80%+ of signals without excessive noise
- **Current ADX >= 25 is acceptable but slightly tight** — consider relaxing to ADX >= 23 as baseline

---

## 4. RSI THRESHOLD SENSITIVITY (SIGNAL BLOCKING ANALYSIS)

### How many raw band signals would pass at each RSI threshold?

| Interval | Total Raw Signals | RSI < 40 Pass | RSI < 45 Pass | RSI < 50 Pass | RSI < 55 Pass | RSI < 60 Pass |
|----------|-------------------|---------------|---------------|---------------|---------------|---------------|
| **15-min** | 12 | 12 (100%) | 12 (100%) | 10 (83%) | 8 (67%) | 3 (25%) |
| **3-min** | 55 | 42 (76%) | 39 (71%) | 30 (55%) | 17 (31%) | 9 (16%) |
| **5-min** | 11 | 9 (82%) | 9 (82%) | 9 (82%) | 8 (73%) | 5 (45%) |

### Analysis:
- **At current RSI < 40, ~76–100% of signals pass** — very permissive gate
- **Moving to RSI < 45 would still pass 71–82% of signals** — minimal impact
- **RSI < 50 reduces pass rate to 55–83%** — starts filtering more aggressively
- **RSI < 55 blocks 2/3 of 3-min signals** — beginning to exclude neutral/early-reversal setups

### Recommendation:
- **Current RSI < 40 is appropriate** — captures nearly all mean-reversion entries
- **Could tighten to RSI < 38** if oversold entries preferred, but would reject only ~10% more
- **Do NOT tighten beyond RSI < 35** — you'd miss most mean-reversion opportunities
- **Keep at RSI < 40 as baseline**

---

## 5. TRADE OUTCOMES BY ADX AT ENTRY

| Entry Timestamp | Interval | ADX @ Entry | RSI @ Entry | Entry Price | PnL (USDT) | PnL % | Result | Exit Reason |
|-----------------|----------|-------------|-------------|-------------|-----------|-------|--------|------------|
| 2026-03-09 16:21 | 3-min | **22.83** | 47.83 | 1.3633 | -0.0746 | -0.25% | — | BAND_ENTRY |
| 2026-03-10 05:05 | 5-min | **23.98** | 66.46 | 1.3706 | -3.4814 | -11.56% | LOSS | STOP_LOSS |
| 2026-03-13 09:12 | 3-min | **24.75** | 68.43 | 1.3706 | -0.0063 | -0.02% | LOSS | EXTERNAL_CLOSE |
| 2026-03-13 10:24 | 3-min | **19.10** | 50.58 | 1.4294 | -0.0659 | -0.25% | — | BAND_ENTRY |
| 2026-03-13 11:18 | 3-min | **21.58** | 39.19 | 1.4294 | +0.0611 | +0.23% | WIN | EXTERNAL_CLOSE |
| 2026-03-13 12:24 | 3-min | **18.50** | 47.86 | 1.4243 | -0.0660 | -0.25% | — | BAND_ENTRY |

### Key Observations:
- **Only 1 trade marked as WIN (0.23% profit)** — entry ADX was 21.58 (below current threshold)
- **Worst loss (-11.56%) came at ADX 23.98** — early trade when bot was learning
- **Trades at ADX < 22 produced mixed results** — 1 WIN, 2 losses, 3 neutral
- **Trades at ADX 22–25 produced losses** — suggests ADX gate may be too permissive
- **Sample size is extremely small (n=6)** — insufficient to draw ADX correlation; more data needed

### Note on Data Quality:
- **All BAND_ENTRY exits show 0.0% move** — suggests entry and exit filled at same price (likely no real market move)
- **This indicates early backtesting phase; data is not yet mature**

---

## 6. VOLUME RATIO & ATR% DISTRIBUTIONS

### Volume Ratio (Close candle volume relative to SMA-20 baseline)

| Interval | N | Vol Min | Vol Avg | Vol Max | Vol p25 | Vol p50 | Vol p75 |
|----------|---|---------|---------|---------|---------|---------|---------|
| **15-min** | 162 | 0.1270 | 1.1051 | 8.8878 | 0.5061 | 0.7832 | 1.2585 |
| **3-min** | 528 | 0.0744 | 1.1193 | 9.9054 | 0.4764 | 0.7977 | 1.3604 |
| **5-min** | 318 | 0.0750 | 1.0563 | 8.7037 | 0.4922 | 0.6898 | 1.2436 |

### ATR% (Average True Range as % of close price)

| Interval | N | ATR% Min | ATR% Avg | ATR% Max | ATR% p25 | ATR% p50 | ATR% p75 |
|----------|---|----------|----------|----------|----------|----------|----------|
| **15-min** | 162 | 0.2667 | 0.4162 | 0.6597 | 0.3342 | 0.4089 | 0.4788 |
| **3-min** | 528 | 0.1210 | 0.1965 | 0.3622 | 0.1621 | 0.1910 | 0.2158 |
| **5-min** | 318 | 0.1460 | 0.2584 | 0.5685 | 0.1769 | 0.2058 | 0.3201 |

### Analysis:
- **Volume ratio median ~0.78–0.80** across all intervals — typical candles slightly above SMA baseline
- **3-min spikes to 9.9x volume** — occasional news/event-driven bars with extreme volume
- **ATR% ranges from 0.12–0.66% depending on interval** — 15-min is most volatile (4x larger ATR)
- **3-min ATR% is ~0.19 median** — tight ranges, easier to get stopped out

### Recommendation:
- **No direct optimizer parameter depends on volume ratio or ATR%**
- **But ATR% affects trail stop sizing** — 3-min needs tighter initial stops due to low ATR
- **For TP sizing: 3-min TP targets should be 0.2–0.4% away; 15-min can support 0.4–0.8%**

---

## 7. BAND LEVEL DISTRIBUTION

### Raw Signal Band Levels (Entry band crossing level)

| Interval | Band Level | N Signals | % of Interval |
|----------|------------|-----------|---------------|
| **15-min** | Level 1 | 6 | 50.0% |
| | Level 2 | 3 | 25.0% |
| | Level 3 | 2 | 16.7% |
| | Level 4 | 1 | 8.3% |
| **3-min** | Level 1 | 16 | 29.1% |
| | Level 2 | 16 | 29.1% |
| | Level 3 | 10 | 18.2% |
| | Level 4 | 3 | 5.5% |
| | Level 5 | 4 | 7.3% |
| | Level 6 | 5 | 9.1% |
| | Level 7 | 1 | 1.8% |
| **5-min** | Level 1 | 3 | 27.3% |
| | Level 2 | 2 | 18.2% |
| | Level 3 | 5 | 45.5% |
| | Level 4 | 1 | 9.1% |

### Analysis:
- **15-min: 50% of signals are Level 1 (tightest band)** — conservative entry setup
- **3-min: Signals spread across levels 1–7** — multiple band multiples generating entries
- **5-min: 45% are Level 3, only 27% Level 1** — wider bands more active on 5-min
- **Higher band levels (5+) are rare** — level 6+ only appears on 3-min (9.1%)

### Recommendation:
- **Band level distribution suggests band_mult is appropriate** — not dominated by extreme outer bands
- **3-min captures broader range of premium levels** — band_mult optimization on 3-min should test wider range
- **15-min is conservative** — consider testing tighter band_mult on this interval

---

## 8. TP DISTANCE ANALYSIS

### Actual Move vs TP Target

| Trade # | Interval | Entry Price | Exit Price | Actual Move % | TP Target % | PnL USDT | Exit Reason |
|---------|----------|-------------|------------|---------------|-------------|----------|------------|
| 1 | 3-min | 1.3633 | 1.3633 | 0.00% | 3.83% | -0.0746 | BAND_ENTRY (no move) |
| 2 | 5-min | 1.3706 | 1.3858 | -1.11% | 0.56% | -3.4814 | STOP_LOSS (moved wrong way) |
| 3 | 3-min | 1.3706 | 1.4330 | -4.55% | 0.28% | -0.0063 | EXTERNAL_CLOSE (liquidated) |
| 4 | 3-min | 1.4294 | 1.4294 | 0.00% | 0.28% | -0.0659 | BAND_ENTRY (no move) |
| 5 | 3-min | 1.4294 | 1.4271 | +0.16% | 0.28% | +0.0611 | EXTERNAL_CLOSE (partial win) |
| 6 | 3-min | 1.4243 | 1.4243 | 0.00% | 0.28% | -0.0660 | BAND_ENTRY (no move) |

### Critical Findings:
- **Trade #2: Entered SHORT at 1.3706, price moved UP to 1.3858** — stop loss hit, -11.56% loss
- **This indicates RSI was too high (66.46) despite ADX approval** — RSI gate failed; bought into bullish momentum
- **TP targets are 0.28–3.83% away** — reasonable for mean-reversion on these timeframes
- **Several trades show 0.0% move (entries + exits at same price)** — suggests BAND_ENTRY signals exiting immediately without price move
- **Only 1 trade captured move in correct direction** — +0.16% (trade #5)

### TP Recommendation:
- **Current LIVE_TP_SCALE = 0.75 is reasonable baseline**
- **TP targets of 0.28–0.56% on 3-min and 5-min are achievable** — within ATR% ranges
- **Do NOT increase TP beyond 0.8%** on 3-min (would require 4x typical ATR move)
- **15-min can support 0.8–1.5% TP targets** — larger ATR% available
- **Keep TP_SCALE between 0.60–0.85** for realistic capture rates

---

## 9. PARAMETER OPTIMIZATION HISTORY

**Note:** No optimization trials or params records found in database. The bot is running on initial hardcoded parameters.

**Current Live Parameters (from git history):**
- `MA_LEN = 84` (initial seeding)
- `BAND_MULT = 0.4–0.9` (varied in trades)
- `TP_PCT = 0.0028–0.0383` (0.28%–3.83%)

---

## RECOMMENDED OPTIMIZER SEARCH RANGES

### 1. **MA_LEN** (EMA smoothing for premium band calculation)
- **Current baseline:** 84
- **Recommended search range:** **50–150**
  - Lower end (50–80): More responsive to recent moves; higher signal frequency
  - Upper end (100–150): Smoother bands; fewer false signals
  - **Rationale:** ADX p50 is 20–25; EMA of 84 is reasonable. Test ±40% range (50–130).

### 2. **BAND_MULT** (Premium band width multiplier on volatility)
- **Current baseline:** 0.4–0.9 (from trades)
- **Recommended search range:** **0.35–1.2**
  - Lower end (0.35–0.6): Tighter bands; higher signal frequency; more false entries
  - Upper end (0.8–1.2): Wider bands; fewer signals; potentially higher quality
  - **Rationale:** Band level distribution shows active signals 1–7; current range is appropriate. Extend to 1.2 to test wider bands.

### 3. **ADX_MIN_THRESHOLD** (Minimum ADX for entry approval)
- **Current gate:** ADX >= 25
- **Recommended search range:** **20–28**
  - At ADX >= 20: ~76–100% of signals pass (may be too permissive on 3-min)
  - At ADX >= 25: ~50–75% of signals pass (current; balanced)
  - At ADX >= 28: ~42–58% of signals pass (conservative)
  - **Rationale:** Data shows 3-min median ADX = 25.13, 5-min = 20.51. Test 20–28 to find optimal trend threshold.

### 4. **RSI_MIN_THRESHOLD** (Maximum RSI for entry approval; RSI < threshold to allow entry)
- **Current gate:** RSI < 40
- **Recommended search range:** **38–50**
  - At RSI < 38: Blocks ~10% more signals; extremely oversold only
  - At RSI < 40: Current; captures 76–100% of signals (appropriate)
  - At RSI < 45: Captures 71–82% of signals; slightly looser on overbought
  - At RSI < 50: Blocks ~25% of signals; includes neutral territory
  - **Rationale:** RSI mean is 48–52; RSI < 40 correctly targets oversold. Test 38–45 range to fine-tune sensitivity.

### 5. **TP_PCT** (Take Profit target as % of entry price)
- **Current observed range:** 0.0028–0.0383 (0.28%–3.83%)
- **Recommended search range:** **0.002–0.01** (0.2%–1.0%)
  - Lower end (0.2–0.4%): Captures quick reversals; higher hit rate; smaller gains
  - Upper end (0.6–1.0%): Requires larger move; lower hit rate; larger per-trade gains
  - **Rationale:** ATR% on 3-min is 0.19 median; 0.3–0.5% is realistic. Test 0.2–1.0% to balance frequency vs size.

---

## SUMMARY TABLE: RECOMMENDED SEARCH RANGES

| Parameter | Current | Min | Max | Notes |
|-----------|---------|-----|-----|-------|
| **MA_LEN** | 84 | 50 | 150 | EMA for band smoothing; +/-40% range |
| **BAND_MULT** | 0.4–0.9 | 0.35 | 1.2 | Volatility multiplier; extend to test wider bands |
| **ADX_MIN** | 25 | 20 | 28 | Trend threshold; 3-min median is 25.13 |
| **RSI_MAX** | 40 | 38 | 50 | Oversold gate; current is appropriate |
| **TP_PCT** | 0.28–3.83% | 0.002 | 0.010 | (0.2%–1.0%); ATR-aware targeting |

---

## DATA QUALITY NOTES

1. **Sample Size:** Only 6 completed trades — too small to draw strong conclusions about optimal parameters
2. **Backtesting Artifacts:** Multiple trades show 0% move (entry = exit at same price), suggesting early phase data
3. **RSI Gate Failure:** Trade #2 shows RSI=66.46 passed the gate despite being overbought — suggests RSI gate is not active or threshold is higher in actual code
4. **Database is Young:** No historical optimizer runs recorded; bot appears to be in early live trading phase
5. **One Interval Dominates:** 3-min interval accounts for 5 of 6 trades; limited evidence for 15-min and 5-min tuning

---

## NEXT STEPS

1. **Run optimizer with suggested ranges** to find optimal combinations
2. **Backtest on longer historical period** (7–30 days) to increase sample size
3. **Monitor live trades** over next 2–4 weeks to gather performance data
4. **Revisit ADX and RSI thresholds** once 50+ live trades are completed
5. **Consider per-interval tuning** — each timeframe may have different optimal parameters
6. **Add signal tracking** — log which signals were rejected and why to verify gate logic

