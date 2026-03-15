# Mean Reversion Trader

An automated LONG-only spot margin mean-reversion trading bot for Bybit. Buys bounces off EMA-smoothed discount bands below the RMA centre line, with an integrated parameter optimiser that re-tunes itself every 12 hours.

Symbols, leverage, intervals, and all strategy parameters are fully configurable — either through the GUI settings panel, a JSON config file, or `engine/utils/constants.py`.

All trade data, optimisation runs, signals, events, and diagnostics are written to a single SQLite database (`data/trading.db`). No CSV or log files are used.
<img width="966" height="840" alt="Screenshot 2026-03-05 at 9 25 55 PM" src="https://github.com/user-attachments/assets/97fda080-b9a0-4ddc-af59-9377b9c57a8f" />

---

## Requirements

### Python Version

**Python 3.11 or later** is required. The project is developed on **Python 3.14**.

Download Python from [python.org](https://www.python.org/downloads/).

### Dependencies

Install all runtime dependencies with:

```bash
pip install -r requirements.txt
```

| Package | Purpose |
|---------|---------|
| `pandas` | Candle DataFrames, indicator calculations |
| `numpy` | Numerical operations in backtester and optimiser |
| `requests` | Bybit REST API calls |
| `websocket-client` | Bybit WebSocket live feed |
| `tqdm` | Progress bar during optimisation |
| `customtkinter` | GUI (not needed if running CLI only) |
| `colorama` | ANSI colour output on Windows (optional on macOS/Linux) |
| `fastapi` | Chart web server (served on `127.0.0.1:8765`) |
| `uvicorn` | ASGI server for the chart backend |

To build a standalone executable (optional):

```bash
pip install -r requirements-build.txt  # installs PyInstaller
python build.py          # GUI build
python build.py --cli    # CLI-only build
```

---

## Setup

### 1. Clone / download the project

```
Mean Reversion Trader/
├── main.py                  # CLI entry point
├── gui.py                   # GUI entry point (scrollable CustomTkinter)
├── build.py                 # PyInstaller build script
├── requirements.txt
├── requirements-build.txt
├── STRATEGY.md              # Full strategy specification
├── scripts/
│   └── run_analysis.py      # Standalone scheduled analysis (saves best params)
├── .claude/
│   └── agents/
│       ├── market-analyst.md  # Claude agent: multi-interval optimisation
│       └── trade-analyst.md   # Claude agent: DB/signal diagnostics
├── web/                     # TradingView Lightweight Charts UI
│   ├── server.py            # FastAPI server (REST + WebSocket, read-only DB access)
│   └── static/
│       └── index.html       # Chart frontend (candlestick + bands + trade markers)
└── engine/                  # Core package
    ├── core/
    │   ├── indicators.py        # All indicator maths
    │   └── orders.py            # Slippage simulation
    ├── backtest/
    │   └── backtester.py        # Historical backtest + Monte Carlo engine
    ├── optimize/
    │   └── optimizer.py         # Random-search parameter optimiser
    ├── trading/
    │   ├── bybit_client.py      # Bybit REST + WebSocket client
    │   ├── live_trader.py       # Live trading engine
    │   └── paper_trader.py      # Paper trading engine
    ├── utils/
    │   ├── api_key_prompt.py    # Interactive API credential setup
    │   ├── constants.py         # All configuration constants
    │   ├── data_structures.py   # Shared dataclasses
    │   ├── db_logger.py         # SQLite logger — all DB writes + maintenance
    │   ├── helpers.py           # Rate limiter, interval parsing, fee lookups
    │   ├── logger.py            # Colour-coded console order logger (no file I/O)
    │   ├── plotting.py          # ASCII equity chart + Monte Carlo report
    │   ├── position_gate.py     # Thread-safe slot gate (MAX_SLOTS=1)
    │   └── trading_status.py    # Real-time status monitor (periodic display)
    └── config/
        └── default_config.json  # Default configuration (patched by agent after analysis)
```

### 2. Create and activate a virtual environment (recommended)

```bash
python3 -m venv venv

# macOS / Linux
source venv/bin/activate

# Windows
venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Set your Bybit API credentials (live trading only)

The bot needs a Bybit API key with **Spot trading** permissions. Read-only is not sufficient — order placement is required. Paper trading requires no API keys.

**Option A — environment variables (recommended)**

```bash
# macOS / Linux
export BYBIT_API_KEY="your_key_here"
export BYBIT_API_SECRET="your_secret_here"

# Windows PowerShell
$env:BYBIT_API_KEY="your_key_here"
$env:BYBIT_API_SECRET="your_secret_here"
```

**Option B — interactive prompt**

Run the bot without setting environment variables. It will prompt you once (with hidden input), then save the credentials to `~/.bybit_credentials.json` (chmod 600 on macOS/Linux) for future runs.

> Create API keys at: https://www.bybit.com/en/user-center/account-api

---

## Running the Bot

### GUI

```bash
python gui.py
```

The GUI is designed to fit a standard 13-inch screen (~1280×832 px) without scrolling. The API Credentials and Settings panels are **collapsible** — click the toggle to expand or hide them, keeping the core trading view on screen at all times.

#### Settings panel (click **Settings ▼** to expand)

| Setting | Description |
|---------|-------------|
| Risk Profile | Max wallet fraction used as margin per trade (10–95%) |
| Testing Period | Days of candle history used for optimisation (2–90 days) |
| Number of Tests | Optimiser trials per symbol/interval pair |
| Intervals | Candle sizes to test (1m, 3m, 5m, 15m, 30m, 60m) |
| Symbols | Comma-separated list of Bybit spot margin symbols for **LIVE** mode. Paper mode runs all configured `PAPER_SYMBOLS` (default: XRPUSDT only) independently |
| Leverage | Initial default leverage. The optimiser searches discrete spot margin values (2×, 3×, 4×, 8×, 10×) and may override this; live mode reads the confirmed setting from Bybit |

Every setting has a dedicated **Apply** button. Changes take effect only when Apply is clicked; they are also locked in automatically when **START** is pressed.

#### Stat cards (always visible while running)

| Card | Description |
|------|-------------|
| Account Balance | Current wallet balance (USDT) |
| P&L | Realized session P&L and account-level P&L |
| Win Rate | Win percentage across all closed trades |
| Total Trades | Number of completed trades this session |
| Leverage | Confirmed leverage in use — fetched from Bybit in live mode, set from Settings in paper mode |

#### Bottom tab panel

The bottom of the GUI has three tabs:

| Tab | Description |
|-----|-------------|
| 🤖 Agent Analysis | Real-time feed of the Claude agent's work — per-interval optimisation results, warm-start notice, best params selection, and trading start confirmation. Switches to this tab automatically during analysis. |
| Activity | Raw bot log — market data download, leverage check, re-opt events, stop/error messages. Switches to this tab automatically once trading begins. |
| 📈 Equity | Wallet equity history loaded from `balance_snapshots` in the SQLite DB, with a mini ASCII sparkline and session P&L summary. Updates every 10 s while the bot is running. |

The **Best Strategy** panel also shows a **"Next re-optimisation in Xh Ym"** countdown, and the **Last Signal** panel shows the **Band level** (1–8) that triggered the signal.

Use the **LIVE / PAPER** toggle to switch modes, then press **START**.

### CLI — Live trading

```bash
python main.py
```

With optional arguments:

```bash
# Use a custom config file
python main.py --config path/to/config.json

# Override trading symbols
python main.py --symbols XRPUSDT ETHUSDT

# Paper trading (no API keys required)
python main.py --paper

# Paper trading with specific symbols
python main.py --paper --symbols XRPUSDT ETHUSDT

# Halt trading for 4 hours if session P&L drops by 5%
python main.py --max-loss 5

# Combined: paper trading with a 3% max-loss guard
python main.py --paper --max-loss 3
```

### Live trading startup sequence

1. Load and validate API credentials (env vars → saved file → interactive prompt)
2. Read actual leverage from Bybit for each symbol; log it and warn if it differs from the GUI/config setting
3. Download seed candle history for each (symbol, interval) pair
4. Run the 12-dim parameter optimiser (~4 000 backtest trials per pair, each on a random 5–30-day window); each pair logs `Testing {sym} {iv}m — leverage = Nx`
5. Rank all pairs by score: `PnL% / (1 + max_drawdown%)`; launch the top-ranked trader per symbol
6. Connect to Bybit WebSocket and begin live trading
7. `TradingStatusMonitor` prints a full status table every 3 minutes
8. Browser opens automatically to the chart UI once `candle_analytics` has data (first optimisation complete)

### Paper trading startup sequence

1. Download public candle history for all paper symbols (no API keys needed)
2. Run the parameter optimiser for each (symbol, interval) pair across all configured `PAPER_SYMBOLS` (default: XRPUSDT)
3. Rank all pairs by score; launch the top-ranked paper trader **for each symbol independently**
4. All symbol traders run concurrently — each has its own position gate, so they never block one another
5. Connect to Bybit public WebSocket for live candle data
6. Simulate fills, PnL, fees, slippage, and liquidation using the spot margin LONG formula
7. Virtual starting wallet: **$500 USDT** per symbol

---

## Live Chart UI

A TradingView Lightweight Charts candlestick chart is served at `http://127.0.0.1:8765` and opens automatically in the browser once the first optimisation completes and candle analytics data is available in the DB.

The chart is read-only — it never writes to the database.

### Features

| Feature | Description |
|---------|-------------|
| Candlestick chart | 10 000 candles of OHLCV history with live WebSocket updates |
| MA + 8 premium/discount bands | RMA centre line and all 8 discount (entry) and premium (exit) bands overlaid |
| Live/paper trade markers | Entry → green ▲ arrow; exit → red ▼ arrow. Label shows fill price and result |
| Backtest trade markers | Semi-transparent circles at entry/exit timestamps from the most recent accepted optimisation run |
| Loading overlay | Shows "Waiting for first optimisation…" until `candle_analytics` has data; auto-dismisses |
| Symbol + interval switcher | Dropdown to switch between any (symbol, interval) pair in the DB |
| Legend | Colour-coded band and marker legend in the top-left corner |

### API endpoints (read-only)

| Endpoint | Description |
|----------|-------------|
| `GET /api/ready` | Returns `{"ready": true}` once `candle_analytics` has at least one row |
| `GET /api/history?symbol=XRPUSDT&interval=15&limit=10000` | Historical OHLCV + band data |
| `GET /api/trades?symbol=XRPUSDT&interval=15` | Last 500 trades (all modes) |
| `GET /api/symbols` | All (symbol, interval) pairs present in the DB |
| `WS /ws?symbol=XRPUSDT&interval=15` | Live candle and trade push (excludes backtest trades) |

---

## Configuration

Default settings live in `engine/utils/constants.py`. You can override most of them with a JSON config file:

```json
{
  "symbol":          "XRPUSDT",
  "leverage":        10.0,
  "starting_wallet": 100.0,
  "entry": {
    "ma_len":    100,
    "band_mult": 2.5
  },
  "exit": {
    "tp_pct": 0.0028
  },
  "optimizer": {
    "n_trials": 4000
  }
}
```

Place the file anywhere and pass it with `--config`.

> **Note:** In the **GUI**, the `"symbol"` key in the config file only sets the initial default displayed in the Symbols field. The GUI Symbols field (and its Apply button) is the active source of truth for both LIVE and PAPER modes. In the **CLI**, `"symbol"` in the config sets the active symbol unless overridden by `--symbols`.

### Key constants

| Constant | Default | Description |
|----------|---------|-------------|
| `SYMBOLS` | `["XRPUSDT"]` | Active symbols for **LIVE** mode; overridden by the GUI Symbols field or `--symbols` CLI flag. Paper mode uses `PAPER_SYMBOLS` instead |
| `CANDLE_INTERVALS` | `["1","3","5"]` | Candle sizes (minutes) tested during optimisation |
| `DEFAULT_LEVERAGE` | `3.0` | Initial fallback leverage (overridden by the optimiser on first run); spot margin supports 2×, 3×, 4×, 8×, 10× |
| `STARTING_WALLET` | `100.0` | Simulated wallet used by the backtester and optimiser (not the paper trading wallet) |
| `PAPER_STARTING_BALANCE` | `500.0` | Virtual wallet size for paper trading (USDT) |
| `MAX_SYMBOL_FRACTION` | `0.45` | Max wallet fraction used as margin per trade |
| `MAX_ACTIVE_SYMBOLS` | `1` | Maximum simultaneous traders in **LIVE** mode (one per symbol). Paper mode launches all configured `PAPER_SYMBOLS` independently (default: XRPUSDT only) |
| `DAYS_BACK_SEED` | `30` | Days of history downloaded for seed; each optimiser trial uses a random 5–30-day slice |
| `INIT_TRIALS` | `4000` | Optimiser trials at startup |
| `REOPT_INTERVAL_SEC` | `43200` | Re-optimise every **12 hours** when flat |
| `LIVE_TP_SCALE` | `1.0` | Server-side TP is placed at exactly the backtested distance |
| `VOL_FILTER_MAX_PCT` | `5.0` | Entry vetoed if position notional > 5% of candle USDT volume |
| `TIME_TP_HOURS` | `20.0` | Hours after entry before the data-driven time TP kicks in |
| `TIME_TP_FALLBACK_PCT` | `0.005` | 0.5% fallback TP when the DB has insufficient trade history |
| `TIME_TP_SCALE` | `0.75` | Scale factor applied to the data-driven average TP % |
| `FEE_RATE` | `0.00055` | Bybit taker fee rate |
| `MAKER_FEE_RATE` | `0.0002` | Bybit maker fee rate |
| `SLIPPAGE_TICKS` | `1` | Slippage ticks applied to simulated fills (paper and backtest only) |
| `RANDOM_SEED` | `None` | Optimiser RNG seed — `None` for non-deterministic runs; set to an integer for reproducible results |
| `SIGNAL_DROUGHT_HOURS` | `4.0` | Hours without any raw band crossover before a WARNING event is logged and the status monitor alerts |
| `MAX_LOSS_PCT` | `None` | Session P&L drawdown threshold for the 4-hour trading halt (set via `--max-loss N`; `None` = disabled) |

---

## Strategy Summary

**LONG only — no short trades.**

The bot enters long when price touches a discount band below the RMA centre line and then bounces back above it (band crossover), signalling a mean-reversion recovery. Two gates must pass:

- **ADX < adx_threshold** — market must be range-bound (threshold optimised: 20–28)
- **RSI ≤ rsi_neutral_lo** — close must confirm oversold/neutral (threshold optimised: 40–60)

**Exit priority (strictly ordered):**

| # | Type | Trigger |
|---|------|---------|
| 0 | Liquidation | low ≤ liq_price — spot margin LONG formula: `entry × (lev−1) / (lev × (1−MMR))` |
| 1 | Take-Profit | high ≥ entry × (1 + tp_pct) |
| 2 | Stop-Loss | low ≤ entry × (1 − sl_pct) — wide guard (default 5%) |
| 3 | Band Exit | high crosses above a premium band (mirrors entry logic on the premium side) |

In **live** mode, TP is handled server-side by Bybit; stop-loss and band exits are checked on every closed candle. In **paper** mode, all four exits are simulated locally. Slippage (1 tick) is applied to all simulated fills. Trail stop is disabled (`TRAIL_STOP_PCT = 0.0`).

### TradingView Execution Model

The bot uses the same execution model as TradingView's strategy tester with these settings enabled:

- **Fill orders: On bar close** — entry and exit fills use the closing price (with slippage)
- **On every tick** — intrabar high/low are used for TP, SL, and liquidation checks
- **Recalculate: After order is filled** — after an entry fires on bar N, all four exit conditions are immediately re-evaluated on the same bar N before advancing to bar N+1

The "after order is filled" same-bar re-check is implemented identically in the backtester and live trader.

The optimiser searches **12 parameters** — entry MA length, entry band multiplier, ADX threshold, RSI threshold, band EMA length, ADX period, RSI period, exit MA length, exit band multiplier, TP %, SL %, and leverage — and re-runs every **12 hours** in a background thread so live candle processing is never blocked. Each trial uses a random 5–30-day slice of the 30-day seeded dataset to prevent window overfitting. A volume filter (5% of candle USDT volume) vetoes entries on thin candles. All (symbol, interval) pairs are ranked by `score = PnL% / (1 + max_drawdown%)` and the top-ranked pair per symbol is selected for trading.

See **`STRATEGY.md`** for the complete specification including exact formulas and all invariants.

---

## Claude Agent System

The bot integrates two Claude agents (`.claude/agents/`). They require the Claude Code CLI to be installed locally — they are never called from the live trading loop.

### market-analyst
Invoked manually (or on a cron schedule) to run a full multi-interval optimisation, print a ranked report, and **save the best params** back to `data/best_params.json` and `engine/config/default_config.json`. The bot reads these params at the next startup and warm-starts its own optimiser from them (60% of trials exploit the proven region).

```bash
# One-shot analysis for the symbol in config
python scripts/run_analysis.py

# Override symbol, days, and trial count
python scripts/run_analysis.py --symbol ETHUSDT --days 3 --trials 8000

# Loop every 8 hours
python scripts/run_analysis.py --loop --hours 8
```

Ask the Claude Code CLI directly:
```
claude "optimise for XRPUSDT" --agent market-analyst
claude "scan last 3 days for ETHUSDT" --agent market-analyst
```

### trade-analyst
Queries `data/trading.db` to answer runtime questions about trades, signals, blocked entries, optimizer runs, and events. Includes four diagnostic queries:

- **Parameter drift**: compares MA/BandMult/TP across last 5 optimisation runs
- **Losing trade patterns**: groups completed trades by exit reason to identify the costliest exit type
- **Signal drought check**: shows the most recent signal row and flags if it's older than 4 hours
- **Hourly performance**: groups trades by UTC hour to identify the best and worst trading periods

```
claude "why aren't signals firing?" --agent trade-analyst
claude "show me recent trades" --agent trade-analyst
claude "analyse my losing trades" --agent trade-analyst
claude "what hours perform best?" --agent trade-analyst
```

---

## Logs and Output

All data is written to a single SQLite database — no CSV or log files are created.

| Path | Contents |
|------|---------|
| `data/trading.db` | SQLite database (WAL mode) containing all tables below |
| `data/best_params.json` | Latest best params saved by the market-analyst agent |

### Database Tables

| Table | Retention | Contents |
|-------|-----------|---------|
| `trades` | 365 days | Every entry and exit: fill price, PnL, fees, slippage, exit reason. Mode column distinguishes `live`, `paper`, and `backtest` rows |
| `orders` | 90 days | Raw order placement log with side, qty, price, status |
| `params` | 365 days | Parameter set in use after each optimisation run |
| `signals` | 30 days | Every entry signal: raw band level, final level, blocked_by reason |
| `candles` | 30 days | Closed candle OHLCV |
| `candle_analytics` | 30 days | 53-column per-candle diagnostics (bands, ADX, RSI, HV, ATR, etc.) |
| `positions` | 30 days | Position snapshots |
| `events` | 60 days | Startup, re-opt, stale candle, skip/fail, TIME_TP computation details |
| `optimization_runs` | 365 days | Per-run summary: trials, best params, duration |
| `optimization_trials` | 14 days | Every valid trial from every optimisation run |
| `monte_carlo_runs` | 365 days | Monte Carlo simulation results |
| `balance_snapshots` | 90 days | Periodic wallet balance snapshots |
| `mark_price_ticks` | 3 days | High-frequency mark-price ticks for liquidation monitoring |

**Backtest trade rows** (`mode = 'backtest'`) are written to the `trades` table after every accepted re-optimisation. They include millisecond timestamps (`entry_ts_ms`, `exit_ts_ms`) so the chart can place markers at the correct candle. Old backtest rows for a symbol/interval are replaced on each accepted reopt.

DB maintenance (pruning, WAL checkpoint, ANALYZE, VACUUM) runs automatically in a background thread at startup and every 24 hours.

---

## Unit Tests

A `tests/` directory contains pytest-based unit tests for core engine components:

```bash
python -m pytest tests/ -v
```

| Test file | Coverage |
|-----------|---------|
| `tests/test_indicators.py` | RMA, EMA, crossover detection, `compute_entry_signals_raw`, `resolve_entry_signals` (ADX/RSI gates) |
| `tests/test_orders.py` | `apply_slippage()` — LONG buy entry and sell exit, zero-tick case |
| `tests/test_backtester.py` | `backtest_once()` smoke test, flat-market no-liquidation, wallet history, PnL consistency |

No live API calls are made during tests — all fixtures use synthetic OHLCV data.

---

## Building a Standalone Executable

```bash
pip install -r requirements-build.txt

python build.py        # produces dist/mean_reversion_trader      (GUI)
python build.py --cli  # produces dist/mean_reversion_trader_cli  (CLI)
```

The executable bundles all dependencies and runs without a Python installation.

---

## Disclaimer

This software is for educational and research purposes. Automated trading involves significant financial risk. Past backtest performance does not guarantee future results. Use at your own risk.
