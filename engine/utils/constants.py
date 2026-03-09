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
PAPER_SYMBOLS = ["XRPUSDT", "ETHUSDT", "ESPUSDT", "BTCUSDT"]

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

# ── Time-based TP tightening ─────────────────────────────────────────────────
# If a position is still open after TIME_TP_HOURS, the TP is overridden with a
# data-driven tighter target so the trade exits sooner.
# The new TP = avg(top-3 highest-PnL 20h+ exits) × TIME_TP_SCALE.
# Falls back to TIME_TP_FALLBACK_PCT when fewer than 3 qualifying trades exist.
TIME_TP_HOURS        = 20.0   # hours after entry before tightening kicks in
TIME_TP_FALLBACK_PCT = 0.005  # 0.5% fallback when DB has insufficient data
TIME_TP_SCALE        = 0.75   # scale factor applied to the data-driven avg

# ── Default strategy parameters ───────────────────────────────────────────────
DEFAULT_MA_LEN         = 100    # RMA period for entry (premium) band centre line
DEFAULT_BAND_MULT      = 2.5    # Entry band width multiplier (%)
DEFAULT_EXIT_MA_LEN    = 100    # RMA period for exit (discount) band centre line
DEFAULT_EXIT_BAND_MULT = 2.5    # Exit band width multiplier (%)
DEFAULT_TP_PCT         = 0.0028 # 0.28% take-profit (optimised; ~midpoint of range)

# ── Hard stop-loss (SHORT exit) ───────────────────────────────────────────────
# Fires when: current_high >= entry_price * (1 + sl_pct)
# Intentionally wide — designed to prevent full liquidation, not to be
# routinely triggered.  Optimised alongside TP.
# Typical optimised range: 0.50–9.00% above entry (stays inside liq at ~10%
# for 10× leverage; liquidation threshold varies with margin fraction).
STOP_LOSS_PCT = 0.05     # default 5.0% above entry (optimised at runtime)

# ── Optimiser search ranges ───────────────────────────────────────────────────
INIT_TRIALS          = 4000
REOPT_INTERVAL_SEC   = 12 * 60 * 60  # re-optimise every 12 hours

# Entry — MA length (RMA period for premium band centre line)
OPT_MA_LEN_MIN        = 2
OPT_MA_LEN_MAX        = 300

# Entry — Band multiplier (stored as integer × 10: 3 = 0.3, 100 = 10.0)
OPT_BAND_MULT_X10_MIN = 3    # 0.3%
OPT_BAND_MULT_X10_MAX = 100  # 10.0%

# Exit — MA length (RMA period for discount band centre line; independent of entry)
OPT_EXIT_MA_LEN_MIN        = 2
OPT_EXIT_MA_LEN_MAX        = 300

# Exit — Band multiplier (stored as integer × 10; independent of entry)
OPT_EXIT_BAND_MULT_X10_MIN = 3    # 0.3%
OPT_EXIT_BAND_MULT_X10_MAX = 100  # 10.0%

# Take-profit (in basis points, 1 bp = 0.0001; 18 = 0.18%, 1100 = 11.00%)
OPT_TP_MIN_BP       = 18    # 0.18% price move before leverage
OPT_TP_MAX_BP       = 1100  # 11.00% price move before leverage

# Stop-loss (in basis points; 50 = 0.50%, 900 = 9.00%)
# Upper bound kept below liquidation threshold (~10% for 10× leverage).
OPT_SL_MIN_BP       = 50    # 0.50% above entry
OPT_SL_MAX_BP       = 900   # 9.00% above entry

OPT_N_RANDOM      = INIT_TRIALS
OPT_MIN_TRADES    = 1

RANDOM_SEED       = None     # set int for reproducible runs

# Exploitation: sample near saved best params
EXPLOIT_RATIO                     = 0.60
EXPLOIT_MA_LEN_RADIUS             = 15
EXPLOIT_BAND_MULT_RADIUS_X10      = 3    # ±0.3 around saved best entry band_mult
EXPLOIT_TP_RADIUS_BP              = 50   # ±0.50% around saved best TP
EXPLOIT_SL_RADIUS_BP              = 50   # ±0.50% around saved best SL
EXPLOIT_EXIT_MA_LEN_RADIUS        = 15
EXPLOIT_EXIT_BAND_MULT_RADIUS_X10 = 3    # ±0.3 around saved best exit band_mult

# ── Runtime behaviour ─────────────────────────────────────────────────────────
KEEP_CANDLES            = 3000
PRINT_EVERY_CANDLE      = True
API_POLITE_SLEEP        = 0.1
REST_MIN_INTERVAL_SEC   = 0.2

# ── Signal drought detection ───────────────────────────────────────────────────
# If no raw entry signal fires for this many hours, a WARNING event is logged
# and printed.  Set to 0 to disable.
SIGNAL_DROUGHT_HOURS    = 4.0

# ── Max-loss guard ────────────────────────────────────────────────────────────
# If session PnL drops below -(MAX_LOSS_PCT / 100 × starting wallet), the bot
# exits any open position and halts new entries for the session.
# Set via --max-loss CLI flag.  None = disabled.
MAX_LOSS_PCT            = None   # type: ignore[assignment]  float | None

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
LOG_DIR         = os.path.join(BASE_DIR, "data")
os.makedirs(LOG_DIR, exist_ok=True)

EVENT_LOG_PATH  = os.path.join(LOG_DIR, "events.log")
TRADES_CSV_PATH = os.path.join(LOG_DIR, "trades.csv")
PARAMS_CSV_PATH = os.path.join(LOG_DIR, "params.csv")
ORDERS_LOG_PATH = os.path.join(LOG_DIR, "orders.log")
DB_PATH         = os.path.join(LOG_DIR, "trading.db")

# ── Initialize SQLite database ────────────────────────────────────────────────
def _init_db():
    try:
        from .db_logger import init_db
        init_db(DB_PATH)
    except Exception as _e:
        import logging as _logging
        _logging.getLogger("constants").warning(f"DB init failed: {_e}")

_init_db()

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
