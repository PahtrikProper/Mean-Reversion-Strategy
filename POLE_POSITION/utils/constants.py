"""Constants and Configuration — Mean Reversion Strategy"""

import os
import sys
from typing import Dict

# ── Symbol & trading setup ────────────────────────────────────────────────────
SYMBOLS          = ["XRPUSDT"]
CANDLE_INTERVALS = ["1", "3", "5"]          # 1m, 3m, 5m
CATEGORY         = "linear"

# ── Paper trading ─────────────────────────────────────────────────────────────
PAPER_STARTING_BALANCE = 500.0
PAPER_SYMBOLS = ["XRPUSDT", "ETHUSDT", "BTCUSDT", "ESPUSDT"]

DAYS_BACK_SEED    = 1                        # history window for seed + re-opt
STARTING_WALLET   = 100.0
DEFAULT_LEVERAGE  = 10.0
LEVERAGE_BY_SYMBOL: Dict[str, float] = {
    "XRPUSDT": 10.0,
}

MAX_SYMBOL_FRACTION = 0.45    # max margin per symbol (45% of account)
MAX_ACTIVE_SYMBOLS  = 1
MIN_WALLET_USDT     = 5.0

# ── Fee configuration ─────────────────────────────────────────────────────────
FEE_RATE        = 0.00055    # Bybit taker fee (default)
MAKER_FEE_RATE  = 0.0002     # Bybit maker fee (PostOnly entries)
TAKER_FEE_BY_SYMBOL: Dict[str, float] = {}
MAKER_FEE_BY_SYMBOL: Dict[str, float] = {}

# ── Slippage ──────────────────────────────────────────────────────────────────
SLIPPAGE_TICKS = 1
TICK_SIZE      = 0.0001      # for slippage simulation

# ── Live TP scaling ───────────────────────────────────────────────────────────
# The optimiser finds a tp_pct on historical data.  In live trading the
# server-side TP (and all displays) uses this fraction of that value so the
# order sits closer to the fill price and is more likely to trigger.
# 0.75 → live TP distance = 75% of the back-tested distance.
LIVE_TP_SCALE = 0.75

# ── Default strategy parameters ───────────────────────────────────────────────
DEFAULT_MA_LEN       = 100    # RMA period for band centre line
DEFAULT_BAND_MULT    = 2.5    # Band width multiplier (%)
DEFAULT_HOLDING_DAYS = 30     # max calendar days to hold
DEFAULT_TP_PCT       = 0.0028 # 0.28% take-profit (optimised; ~midpoint of range)

# ── Jason McIntosh ATR trailing stop (SHORT exit) ─────────────────────────────
# Formula: stop = min_low_since_entry + TRAIL_ATR_MULT × ATR(TRAIL_ATR_PERIOD)
# Exit SHORT when: current_high >= trail_stop
# As price falls (trade going in our favour), min_low_since_entry decreases
# → trail_stop also falls, locking in more profit on the way down.
TRAIL_ATR_PERIOD = 14    # Wilder ATR lookback period
TRAIL_ATR_MULT   = 3.0   # ATR multiplier (3× ATR above lowest low since entry)

# ── Optimiser search ranges ───────────────────────────────────────────────────
INIT_TRIALS          = 4000
REOPT_INTERVAL_SEC   = 8 * 60 * 60   # re-optimise every 8 hours

# Entry — MA length (RMA period for band centre line)
OPT_MA_LEN_MIN        = 2
OPT_MA_LEN_MAX        = 300

# Entry — Band multiplier (stored as integer × 10: 3 = 0.3, 100 = 10.0)
OPT_BAND_MULT_X10_MIN = 3    # 0.3%
OPT_BAND_MULT_X10_MAX = 100  # 10.0%

# Max holding period (days)
OPT_HOLDING_MIN   = 1
OPT_HOLDING_MAX   = 30

# Take-profit (in basis points, 1 bp = 0.0001; 18 = 0.18%, 1100 = 11.00%)
OPT_TP_MIN_BP       = 18    # 0.18% price move before leverage
OPT_TP_MAX_BP       = 1100  # 11.00% price move before leverage

OPT_N_RANDOM      = INIT_TRIALS
OPT_MIN_TRADES    = 3

RANDOM_SEED       = None     # set int for reproducible runs

# Exploitation: sample near saved best params
EXPLOIT_RATIO                = 0.60
EXPLOIT_MA_LEN_RADIUS        = 15
EXPLOIT_BAND_MULT_RADIUS_X10 = 3    # ±0.3 around saved best band_mult
EXPLOIT_HOLDING_RADIUS       = 5
EXPLOIT_TP_RADIUS_BP         = 50   # ±0.50% around saved best TP

# ── Runtime behaviour ─────────────────────────────────────────────────────────
KEEP_CANDLES            = 3000
PRINT_EVERY_CANDLE      = True
API_POLITE_SLEEP        = 0.1
REST_MIN_INTERVAL_SEC   = 0.2

# ── API credentials ───────────────────────────────────────────────────────────
def _load_api_credentials():
    api_key    = os.getenv("BYBIT_API_KEY",    "").strip()
    api_secret = os.getenv("BYBIT_API_SECRET", "").strip()
    if api_key and api_secret and not api_key.startswith("YOUR_"):
        return api_key, api_secret
    try:
        from pathlib import Path
        import json
        creds_file = Path.home() / ".bybit_credentials.json"
        if creds_file.exists():
            with open(creds_file, "r") as f:
                data = json.load(f)
            k = data.get("api_key", "").strip()
            s = data.get("api_secret", "").strip()
            if k and s:
                return k, s
    except Exception:
        pass
    return "YOUR_BYBIT_API_KEY", "YOUR_BYBIT_API_SECRET"

API_KEY, API_SECRET = _load_api_credentials()
RECV_WINDOW         = "5000"

# ── Logging paths ─────────────────────────────────────────────────────────────
# When frozen by PyInstaller (--onefile), sys._MEIPASS is a temporary directory
# that is deleted on exit — logs must go next to the executable instead.
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOG_DIR         = os.path.join(BASE_DIR, "paper_logs")
os.makedirs(LOG_DIR, exist_ok=True)

EVENT_LOG_PATH  = os.path.join(LOG_DIR, "events.log")
TRADES_CSV_PATH = os.path.join(LOG_DIR, "trades.csv")
PARAMS_CSV_PATH = os.path.join(LOG_DIR, "params.csv")
ORDERS_LOG_PATH = os.path.join(LOG_DIR, "orders.log")

# ── ANSI colors ───────────────────────────────────────────────────────────────
COLOR_LONG      = "\033[94m"
COLOR_SHORT     = "\033[91m"
COLOR_ENTRY     = "\033[92m"
COLOR_EXIT      = "\033[93m"
COLOR_ERROR     = "\033[41m"
COLOR_RESET     = "\033[0m"
COLOR_PENDING   = "\033[93m"
COLOR_CONFIRMED = "\033[92m"
COLOR_SUBMITTED = "\033[94m"
