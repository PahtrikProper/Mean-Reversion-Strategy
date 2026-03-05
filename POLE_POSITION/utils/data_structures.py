"""Data Structures — Mean Reversion Strategy"""

from dataclasses import dataclass, field
from typing import List, Optional

from .constants import (
    FEE_RATE,
    STARTING_WALLET,
    DEFAULT_MA_LEN,
    DEFAULT_BAND_MULT,
    DEFAULT_TP_PCT,
    TRAIL_ATR_PERIOD,
    TRAIL_ATR_MULT,
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
    reason:          str           # "TP", "TRAIL_STOP", "BAND_EXIT", "LIQUIDATION"
    wallet_at_entry: float = 0.0

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
        AND ADX < 25 (range-bound regime)
        AND RSI >= 40 (not deeply oversold)
    """
    ma_len:    int   = DEFAULT_MA_LEN    # RMA period for band centre line
    band_mult: float = DEFAULT_BAND_MULT # Band width multiplier (%)


@dataclass
class ExitParams:
    """Mean Reversion exit parameters.

    Exit fires on (full system priority order):
        1. Liquidation  mark_high >= liq_price                        [not a param — handled externally]
        2. TP:          low  <= entry * (1 - tp_pct)                  [optimised]
        3. Trail Stop:  high >= min_low_since_entry + mult × ATR       [Jason McIntosh]
        4. Band:        low drops below discount_k band                [mirrors entry logic]

    No hard stop-loss — TP, trail stop, and band exits only.
    Trail stop trails DOWN as price falls (SHORT), locking in profit.
    Exit signal: current HIGH crosses above the trail stop level.
    """
    tp_pct:           float = DEFAULT_TP_PCT    # take-profit fraction (e.g. 0.0028 = 0.28%)
    trail_atr_period: int   = TRAIL_ATR_PERIOD  # ATR lookback for Jason McIntosh trail stop
    trail_atr_mult:   float = TRAIL_ATR_MULT    # ATR multiplier (stop = min_low + mult×ATR)


# Legacy alias
Params = EntryParams


@dataclass
class RealPosition:
    """Snapshot of the live Bybit position fetched via REST."""
    qty:         float    # signed: negative = SHORT
    entry_price: float
    side:        str      # "Buy" or "Sell"
    entry_time:  Optional[object] = None  # pd.Timestamp of entry (tracked locally)


@dataclass
class PendingSignal:
    """An entry signal awaiting execution."""
    side:         str
    entry_params: EntryParams
    exit_params:  ExitParams
    is_flip:      bool = False
    level:        int  = 1     # band level 1-8 that triggered the signal
