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
DEFAULT_MA_LEN         = 100    # RMA period for premium (exit) band centre line
DEFAULT_BAND_MULT      = 2.5    # Premium band width multiplier (%)
DEFAULT_EXIT_MA_LEN    = 100    # RMA period for discount (entry) band centre line; overrides premium when different
DEFAULT_EXIT_BAND_MULT = 2.5    # Discount band width multiplier (%)

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

# ── Fixed take-profit (LONG exit) ────────────────────────────────────────────
# Hard ceiling: exit when price reaches entry × (1 + tp_pct).
# Fixed — not optimised. Serves as a wide safety cap; the McIntosh trailing
# stop and band-exit are the primary profit-taking mechanisms.
STOP_LOSS_PCT = 0.40     # 40.0% below entry (fixed, not optimised)
DEFAULT_TP_PCT = 0.20    # 20.0% above entry (fixed, not optimised)

# ── McIntosh trailing stop (LONG) ────────────────────────────────────────────
# Trails the LONG position at max_high_since_entry * (1 - trail_pct).
# Fires between TP and hard SL (exit priority 2.5): primary profit-protection.
# trail_pct is OPTIMISED at runtime — see OPT_TRAIL_PCT_MIN/MAX below.
TRAIL_STOP_PCT = 0.003   # 0.30% default; overridden by optimiser

# ── Hard-SL cooldown ──────────────────────────────────────────────────────────
# After a hard stop-loss fires, pause new entries for this many hours, then
# immediately trigger a fresh re-optimisation before resuming trading.
HARD_SL_PAUSE_HOURS = 6.0

# ── Optimiser search ranges ───────────────────────────────────────────────────
INIT_TRIALS          = 4000
REOPT_INTERVAL_SEC   = 4 * 60 * 60   # re-optimise every 4 hours (faster adaptation)

# Entry — MA length (RMA period for premium band centre line)
OPT_MA_LEN_MIN        = 2
OPT_MA_LEN_MAX        = 300

# Entry — Band multiplier (stored as integer × 10: 3 = 0.3, 30 = 3.0)
# Data-driven cap: on XRPUSDT 5m, band_mult > 3.0% produces zero entry signals
# over a 30-day window.  Keeping max at 3.0% ensures the optimizer always finds
# param sets that generate at least some trades.
OPT_BAND_MULT_X10_MIN = 3    # 0.3%
OPT_BAND_MULT_X10_MAX = 30   # 3.0%  (was 100 / 10.0% — zero signals above ~3%)

# Exit — MA length (RMA period for discount band centre line; independent of entry)
OPT_EXIT_MA_LEN_MIN        = 2
OPT_EXIT_MA_LEN_MAX        = 300

# Exit — Band multiplier (stored as integer × 10; independent of entry)
OPT_EXIT_BAND_MULT_X10_MIN = 3    # 0.3%
OPT_EXIT_BAND_MULT_X10_MAX = 30   # 3.0%  (capped in line with entry band)

# McIntosh trail percentage (integer × 10000: 10 = 0.10%, 500 = 5.00%)
OPT_TRAIL_X10000_MIN = 10    # 0.10% minimum trail distance from peak
OPT_TRAIL_X10000_MAX = 500   # 5.00% maximum trail distance from peak

# ADX gate threshold (integer; lower = more permissive, higher = stricter)
# Data-driven (5-min XRPUSDT): p10=19.8, p25=24.7, p50=32.9, p75=43.4, p90=55.3
# Previous max of 28 was below the empirical median — expanded to cover full distribution.
OPT_ADX_MIN         = 15
OPT_ADX_MAX         = 55

# RSI neutral-low threshold (integer; lower = more permissive, higher = stricter)
# Data-driven (5-min XRPUSDT): p10=34.3, p25=41.4, p50=49.6, p75=57.4, p90=65.2
OPT_RSI_LO_MIN      = 30
OPT_RSI_LO_MAX      = 70

# Band EMA smoothing length (integer; 2 = most responsive, 25 = smoothest)
OPT_BAND_EMA_MIN    = 2
OPT_BAND_EMA_MAX    = 25

# ADX calculation period (integer; 5 = very sensitive, 28 = smooth/long-term)
# Standard Wilder ADX = 14; short-term scalping: 7–10; longer-term swing: 20–28
OPT_ADX_PERIOD_MIN  = 5
OPT_ADX_PERIOD_MAX  = 28

# RSI calculation period (integer; 5 = very sensitive, 28 = smooth)
# Standard Wilder RSI = 14; commonly varied between 7 and 28
OPT_RSI_PERIOD_MIN  = 5
OPT_RSI_PERIOD_MAX  = 28

# Leverage — full 1–10 range available in GUI and optimizer
OPT_LEVERAGE_VALUES = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
OPT_LEVERAGE_MIN    = 1
OPT_LEVERAGE_MAX    = 10

# Spot margin maintenance margin rate (Bybit default ~0.5%)
# Used to compute the LONG liquidation price: liq = entry × (lev-1) / (lev × (1 - MMR))
SPOT_MARGIN_MMR     = 0.005

# Spot margin hourly borrowing interest rate (Bybit standard base rate ~0.01%/hr = 2.4%/yr).
# Applied to the borrowed portion of every LONG position held open each candle.
# Fetched from Bybit at startup via fetch_spot_margin_borrow_rate(); falls back to this default.
# Formula per trade: interest = borrowed_usdt × rate × hold_hours
#   borrowed_usdt = notional × (leverage - 1) / leverage
BORROW_HOURLY_RATE  = 0.0001   # 0.01% per hour (default fallback)

# Backtest window per trial — fixed 5-day windows with random offsets across
# the 30-day seed.  Each of the 4000 trials sees a different 5-day slice,
# so the optimizer tests params across many market regimes rather than
# overfitting to one continuous block.
OPT_MIN_DAYS        = 5
OPT_MAX_DAYS        = 5

OPT_N_RANDOM          = INIT_TRIALS
OPT_MIN_TRADES_PER_DAY = 1.0   # discard any trial with < 1 trade/day on average
OPT_MIN_TRADES        = 1      # absolute floor (kept for very short windows)

RANDOM_SEED       = None     # set int for reproducible runs

# Exploitation: sample near saved best params
# Ratio reduced to 0.35 so 65% of trials explore freely — prevents the
# optimizer from replaying the same narrow ball around saved_best every run.
EXPLOIT_RATIO                     = 0.35
# Radii doubled from originals so exploitation genuinely explores alternatives
# rather than just confirming the same params with different random seeds.
EXPLOIT_MA_LEN_RADIUS             = 30   # was 15
EXPLOIT_BAND_MULT_RADIUS_X10      = 8    # was 3  — ±0.8% across a 0.3–3.0% range
EXPLOIT_TRAIL_RADIUS_X10000       = 60   # was 20 — ±0.60% trail range
EXPLOIT_EXIT_MA_LEN_RADIUS        = 30   # was 15
EXPLOIT_EXIT_BAND_MULT_RADIUS_X10 = 8    # was 3
EXPLOIT_ADX_RADIUS                = 10   # was 5  — ±10 across a 15–55 range
EXPLOIT_RSI_LO_RADIUS             = 15   # was 7  — ±15 across a 30–70 range
EXPLOIT_BAND_EMA_RADIUS           = 6    # was 3  — ±6 across a 2–25 range
EXPLOIT_ADX_PERIOD_RADIUS         = 6    # was 3  — ±6 across a 5–28 range
EXPLOIT_RSI_PERIOD_RADIUS         = 6    # was 3  — ±6 across a 5–28 range
EXPLOIT_LEVERAGE_RADIUS           = 2    # was 1  — ±2 steps in OPT_LEVERAGE_VALUES

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
COLOR_ENTRY     = "\033[92m"
COLOR_EXIT      = "\033[93m"
COLOR_ERROR     = "\033[41m"
COLOR_RESET     = "\033[0m"
COLOR_CONFIRMED = "\033[92m"
COLOR_SUBMITTED = "\033[94m"
