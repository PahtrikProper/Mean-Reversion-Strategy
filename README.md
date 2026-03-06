# Mean Reversion Trader

An automated SHORT-only mean-reversion trading bot for Bybit USDT linear perpetuals. Fades overextended price moves using EMA-smoothed premium bands, with an integrated parameter optimiser that re-tunes itself every 8 hours.

Symbols, leverage, intervals, and all strategy parameters are fully configurable — either through the GUI settings panel, a JSON config file, or `engine/utils/constants.py`.

All trade data, optimisation runs, signals, events, and diagnostics are written to a single SQLite database (`paper_logs/trading.db`). No CSV or log files are used.
<img width="966" height="840" alt="Screenshot 2026-03-05 at 9 25 55 PM" src="https://github.com/user-attachments/assets/97fda080-b9a0-4ddc-af59-9377b9c57a8f" />

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
├── gui.py                   # GUI entry point
├── build.py                 # PyInstaller build script
├── requirements.txt
├── requirements-build.txt
├── STRATEGY.md              # Full strategy specification
└── engine/           # Core package
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
    │   ├── paper_trader.py      # Paper trading engine
    │   └── liquidation.py       # Bybit isolated SHORT liquidation formula
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
        └── default_config.json  # Default configuration
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

The bot needs a Bybit API key with **Derivatives / Contract trading** permissions. Read-only is not sufficient — order placement is required. Paper trading requires no API keys.

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

The GUI provides a full settings panel (expand **Settings** to configure):

| Setting | Description |
|---------|-------------|
| Risk Profile | Max wallet fraction used as margin per trade (10–95%) |
| Testing Period | Days of candle history used for optimisation (2–90 days) |
| Number of Tests | Optimiser trials per symbol/interval pair |
| Intervals | Candle sizes to test (1m, 3m, 5m, 15m, 30m, 60m) |
| Symbols | Comma-separated list of Bybit USDT perpetual symbols — applies to both LIVE and PAPER modes |
| Leverage | Leverage multiplier for paper trading (1x–100x); live mode reads the actual setting from Bybit |

Every setting has a dedicated **Apply** button. Changes take effect only when Apply is clicked; they are also locked in automatically when **START** is pressed.

Once the bot starts, five **stat cards** are displayed at all times:

| Card | Description |
|------|-------------|
| Account Balance | Current wallet balance (USDT) |
| P&L | Realized session P&L and account-level P&L |
| Win Rate | Win percentage across all closed trades |
| Total Trades | Number of completed trades this session |
| Leverage | Confirmed leverage in use — fetched from Bybit in live mode, set from Settings in paper mode |

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
```

### Live trading startup sequence

1. Load and validate API credentials (env vars → saved file → interactive prompt)
2. Read actual leverage from Bybit for each symbol; log it and warn if it differs from the GUI/config setting
3. Download seed candle history for each (symbol, interval) pair
4. Run the parameter optimiser (~4 000 backtest trials per pair); each pair logs `Testing {sym} {iv}m — leverage = Nx`
5. Rank all pairs by score: `PnL% / (1 + max_drawdown%)`; launch the top-ranked trader per symbol
6. Connect to Bybit WebSocket and begin live trading
7. `TradingStatusMonitor` prints a full status table every 3 minutes

### Paper trading startup sequence

1. Download public candle history (no API keys needed)
2. Run the parameter optimiser for each (symbol, interval) pair
3. Rank all pairs by score; launch the top-ranked paper trader
4. Connect to Bybit public WebSocket for live candle data
5. Simulate fills, PnL, fees, slippage, and liquidation using the exact Bybit formula
6. Virtual starting wallet: **$500 USDT**

---

## Configuration

Default settings live in `engine/utils/constants.py`. You can override most of them with a JSON config file:

```json
{
  "symbol":          "HYPEUSDT",
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
| `SYMBOLS` | `["XRPUSDT"]` | Active symbols — used by both LIVE and PAPER modes; overridden by the GUI Symbols field or `--symbols` CLI flag |
| `CANDLE_INTERVALS` | `["1","3","5"]` | Candle sizes (minutes) tested during optimisation |
| `DEFAULT_LEVERAGE` | `10.0` | Leverage for paper trading (live mode reads the actual setting from Bybit) |
| `STARTING_WALLET` | `100.0` | Simulated wallet used by the backtester and optimiser (not the paper trading wallet) |
| `PAPER_STARTING_BALANCE` | `500.0` | Virtual wallet size for paper trading (USDT) |
| `MAX_SYMBOL_FRACTION` | `0.45` | Max wallet fraction used as margin per trade |
| `MAX_ACTIVE_SYMBOLS` | `1` | Maximum number of simultaneously active traders (one per symbol) |
| `DAYS_BACK_SEED` | `1` | Days of history downloaded for seed + re-optimisation |
| `INIT_TRIALS` | `4000` | Optimiser trials at startup |
| `REOPT_INTERVAL_SEC` | `28800` | Re-optimise every 8 hours when flat |
| `LIVE_TP_SCALE` | `0.75` | Server-side TP is placed at 75% of backtested distance |
| `TIME_TP_HOURS` | `20.0` | Hours after entry before the data-driven time TP kicks in |
| `TIME_TP_FALLBACK_PCT` | `0.005` | 0.5% fallback TP when the DB has insufficient trade history |
| `TIME_TP_SCALE` | `0.75` | Scale factor applied to the data-driven average TP % |
| `FEE_RATE` | `0.00055` | Bybit taker fee rate |
| `MAKER_FEE_RATE` | `0.0002` | Bybit maker fee rate |
| `SLIPPAGE_TICKS` | `1` | Slippage ticks applied to simulated fills (paper and backtest only) |
| `RANDOM_SEED` | `None` | Optimiser RNG seed — `None` for non-deterministic runs; set to an integer for reproducible results |

---

## Strategy Summary

**SHORT only — no long trades.**

The bot enters short when price touches a premium band above the RMA centre line and then drops back below it (band crossover), signalling a failed breakout. Two gates must pass:

- **ADX < 25** — market must be range-bound
- **RSI ≥ 40** — not already deeply oversold

**Exit priority (strictly ordered):**

| # | Type | Trigger |
|---|------|---------|
| 1 | Liquidation | mark price ≥ liquidation price (Bybit isolated formula) |
| 2 | Take-Profit | price drops to entry × (1 − tp_pct) |
| 3 | Trail Stop | high ≥ min_low_since_entry + ATR_mult × ATR (Jason McIntosh) |
| 4 | Band Exit | low drops below a discount band (mirrors entry logic) |

In **live** mode, TP and liquidation are handled server-side by Bybit; trail stop and band exits are checked on every closed candle. In **paper** mode, all four exits are simulated locally using the exact Bybit liquidation formula with the user-selected leverage. Slippage (1 tick) is applied to all simulated fills.

The optimiser searches three parameters — MA length, band multiplier, and TP % — and re-runs every 8 hours in a background thread so live candle processing is never blocked. All (symbol, interval) pairs are ranked by `score = PnL% / (1 + max_drawdown%)` and the top-ranked pair per symbol is selected for trading.

See **`STRATEGY.md`** for the complete specification including exact formulas and all invariants.

---

## Logs and Output

All data is written to a single SQLite database — no CSV or log files are created.

| Path | Contents |
|------|---------|
| `paper_logs/trading.db` | SQLite database (WAL mode) containing all tables below |

### Database Tables

| Table | Retention | Contents |
|-------|-----------|---------|
| `trades` | 365 days | Every entry and exit: fill price, PnL, fees, slippage, exit reason |
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

DB maintenance (pruning, WAL checkpoint, ANALYZE, VACUUM) runs automatically in a background thread at startup and every 24 hours.

---

## Building a Standalone Executable

```bash
pip install -r requirements-build.txt

python build.py        # produces dist/hype_trader      (GUI)
python build.py --cli  # produces dist/hype_trader_cli  (CLI)
```

The executable bundles all dependencies and runs without a Python installation.

---

## Disclaimer

This software is for educational and research purposes. Automated trading involves significant financial risk. Past backtest performance does not guarantee future results. Use at your own risk.
