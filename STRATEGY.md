# Mean Reversion Strategy — Reference Document

**Version**: 8.0
**Package**: `engine/`
**Instrument**: Bybit USDT linear perpetuals (configurable)
**Direction**: SHORT only — no long trades exist or should be added

---

## Purpose of This Document

This file defines the exact trading strategy implemented in this codebase.
**If you are an AI assistant**: do not change any aspect of the strategy logic below unless the user explicitly instructs you to modify it. Treat every detail here as a constraint, not a suggestion.

---

## Strategy Overview

A mean-reversion SHORT strategy. It fades price extensions above a moving-average baseline using EMA-smoothed premium bands. The bot enters short when price touches a premium band and then drops back below it (band crossover), confirming the overextension is reversing. It exits when price reaches a discount band on the other side, or via TP / stop-loss.

---

## Indicators

### Centre Line
```
main = RMA(close, ma_len)
```
`RMA` uses alpha = `1 / ma_len` (Wilder smoothing), seeded at the first close.

### Premium Bands (entry reference — 8 bands above centre)
```
premium_k = EMA(main * (1 + band_mult * 0.01 * k), 5)    for k = 1..8
```
`EMA` uses alpha = `2 / (5 + 1)` (standard EMA with length=5).

### Discount Bands (exit reference — 8 bands below centre)
```
discount_k = EMA(main * (1 - band_mult * 0.01 * k), 5)    for k = 1..8
```

### Gates
```
ADX  = ADX(adx_period)   — Wilder's method; adx_period is optimised (7–21)
RSI  = RSI(rsi_period)   — Wilder's method (avg_gain / avg_loss via RMA); rsi_period is optimised (7–21)
```

---

## Entry Signal (SHORT only)

### Raw Signal
Scan bands **8 → 1** (most extended first). Fire on the highest band that triggers:

```
SIGNAL fires when:
    prev_high >= prev_premium_k   (high was at or above the band last candle)
    AND
    curr_high <  curr_premium_k   (high has dropped back below the band this candle)
```

This is a **band-crossover-above-high**: the premium band crosses above price, meaning price has retreated from the premium zone.

Returns the band level (1–8) that triggered, or 0 for no signal.

### Gates (applied after raw signal)
Both gates must pass for the signal to be accepted:

| Gate | Condition to BLOCK entry | Reason |
|------|--------------------------|--------|
| ADX  | `ADX >= adx_threshold`   | Trending market — mean reversion unreliable; threshold optimised (20–28) |
| RSI  | `RSI < rsi_neutral_lo`   | Already deeply oversold — don't fade exhausted moves; threshold optimised (40–60) |

### Implementation
```python
# indicators.py
compute_entry_signals_raw(current_row, prev_row, current_high, current_low) -> int
resolve_entry_signals(raw_short, adx, rsi) -> int
```

---

## Exit Signal

### Priority Order (strictly enforced — do not reorder)

| Priority | Exit Type | Trigger | Notes |
|----------|-----------|---------|-------|
| 1 | **Liquidation** | mark_high >= liq_price | Bybit isolated SHORT formula; checked in backtest, detected via position=None in live |
| 2 | **Take-Profit** | low <= entry * (1 - tp_pct) | Server-side TP in live (Bybit LastPrice trigger); direct check in backtest. After `TIME_TP_HOURS` (20 h) the tighter **data-driven TP** is substituted — reason logged as `TIME_TP` vs `TP` |
| 3 | **Stop-Loss** | high >= entry * (1 + sl_pct) | Hard stop — wide (default 5%), optimised alongside TP; pre-liquidation guard |
| 4 | **Band Exit** | discount band crossover-above-low | Mirror of entry signal, on discount bands + low |

**This priority order is fixed.** It is implemented identically in the backtester (`backtester.py`), paper trader (`paper_trader.py`), and live trader (`live_trader.py`).

### Same-Bar Exit Recalculation ("After order is filled")

After an entry fires on bar N, all four exit conditions are immediately re-evaluated on the **same bar N** before advancing to bar N+1. This matches TradingView's "Recalculate: After order is filled" execution model.

- **Backtester**: full 4-priority pass using bar N's `low_last`, `high_last`, and `mark_high`; `hold_candles = 0` for same-bar exits
- **Paper trader**: same 4-priority chain re-checked immediately after `_execute_entry()` returns
- **Live trader**: SL and band exit re-checked immediately after `_execute_entry()` (TP and liquidation are server-side and are picked up via `_handle_external_close` on the next candle)

This behaviour is gated by `entry_candle_idx == i` (backtester) and `self.position is not None` after `_execute_entry` (paper/live).

### Band Exit Detail
```
Exit fires when:
    prev_low >= prev_discount_k   (low was at or above the band last candle)
    AND
    curr_low <  curr_discount_k   (low has dropped below the band this candle)
```
Same `crossover()` function as entry, applied to discount bands + LOW. No gates — exits are unconditional on band signal.

```python
# indicators.py
compute_exit_signals_raw(current_row, prev_row, current_low, current_high) -> int
```

---

## Parameters

### Optimised Parameters (12-dimensional search — runs every 12 hours)

#### Entry Parameters

| Parameter | Dataclass | Default | Search Range |
|-----------|-----------|---------|--------------|
| `ma_len` | `EntryParams` | 100 | 2 – 300 (int) |
| `band_mult` | `EntryParams` | 2.5 | 0.3 – 10.0 % (stored ×10 as int during search) |
| `adx_threshold` | `EntryParams` | 25.0 | 20 – 28 (int) |
| `rsi_neutral_lo` | `EntryParams` | 40.0 | 40 – 60 (int) |
| `band_ema_len` | `EntryParams` | 5 | 2 – 15 (int) |
| `adx_period` | `EntryParams` | 14 | 7 – 21 (int) |
| `rsi_period` | `EntryParams` | 14 | 7 – 21 (int) |

#### Exit Parameters

| Parameter | Dataclass | Default | Search Range |
|-----------|-----------|---------|--------------|
| `exit_ma_len` | `ExitParams` | 100 | 2 – 300 (int) |
| `exit_band_mult` | `ExitParams` | 2.5 | 0.3 – 10.0 % (stored ×10 as int during search) |
| `tp_pct` | `ExitParams` | 0.003 | 20 – 100 bp × 0.0001 (0.20% – 1.00%) |
| `sl_pct` | `ExitParams` | 0.05 | 50 – 900 bp × 0.0001 (0.50% – 9.00%) |
| `leverage` | `ExitParams` | 10.0 | 2 – 14 (int) |

### Fixed Constants (never optimised)

| Constant | Value | Location |
|----------|-------|----------|
| `VOL_FILTER_MAX_PCT` | 5.0 % | `constants.py` — skip entry if position notional > 5% of candle USDT volume |
| `LIVE_TP_SCALE` | 1.0 | `constants.py` — server TP matches backtested distance exactly |
| `SIGNAL_DROUGHT_HOURS` | 4.0 | `constants.py` |
| `MAX_LOSS_PCT` | None | `constants.py` (set via `--max-loss` CLI flag) |
| `TIME_TP_HOURS` | 20.0 | `constants.py` |
| `TIME_TP_FALLBACK_PCT` | 0.005 | `constants.py` |
| `TIME_TP_SCALE` | 0.75 | `constants.py` |
| `FEE_RATE` | 0.00055 | `constants.py` |
| `MAKER_FEE_RATE` | 0.0002 | `constants.py` |
| `SLIPPAGE_TICKS` | 1 | `constants.py` |

`VOL_FILTER_MAX_PCT`: if the position notional at the proposed entry price would exceed 5% of the candle's USDT volume, the entry is skipped and logged as a `VOL_FILTER` block.

`LIVE_TP_SCALE`: set to 1.0 — the server-side TP is placed at exactly the backtested distance (no scaling offset).

`SLIPPAGE_TICKS`: applied to all simulated (paper / backtest) fills via `apply_slippage()` in `orders.py`. SHORT entry (sell) receives `price - tick`; cover exit (buy) receives `price + tick`. Live fills rely on Bybit execution.

---

## Optimiser

### Internal Scoring (per-trial ranking)
Within a single optimisation run, results are sorted by: **(n_losses ASC, return_pct DESC)**
Fewest losing trades first; break ties by highest return. No Sharpe-based primary sort.

### Pair Ranking (cross-symbol / cross-interval selection)
After all (symbol, interval) pairs are optimised, they are ranked by:
```
score = pnl_pct / (1 + max_drawdown_pct)
```
Higher score is better. The top-ranked pair per symbol is selected for live or paper trading and displayed in the startup ranking table.

### Exploitation / Exploration Split
- 60% of trials (`EXPLOIT_RATIO`) are sampled near the saved-best params within a radius
- 40% are fully random within the search space

### Acceptance Criteria (re-optimisation)
A new parameter set from re-optimisation is accepted **only if its Monte Carlo score > 0**:
```
mc_score = median(pnl_pct) × P(profitable) / (1 + percentile_95(max_drawdown))
```
If the score is ≤ 0 the old params are kept.

### 30-Day Rolling Windows
Each trial draws a **random contiguous slice** of 5–30 days from the full 30-day seeded dataset (`DAYS_BACK_SEED = 30`). This prevents overfitting to any single time window and improves out-of-sample robustness.

### Volume Filter
Before placing an entry, the bot checks:
```
position_notional = qty × entry_price
max_allowed = candle_volume_usdt × VOL_FILTER_MAX_PCT (5%)
```
If `position_notional > max_allowed`, the entry is vetoed and logged as `VOL_FILTER` in the `events` table. This avoids taking positions that would dominate thin candles.

### Note on `optimise_bayesian`
`optimise_bayesian` exported from `optimizer.py` is an **alias** for `optimise_params`. It is the same random-search engine with exploitation/exploration split — not a Bayesian (TPE) implementation.

### Parallel Trial Execution
The optimiser runs trials in parallel using `ThreadPoolExecutor` (up to `min(cpu_count, 6)` workers). DataFrames are shared as read-only references (no pickling). NumPy operations release the GIL, giving a modest speedup (≈1.5–2×) on multi-core machines.

---

## Live Trading Architecture

### Data Sources
- **Last-price klines** (`/v5/market/kline`): used for all signal generation, TP, stop-loss, and band exit
- **Mark-price klines** (`/v5/market/mark-price-kline`): used only for liquidation price checks
- **Bybit WebSocket** (`wss://stream.bybit.com/v5/public/linear`): live candle stream (`kline.<interval>.<symbol>`) and mark price stream (`tickers.<symbol>`)

### Order Types
- **Entry**: IOC market order (`place_market_order`, side=Sell)
- **Exit**: IOC market order (`place_market_order`, side=Buy, reduceOnly=True)
- **Take-Profit**: Server-side via `set_trading_stop` (LastPrice trigger, tpslMode=Full)

### Warm-Up Period
```
min_candles_required = ma_len + 20
```
No signals are generated until this many candles have been received.

### Re-Optimisation
- Triggered every `REOPT_INTERVAL_SEC` (12 hours) when flat (no open position)
- Runs in a **background daemon thread** — never blocks the WebSocket callback
- Uses `saved_best` exploitation: samples near current params for 60% of trials

### Position Gate
`PositionGate` (`MAX_SLOTS = 1`): only one symbol may hold an open position at a time.

### Status Monitor
`TradingStatusMonitor` (`trading_status.py`) runs in a background daemon thread and prints a full status table every 3 minutes showing: total balance, session P&L, per-symbol trade stats (trades / wins / losses / win rate), open position details (entry, TP, mark price, uPnL), and next re-opt countdown. It never blocks candle processing. Additionally it checks every 10 s for: signal drought (no raw band crossover for `SIGNAL_DROUGHT_HOURS` → prints WARNING banner), and max-loss halt state (session PnL below `MAX_LOSS_PCT` → prints HALT banner with remaining time).

### Reliability Guards
| Guard | Implementation |
|-------|---------------|
| `_refresh_state()` error handling | REST failures keep cached wallet/position and log `REFRESH_STATE_FAILED` event; exits still run |
| `on_closed_candle()` outer wrapper | Entire callback body wrapped in try/except — logs `CANDLE_CALLBACK_ERROR` and returns cleanly so the WebSocket thread never crashes |
| WebSocket exponential backoff | Reconnection delay = `min(5 × 2^attempt, 60)` seconds; resets to 5 s on clean connect |
| Gate release guarantee | `_execute_exit()` releases the `PositionGate` in a `finally` block unconditionally |

### Signal Drought Detection
After every closed candle, if no raw band crossover (`_raw_short > 0`) has been seen for `SIGNAL_DROUGHT_HOURS` (4.0 h), a `SIGNAL_DROUGHT` WARNING event is logged to the DB and the status monitor prints a WARNING banner. A cooldown prevents duplicate events within the same drought window.

### Max-Loss Halt (`--max-loss`)
When `MAX_LOSS_PCT` is set (via `--max-loss N` CLI flag), the bot monitors session P&L on every candle. If session PnL drops below `-MAX_LOSS_PCT%`: any open position is exited immediately, a `MAX_LOSS_HALT` event is logged, and new entries are blocked for **4 hours**. The halt auto-expires — normal trading resumes after the 4-hour window.

### Liquidation Formula (SHORT, isolated margin, USDT linear)
```
LP = Entry + (IM + ExtraMargin - MM) / |Qty|

 IM  = |Qty| × Entry / Leverage          (entry-price based)
 MM  = max(0, |Qty| × Mark × MMR - mmDeduction + |Qty| × Mark × fee_rate)
```
Tier (MMR, mmDeduction) is selected from Bybit's risk-limit table based on position value at mark price.

---

## Paper Trading Architecture

Paper trading uses `PaperTrader` (`paper_trader.py`) instead of `LiveRealTrader`. Key differences:

- No API keys required — uses public REST and WebSocket only
- Virtual wallet starts at `PAPER_STARTING_BALANCE` (default **$500 USDT**)
- All four exit types are simulated locally (liquidation, TP, stop-loss, band)
- Same-bar exit re-check ("After order is filled") implemented — all four exits checked on entry bar
- Slippage (`SLIPPAGE_TICKS = 1`) applied to all fills via `apply_slippage()` in `orders.py`
- Taker and maker fees both simulated using per-symbol fee lookups from `helpers.py`
- After each accepted re-optimisation, backtest trades are written to the `trades` table (`mode='backtest'`) via `bulk_log_backtest_trades()` for chart visualisation

---

## Key Invariants — Do Not Change Without Explicit Instruction

1. **SHORT only.** No long entries. No flip logic.
2. **Exit priority is fixed**: Liquidation → TP → Stop-Loss → Band. This must be identical in `backtester.py`, `paper_trader.py`, and `live_trader.py`. The same-bar re-check after entry must preserve the same priority order.
3. **Hard stop-loss only.** `sl_pct` is optimised alongside `tp_pct`. No trail stop.
4. **Band EMA length is optimised (2–15).** `band_ema_len` is a search dimension, not a fixed constant.
5. **ADX threshold (20–28), RSI threshold (40–60), ADX period (7–21), and RSI period (7–21) are all optimised.** Do not treat them as fixed constants.
6. **The optimiser sorts internally by (n_losses ASC, return_pct DESC).** Pair selection uses `pnl_pct / (1 + max_drawdown_pct)`. Do not conflate these two scoring steps.
7. **Last-price for signals, mark-price for liquidation.** This split must be preserved in the backtester.
8. **`_maybe_reoptimise` must never block the WebSocket thread.** It spawns `_run_reoptimise` as a daemon thread.
9. **Gate must always be released when a position closes**, including on early returns and exceptions inside `_execute_exit`.
10. **`LIVE_TP_SCALE = 1.0`** — the server-side TP is placed at exactly the backtested distance (no scaling).
11. **`SLIPPAGE_TICKS = 1`** — applies to paper/backtest fills only; live fills rely on Bybit execution.
12. **Data-driven time TP** — after `TIME_TP_HOURS` (20 h) of holding, `compute_time_tp_pct()` queries the top-3 profitable 20h+ exits from the DB, averages their TP%, scales by `TIME_TP_SCALE` (0.75), and substitutes the result as the active TP. Falls back to `TIME_TP_FALLBACK_PCT` (0.5%) if fewer than 3 qualifying trades exist. Exit reason is `TIME_TP` (not `TP`) when this fires.
13. **SQLite only — no CSV or log files.** All trade data, signals, orders, optimisation runs, events, and diagnostics are written exclusively to `data/trading.db`. `csv_append` and `ensure_csv` in `logger.py` are permanent no-ops.
14. **DB maintenance runs automatically.** `run_maintenance()` is called at startup (no VACUUM) and every 24 hours (full VACUUM) by a daemon thread in `main.py`. Each table has a defined retention period; stale rows are pruned before the WAL checkpoint and ANALYZE pass.
15. **`_maybe_reoptimise` must never block the WebSocket thread — and must not start if `_refresh_state()` just failed.** If `_refresh_failed` is set, skip the re-opt trigger for that candle.
16. **Max-loss halts are temporary (4 hours), not permanent.** After `4 × 3600` seconds the `_halted` flag auto-clears and entries resume normally.
17. **Config validation at startup.** `_validate_config()` in `main.py` checks leverage (1–100), symbols format (`\w+USDT`), intervals (subset of {1,3,5,15,30,60}), `days_back > 0`, `trials > 0`. Invalid config raises `SystemExit` with a clear message.

---

## File Map

| File | Role |
|------|------|
| `engine/core/indicators.py` | All indicator maths: RMA, EMA, ATR, ADX, RSI, band construction, crossover detection, signal generation |
| `engine/core/orders.py` | Slippage simulation: `apply_slippage(price, side)` — 1 tick applied to all simulated fills |
| `engine/backtest/backtester.py` | Historical backtest engine; single run and Monte Carlo |
| `engine/optimize/optimizer.py` | Random-search optimiser over the 12-parameter space; `optimise_bayesian` is an alias for `optimise_params`; 30-day rolling windows; volume filter |
| `engine/trading/live_trader.py` | Live trading engine: WebSocket candle processing, entry/exit execution, re-optimisation |
| `engine/trading/paper_trader.py` | Paper trading engine: simulates fills, fees, slippage, liquidation locally using public data |
| `engine/trading/bybit_client.py` | Bybit REST + WebSocket client, order placement, execution polling |
| `engine/trading/liquidation.py` | Exact Bybit isolated SHORT liquidation price formula |
| `engine/utils/api_key_prompt.py` | Interactive API credential setup with hidden input; saves/loads `~/.bybit_credentials.json` |
| `engine/utils/constants.py` | All configuration constants and defaults |
| `engine/utils/data_structures.py` | `EntryParams`, `ExitParams`, `TradeRecord` (includes `entry_ts_ms`/`exit_ts_ms` for chart marker placement), `BacktestResult`, `MCSimResult`, `RealPosition`, `PendingSignal` |
| `engine/utils/helpers.py` | Rate limiter, interval parsing, fee/leverage lookups |
| `engine/utils/db_logger.py` | SQLite WAL singleton logger — all DB writes, `bulk_log_backtest_trades()` (writes mode='backtest' trade rows after each accepted reopt), `compute_time_tp_pct()`, `run_maintenance()` |
| `engine/utils/logger.py` | Colour-coded console order logger (`log_order`); `csv_append` / `ensure_csv` are no-ops — all persistence is via SQLite |
| `engine/utils/plotting.py` | ASCII equity-curve chart (`plot_pnl_chart`) and Monte Carlo terminal report (`print_monte_carlo_report`) |
| `engine/utils/position_gate.py` | Thread-safe slot gate (MAX_SLOTS=1) |
| `engine/utils/trading_status.py` | `TradingStatusMonitor`: background daemon printing full status tables every 3 minutes |
| `main.py` | CLI entry point: download → optimise → rank → live or paper trade; `--max-loss` flag; `_validate_config()` |
| `gui.py` | CustomTkinter GUI: Agent Analysis, Activity, and 📈 Equity tabs; re-opt countdown; band level display; SQLite trade loading |
| `tests/test_indicators.py` | Unit tests for RMA, EMA, crossover, entry/exit signal functions |
| `tests/test_orders.py` | Unit tests for slippage application |
| `tests/test_backtester.py` | Smoke and regression tests for `backtest_once()` |
| `scripts/run_analysis.py` | Standalone scheduled analysis: multi-interval optimise, ranked report, saves best params to `data/best_params.json` and patches `default_config.json` |
| `web/server.py` | FastAPI chart server (read-only DB access) — REST endpoints (`/api/history`, `/api/trades`, `/api/symbols`, `/api/ready`) + WebSocket live push; `_db_is_ready()` polls `candle_analytics` row count |
| `web/static/index.html` | TradingView Lightweight Charts frontend — candlestick, MA+bands overlay, live/paper/backtest trade markers, loading overlay, symbol switcher |
| `.claude/agents/market-analyst.md` | Claude agent: runs optimisation, signal scan, param warm-start, saves results |
| `.claude/agents/trade-analyst.md` | Claude agent: queries `data/trading.db` for trades, signals, events, diagnostics |
