-- ============================================================================
-- Candle Analytics Analysis SQL Queries
-- Generated: 2026-03-13
-- Database: /Users/partyproper/Documents/Mean Reversion Trader/data/trading.db
-- ============================================================================

-- QUERY 1: ADX Distribution with Percentiles
-- Shows ADX min/max/avg/p10/p25/p50/p75/p90 per interval
-- Used to determine ADX gate sensitivity and distribution health

WITH adx_data AS (
  SELECT interval, adx,
    ROW_NUMBER() OVER (PARTITION BY interval ORDER BY adx) as rn,
    COUNT(*) OVER (PARTITION BY interval) as cnt
  FROM candle_analytics
  WHERE adx IS NOT NULL
)
SELECT 
  interval,
  COUNT(*) as n,
  MIN(adx) as adx_min,
  ROUND(AVG(adx),2) as adx_avg,
  MAX(adx) as adx_max,
  ROUND((SELECT adx FROM adx_data a2 WHERE a2.interval = adx_data.interval AND a2.rn = CEIL(a2.cnt * 0.10) LIMIT 1),2) as adx_p10,
  ROUND((SELECT adx FROM adx_data a2 WHERE a2.interval = adx_data.interval AND a2.rn = CEIL(a2.cnt * 0.25) LIMIT 1),2) as adx_p25,
  ROUND((SELECT adx FROM adx_data a2 WHERE a2.interval = adx_data.interval AND a2.rn = CEIL(a2.cnt * 0.50) LIMIT 1),2) as adx_p50,
  ROUND((SELECT adx FROM adx_data a2 WHERE a2.interval = adx_data.interval AND a2.rn = CEIL(a2.cnt * 0.75) LIMIT 1),2) as adx_p75,
  ROUND((SELECT adx FROM adx_data a2 WHERE a2.interval = adx_data.interval AND a2.rn = CEIL(a2.cnt * 0.90) LIMIT 1),2) as adx_p90
FROM adx_data
GROUP BY interval
ORDER BY interval;

-- ============================================================================

-- QUERY 2: RSI Distribution with Percentiles
-- Shows RSI min/max/avg/p10/p25/p50/p75/p90 per interval
-- Used to determine RSI gate sensitivity

WITH rsi_data AS (
  SELECT interval, rsi,
    ROW_NUMBER() OVER (PARTITION BY interval ORDER BY rsi) as rn,
    COUNT(*) OVER (PARTITION BY interval) as cnt
  FROM candle_analytics
  WHERE rsi IS NOT NULL
)
SELECT 
  interval,
  COUNT(*) as n,
  MIN(rsi) as rsi_min,
  ROUND(AVG(rsi),2) as rsi_avg,
  MAX(rsi) as rsi_max,
  ROUND((SELECT rsi FROM rsi_data a2 WHERE a2.interval = rsi_data.interval AND a2.rn = CEIL(a2.cnt * 0.10) LIMIT 1),2) as rsi_p10,
  ROUND((SELECT rsi FROM rsi_data a2 WHERE a2.interval = rsi_data.interval AND a2.rn = CEIL(a2.cnt * 0.25) LIMIT 1),2) as rsi_p25,
  ROUND((SELECT rsi FROM rsi_data a2 WHERE a2.interval = rsi_data.interval AND a2.rn = CEIL(a2.cnt * 0.50) LIMIT 1),2) as rsi_p50,
  ROUND((SELECT rsi FROM rsi_data a2 WHERE a2.interval = rsi_data.interval AND a2.rn = CEIL(a2.cnt * 0.75) LIMIT 1),2) as rsi_p75,
  ROUND((SELECT rsi FROM rsi_data a2 WHERE a2.interval = rsi_data.interval AND a2.rn = CEIL(a2.cnt * 0.90) LIMIT 1),2) as rsi_p90
FROM rsi_data
GROUP BY interval
ORDER BY interval;

-- ============================================================================

-- QUERY 3: ADX Threshold Sensitivity
-- Shows how many raw band signals would pass at each ADX threshold
-- Critical for determining optimal ADX gate range

SELECT interval,
  COUNT(*) as total_raw_signals,
  SUM(CASE WHEN adx >= 20 THEN 1 ELSE 0 END) as would_pass_adx_20,
  SUM(CASE WHEN adx >= 22 THEN 1 ELSE 0 END) as would_pass_adx_22,
  SUM(CASE WHEN adx >= 25 THEN 1 ELSE 0 END) as would_pass_adx_25,
  SUM(CASE WHEN adx >= 28 THEN 1 ELSE 0 END) as would_pass_adx_28,
  SUM(CASE WHEN adx >= 30 THEN 1 ELSE 0 END) as would_pass_adx_30,
  SUM(CASE WHEN adx >= 32 THEN 1 ELSE 0 END) as would_pass_adx_32
FROM signals
WHERE raw_band_level > 0 AND adx IS NOT NULL
GROUP BY interval
ORDER BY interval;

-- ============================================================================

-- QUERY 4: RSI Threshold Sensitivity
-- Shows how many raw band signals would pass at each RSI threshold
-- RSI < threshold allows entry; >= threshold blocks

SELECT interval,
  COUNT(*) as total_raw_signals,
  SUM(CASE WHEN rsi < 40 THEN 1 ELSE 0 END) as would_pass_rsi_40,
  SUM(CASE WHEN rsi < 45 THEN 1 ELSE 0 END) as would_pass_rsi_45,
  SUM(CASE WHEN rsi < 50 THEN 1 ELSE 0 END) as would_pass_rsi_50,
  SUM(CASE WHEN rsi < 55 THEN 1 ELSE 0 END) as would_pass_rsi_55,
  SUM(CASE WHEN rsi < 60 THEN 1 ELSE 0 END) as would_pass_rsi_60
FROM signals
WHERE raw_band_level > 0 AND rsi IS NOT NULL
GROUP BY interval
ORDER BY interval;

-- ============================================================================

-- QUERY 5: Trade Outcomes with ADX and RSI at Entry
-- Matches completed trades to candle_analytics at entry time
-- Used to analyze whether trades at certain ADX/RSI levels are profitable

SELECT 
  t.ts_utc,
  t.symbol,
  t.interval,
  ROUND(t.entry_price, 5) as entry_price,
  ROUND(ca.adx, 2) as adx_at_entry,
  ROUND(ca.rsi, 2) as rsi_at_entry,
  ROUND(t.pnl_net, 4) as pnl_net,
  t.result,
  ROUND(t.pnl_pct, 4) as pnl_pct
FROM trades t
LEFT JOIN candle_analytics ca 
  ON ca.ts_utc = t.ts_utc 
  AND ca.symbol = t.symbol 
  AND ca.interval = t.interval
ORDER BY t.ts_utc;

-- ============================================================================

-- QUERY 6: TP Distance Analysis
-- For each trade: compares actual price move to TP target distance
-- Shows whether TP targets are realistic

SELECT 
  ts_utc,
  symbol,
  interval,
  ROUND(entry_price, 5) as entry_px,
  ROUND(fill_price, 5) as exit_px,
  ROUND((entry_price - fill_price) / entry_price * 100, 4) as actual_move_pct,
  ROUND(tp_pct * 100, 4) as tp_target_pct,
  ROUND(pnl_net, 4) as pnl_net,
  result,
  reason
FROM trades
ORDER BY ts_utc;

-- ============================================================================

-- QUERY 7: Volume Ratio and ATR% Distribution
-- Shows volatility profiles per interval
-- Used to calibrate position sizing and TP distances

WITH vol_atr_stats AS (
  SELECT 
    interval,
    volume_ratio,
    atr_pct,
    ROW_NUMBER() OVER (PARTITION BY interval ORDER BY volume_ratio) as vol_rn,
    COUNT(*) OVER (PARTITION BY interval) as vol_cnt,
    ROW_NUMBER() OVER (PARTITION BY interval ORDER BY atr_pct) as atr_rn,
    COUNT(*) OVER (PARTITION BY interval) as atr_cnt
  FROM candle_analytics
  WHERE volume_ratio IS NOT NULL AND atr_pct IS NOT NULL
)
SELECT 
  interval,
  COUNT(*) as n,
  ROUND(MIN(volume_ratio), 4) as vol_min,
  ROUND(AVG(volume_ratio), 4) as vol_avg,
  ROUND(MAX(volume_ratio), 4) as vol_max,
  ROUND((SELECT volume_ratio FROM vol_atr_stats v2 WHERE v2.interval = vol_atr_stats.interval AND v2.vol_rn = CEIL(v2.vol_cnt * 0.25) LIMIT 1), 4) as vol_p25,
  ROUND((SELECT volume_ratio FROM vol_atr_stats v2 WHERE v2.interval = vol_atr_stats.interval AND v2.vol_rn = CEIL(v2.vol_cnt * 0.50) LIMIT 1), 4) as vol_p50,
  ROUND((SELECT volume_ratio FROM vol_atr_stats v2 WHERE v2.interval = vol_atr_stats.interval AND v2.vol_rn = CEIL(v2.vol_cnt * 0.75) LIMIT 1), 4) as vol_p75,
  ROUND(MIN(atr_pct), 4) as atr_min,
  ROUND(AVG(atr_pct), 4) as atr_avg,
  ROUND(MAX(atr_pct), 4) as atr_max,
  ROUND((SELECT atr_pct FROM vol_atr_stats v2 WHERE v2.interval = vol_atr_stats.interval AND v2.atr_rn = CEIL(v2.atr_cnt * 0.25) LIMIT 1), 4) as atr_p25,
  ROUND((SELECT atr_pct FROM vol_atr_stats v2 WHERE v2.interval = vol_atr_stats.interval AND v2.atr_rn = CEIL(v2.atr_cnt * 0.50) LIMIT 1), 4) as atr_p50,
  ROUND((SELECT atr_pct FROM vol_atr_stats v2 WHERE v2.interval = vol_atr_stats.interval AND v2.atr_rn = CEIL(v2.atr_cnt * 0.75) LIMIT 1), 4) as atr_p75
FROM vol_atr_stats
GROUP BY interval
ORDER BY interval;

-- ============================================================================

-- QUERY 8: Band Level Distribution
-- Shows what proportion of signals occur at each premium band level
-- Used to verify band_mult range is appropriate

SELECT 
  interval,
  raw_band_level,
  COUNT(*) as n_signals,
  ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (PARTITION BY interval), 2) as pct_of_interval
FROM signals
WHERE raw_band_level > 0
GROUP BY interval, raw_band_level
ORDER BY interval, raw_band_level;

-- ============================================================================

-- QUERY 9: Row Counts Across All Tables
-- Diagnostic query to verify data completeness

SELECT 'candles' as tbl, COUNT(*) as row_count FROM candles
UNION ALL
SELECT 'candle_analytics', COUNT(*) FROM candle_analytics
UNION ALL
SELECT 'signals', COUNT(*) FROM signals
UNION ALL
SELECT 'trades', COUNT(*) FROM trades
UNION ALL
SELECT 'orders', COUNT(*) FROM orders
UNION ALL
SELECT 'events', COUNT(*) FROM events
UNION ALL
SELECT 'params', COUNT(*) FROM params
UNION ALL
SELECT 'optimization_runs', COUNT(*) FROM optimization_runs
UNION ALL
SELECT 'optimization_trials', COUNT(*) FROM optimization_trials;

-- ============================================================================
