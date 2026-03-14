"""Constants and Configuration — Mean Reversion Strategy"""

import os
import sys
from typing import Dict

# ── Symbol & trading setup ────────────────────────────────────────────────────
SYMBOLS          = ["XRPUSDT"]
CANDLE_INTERVALS = ["5"]                    # 5m only
CATEGORY         = "spot"

DAYS_BACK_SEED    = 30                       # history window for seed + re-opt (max 30 days)
STARTING_WALLET   = 100.0
DEFAULT_LEVERAGE  = 3.0                      # spot margin default leverage

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
# 1.0 = live TP matches backtested TP exactly (entry * (1 + tp_pct) for LONG).
# tp_pct is a raw price-move fraction.
LIVE_TP_SCALE = 1.0

# ── Time-based TP tightening ─────────────────────────────────────────────────
# If a LONG position is still open after TIME_TP_HOURS, the TP is overridden
# with a data-driven tighter target so the trade exits sooner.
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
DEFAULT_TP_PCT         = 0.003  # 0.30% take-profit (default; now also optimised)

# ── Entry gate defaults (also optimised at runtime) ───────────────────────────
ADX_THRESHOLD   = 25.0  # ADX must be below this for entry (range-bound regime)
RSI_NEUTRAL_LO  = 50.0  # RSI must be <= this at candle close (oversold confirmation for LONG)
BAND_EMA_LENGTH = 5     # EMA smoothing period applied to all 8 premium/discount bands
ADX_PERIOD      = 14    # Wilder's ADX calculation period (optimised at runtime; default 14)
RSI_PERIOD      = 14    # Wilder's RSI calculation period (optimised at runtime; default 14)

# ── Volume liquidity filter ────────────────────────────────────────────────────
# Skip entry if our position notional exceeds this fraction of the candle's USDT
# volume.  Catches pathologically thin candles.  Fixed constant (not optimised)
# because at typical XRP position sizes vs $500K–$5M candle volumes the filter
# almost never fires; making it a search dim would add noise without signal.
VOL_FILTER_MAX_PCT = 0.05   # 5% of candle USDT volume

# ── Hard stop-loss (LONG exit) ────────────────────────────────────────────────
# Fires when: current_low <= entry_price * (1 - sl_pct)
# Intentionally wide — designed as a last-resort guard, not routinely triggered.
# Optimised alongside TP.
STOP_LOSS_PCT = 0.05     # default 5.0% below entry (optimised at runtime)

# ── Jason McIntosh trailing stop ──────────────────────────────────────────────
# Tracks the highest candle-high since LONG entry.
# Trail fires when: current_low <= highest_high_since_entry * (1 - TRAIL_STOP_PCT)
# The trail level only rises — it never moves down.
# Priority: highest — fires before TP and hard stop-loss.
# Set to 0.0 to disable.
TRAIL_STOP_PCT = 0.02    # 2.0% trailing stop below the highest high since entry

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

# Take-profit (in basis points, 1 bp = 0.0001; 20 = 0.20%, 100 = 1.00%)
# Data-driven range: ATR% medians are 0.19–0.41% across intervals → 20–100 bp
OPT_TP_MIN_BP       = 20    # 0.20% price move (1× ATR on 3-min)
OPT_TP_MAX_BP       = 100   # 1.00% price move (2.5× ATR on 15-min)

# Stop-loss (in basis points; 50 = 0.50%, 900 = 9.00%)
# Upper bound intentionally below the minimum liquidation distance at max leverage
# (10× → liq at ~9% below entry) so the optimizer always finds SL-before-liquidation params.
OPT_SL_MIN_BP       = 50    # 0.50% above entry
OPT_SL_MAX_BP       = 900   # 9.00% above entry

# ADX gate threshold (integer; 20 = most permissive, 28 = strictest in tested range)
# Data-driven: 3-min p50=25.13, 5-min p50=20.51, 15-min p50=23.33
OPT_ADX_MIN         = 20
OPT_ADX_MAX         = 28

# RSI neutral-low threshold (integer; 40 = most permissive, 60 = strictest)
# Data-driven: RSI p25=41, p75=60 across all intervals
OPT_RSI_LO_MIN      = 40
OPT_RSI_LO_MAX      = 60

# Band EMA smoothing length (integer; 2 = most responsive, 15 = smoothest)
OPT_BAND_EMA_MIN    = 2
OPT_BAND_EMA_MAX    = 15

# ADX calculation period (integer; 7 = sensitive, 21 = smooth)
OPT_ADX_PERIOD_MIN  = 7
OPT_ADX_PERIOD_MAX  = 21

# RSI calculation period (integer; 7 = sensitive, 21 = smooth)
OPT_RSI_PERIOD_MIN  = 7
OPT_RSI_PERIOD_MAX  = 21

# Leverage — discrete spot margin values tested by the optimizer
# Bybit spot margin supports: 2×, 3×, 4×, 8×, 10×
OPT_LEVERAGE_VALUES = [2, 3, 4, 8, 10]
OPT_LEVERAGE_MIN    = 2
OPT_LEVERAGE_MAX    = 10

# Spot margin maintenance margin rate (Bybit default ~0.5%)
# Used to compute the liquidation price: liq = entry × (lev-1) / (lev × (1 - MMR))
SPOT_MARGIN_MMR     = 0.005

# Backtest window per trial (days; fixed at 5 for fast spot optimisation)
OPT_MIN_DAYS        = 5
OPT_MAX_DAYS        = 5

OPT_N_RANDOM      = INIT_TRIALS
OPT_MIN_TRADES    = 1

RANDOM_SEED       = None     # set int for reproducible runs

# Exploitation: sample near saved best params
EXPLOIT_RATIO                     = 0.60
EXPLOIT_MA_LEN_RADIUS             = 15
EXPLOIT_BAND_MULT_RADIUS_X10      = 3    # ±0.3 around saved best entry band_mult
EXPLOIT_TP_RADIUS_BP              = 15   # ±0.15% around saved best TP (range is 0.20–1.00%)
EXPLOIT_SL_RADIUS_BP              = 50   # ±0.50% around saved best SL
EXPLOIT_EXIT_MA_LEN_RADIUS        = 15
EXPLOIT_EXIT_BAND_MULT_RADIUS_X10 = 3    # ±0.3 around saved best exit band_mult
EXPLOIT_ADX_RADIUS                = 2    # ±2 around saved best ADX threshold
EXPLOIT_RSI_LO_RADIUS             = 5    # ±5 around saved best RSI neutral-low
EXPLOIT_BAND_EMA_RADIUS           = 2    # ±2 around saved best band EMA length
EXPLOIT_ADX_PERIOD_RADIUS         = 2    # ±2 around saved best ADX period
EXPLOIT_RSI_PERIOD_RADIUS         = 2    # ±2 around saved best RSI period
EXPLOIT_LEVERAGE_RADIUS           = 1    # explore ±1 step in OPT_LEVERAGE_VALUES list

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
