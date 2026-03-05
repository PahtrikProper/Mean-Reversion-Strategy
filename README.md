# Mean Reversion Trader

An automated SHORT-only mean-reversion trading bot for Bybit USDT linear perpetuals. Fades overextended price moves using EMA-smoothed premium bands, with an integrated parameter optimiser that re-tunes itself every 8 hours.

Symbols, leverage, intervals, and all strategy parameters are fully configurable — either through the GUI settings panel, a JSON config file, or `POLE_POSITION/utils/constants.py`.

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
└── POLE_POSITION/           # Core package
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
    │   ├── helpers.py           # Rate limiter, interval parsing, fee lookups
    │   ├── logger.py            # CSV and order log writers
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
| Symbols | Comma-separated list of Bybit USDT perpetual symbols to trade |
| Leverage | Leverage multiplier for paper trading (1x–100x); live mode syncs from Bybit |

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
2. Read live leverage from Bybit for each symbol
3. Download seed candle history for each (symbol, interval) pair
4. Run the parameter optimiser (~4 000 backtest trials per pair)
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

Default settings live in `POLE_POSITION/utils/constants.py`. You can override most of them with a JSON config file:

```json
{
  "symbol":          "XRPUSDT",
  "leverage":        10,
  "starting_wallet": 100,
  "entry": {
    "ma_len":    100,
    "band_mult": 2.5
  },
  "exit": {
    "holding_days": 30,
    "tp_pct":       0.0028
  },
  "optimizer": {
    "n_trials": 4000
  }
}
```

Place the file anywhere and pass it with `--config`.

### Key constants

| Constant | Default | Description |
|----------|---------|-------------|
| `SYMBOLS` | `["XRPUSDT"]` | Symbols for live trading |
| `PAPER_SYMBOLS` | `["XRPUSDT", "ETHUSDT", "BTCUSDT", "ESPUSDT"]` | Symbols for paper trading |
| `CANDLE_INTERVALS` | `["1","3","5"]` | Candle sizes (minutes) tested during optimisation |
| `DEFAULT_LEVERAGE` | `10.0` | Leverage for paper trading (live syncs from Bybit) |
| `PAPER_STARTING_BALANCE` | `500.0` | Virtual wallet size for paper trading (USDT) |
| `MAX_SYMBOL_FRACTION` | `0.45` | Max wallet fraction used as margin per trade |
| `DAYS_BACK_SEED` | `1` | Days of history downloaded for seed + re-optimisation |
| `INIT_TRIALS` | `4000` | Optimiser trials at startup |
| `REOPT_INTERVAL_SEC` | `28800` | Re-optimise every 8 hours |
| `LIVE_TP_SCALE` | `0.75` | Server-side TP is placed at 75% of backtested distance |
| `FEE_RATE` | `0.00055` | Bybit taker fee rate |
| `MAKER_FEE_RATE` | `0.0002` | Bybit maker fee rate |
| `SLIPPAGE_TICKS` | `1` | Slippage ticks applied to simulated fills |

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
| 4 | Time Exit | max holding days exceeded |
| 5 | Band Exit | low drops below a discount band (mirrors entry logic) |

In **live** mode, TP and liquidation are handled server-side by Bybit; trail stop, time, and band exits are checked on every closed candle. In **paper** mode, all five exits are simulated locally using the exact Bybit liquidation formula with the user-selected leverage. Slippage (1 tick) is applied to all simulated fills.

The optimiser searches four parameters — MA length, band multiplier, max holding days, and TP % — and re-runs every 8 hours in a background thread so live candle processing is never blocked. All (symbol, interval) pairs are ranked by `score = PnL% / (1 + max_drawdown%)` and the top-ranked pair per symbol is selected for trading.

See **`STRATEGY.md`** for the complete specification including exact formulas and all invariants.

---

## Logs and Output

| Path | Contents |
|------|---------|
| `paper_logs/trades.csv` | Every entry and exit with fill prices, PnL, fees, slippage |
| `paper_logs/params.csv` | Parameter changes from each optimisation run |
| `paper_logs/orders.log` | Raw order placement log with colour codes |
| `paper_logs/events.log` | General event log (startup, errors, re-optimisation) |

---

## Building a Standalone Executable

```bash
pip install -r requirements-build.txt

python build.py        # produces dist/MeanReversionTrader (GUI)
python build.py --cli  # produces dist/MeanReversionTraderCLI
```

The executable bundles all dependencies and runs without a Python installation.

---

## Disclaimer

This software is for educational and research purposes. Automated trading involves significant financial risk. Past backtest performance does not guarantee future results. Use at your own risk.
