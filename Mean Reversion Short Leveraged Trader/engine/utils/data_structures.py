"""Data Structures — Mean Reversion Strategy"""

from dataclasses import dataclass, field
from typing import List, Optional

from .constants import (
    FEE_RATE,
    STARTING_WALLET,
    DEFAULT_MA_LEN,
    DEFAULT_BAND_MULT,
    DEFAULT_EXIT_MA_LEN,
    DEFAULT_EXIT_BAND_MULT,
    DEFAULT_TP_PCT,
    STOP_LOSS_PCT,
    ADX_THRESHOLD,
    RSI_NEUTRAL_LO,
    BAND_EMA_LENGTH,
    DEFAULT_LEVERAGE,
    ADX_PERIOD,
    RSI_PERIOD,
)


@dataclass
class TradeRecord:
    """A single completed round-trip trade from a backtest run."""
    side:            str
    entry_price:     float
    exit_price:      float
    qty:             float
    entry_fee:       float
    exit_fee:        float
    pnl_gross:       float
    pnl_net:         float
    reason:          str           # "TP", "TIME_TP", "STOP_LOSS", "BAND_EXIT", "LIQUIDATION"
    wallet_at_entry: float = 0.0
    hold_candles:    int   = 0     # candles held from entry to exit
    entry_ts_ms:     int   = 0     # candle open timestamp at entry (ms since epoch)
    exit_ts_ms:      int   = 0     # candle open timestamp at exit  (ms since epoch)

    @property
    def return_pct(self) -> float:
        return self.pnl_net / self.wallet_at_entry if self.wallet_at_entry > 0 else 0.0


@dataclass
class BacktestResult:
    final_wallet:     float
    pnl_usdt:         float
    pnl_pct:          float
    trades:           int
    winrate:          float
    liquidated:       bool
    sharpe_ratio:     float
    max_drawdown_pct: float             = 0.0
    wallet_history:   List[float]       = field(default_factory=list)
    trade_records:    List[TradeRecord] = field(default_factory=list)
    avg_hold_minutes: float             = 0.0   # mean position hold time in minutes
    min_hold_minutes: float             = 0.0   # shortest hold time in minutes
    max_hold_minutes: float             = 0.0   # longest hold time in minutes


# ── Monte Carlo ────────────────────────────────────────────────────────────────
MC_SIMS       = 5000
MC_MIN_TRADES = 5

_MC_RESET = "\033[0m"
_MC_BOLD  = "\033[1m"
_MC_GREEN = "\033[92m"
_MC_RED   = "\033[91m"
_MC_CYAN  = "\033[96m"
_MC_WHITE = "\033[97m"
_MC_DIM   = "\033[2m"


@dataclass
class MCSimResult:
    final_wallet:       float
    pnl_usdt:           float
    pnl_pct:            float
    max_drawdown_pct:   float
    max_losing_streak:  int
    trades:             int
    wins:               int
    winrate:            float
    sharpe:             float
    ruined:             bool


# ── Strategy parameters ────────────────────────────────────────────────────────

@dataclass
class EntryParams:
    """Mean Reversion entry parameters.

    Entry fires when:
        high drops back below premium_k band (crossover of band above high)
        AND ADX < adx_threshold (range-bound regime; default 25)
        AND RSI >= rsi_neutral_lo (neutral-to-overbought close confirms the fade; default 50)

    All seven fields are optimised at runtime by the random-search optimizer.
    """
    ma_len:         int   = DEFAULT_MA_LEN    # RMA period for band centre line
    band_mult:      float = DEFAULT_BAND_MULT # Band width multiplier (%)
    adx_threshold:  float = ADX_THRESHOLD     # Max ADX for entry (range-bound gate)
    rsi_neutral_lo: float = RSI_NEUTRAL_LO    # Min RSI at close (overbought confirmation)
    band_ema_len:   int   = BAND_EMA_LENGTH   # EMA smoothing on all 8 premium/discount bands
    adx_period:     int   = ADX_PERIOD        # Wilder's ADX calculation period (optimised)
    rsi_period:     int   = RSI_PERIOD        # Wilder's RSI calculation period (optimised)


@dataclass
class ExitParams:
    """Mean Reversion exit parameters.

    Exit fires on (full system priority order):
        1. Liquidation  mark_high >= liq_price                        [not a param — handled externally]
        2. TP:          low  <= entry * (1 - tp_pct)                  [optimised]
        3. Stop-Loss:   high >= entry * (1 + sl_pct)                  [optimised — wide, pre-liquidation guard]
        4. Band:        low drops below discount_k band                [independent exit-band params]

    SL is intentionally wide (default 5%) — intended to prevent full account
    liquidation, not to be routinely triggered.  Optimised alongside TP so
    the backtest finds the widest SL that still protects the account.

    exit_ma_len / exit_band_mult control the discount bands used for the band
    exit signal.  These are optimised independently from the entry (premium)
    band params (EntryParams.ma_len / band_mult), allowing the system to find
    different sensitivity for exiting vs entering.

    leverage is optimised (2–14×) — controls position sizing and liquidation distance.
    """
    tp_pct:         float = DEFAULT_TP_PCT         # take-profit fraction (e.g. 0.0028 = 0.28%)
    sl_pct:         float = STOP_LOSS_PCT          # stop-loss fraction above entry (e.g. 0.05 = 5.0%)
    exit_ma_len:    int   = DEFAULT_EXIT_MA_LEN    # RMA period for discount (exit) band centre line
    exit_band_mult: float = DEFAULT_EXIT_BAND_MULT # exit band width multiplier (%)
    leverage:       float = DEFAULT_LEVERAGE       # position leverage (optimised; 2–14×)


@dataclass
class RealPosition:
    """Snapshot of the live Bybit position fetched via REST."""
    qty:         float    # signed: negative = SHORT
    entry_price: float
    side:        str      # "Buy" or "Sell"
    entry_time:  Optional[object] = None  # pd.Timestamp of entry (tracked locally)
    liq_price:   Optional[float]  = None  # liquidation price from Bybit (None in paper mode)


@dataclass
class PendingSignal:
    """An entry signal awaiting execution."""
    side:         str
    entry_params: EntryParams
    exit_params:  ExitParams
    is_flip:      bool = False
    level:        int  = 1     # band level 1-8 that triggered the signal
