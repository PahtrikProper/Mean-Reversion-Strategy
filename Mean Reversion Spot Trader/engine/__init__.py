"""
Mean Reversion Trader — Bybit Spot (LONG only)

Entry:  low crosses back above discount_k band (crossover)
        AND ADX < 25  (range-bound regime)
        AND RSI <= 50 (neutral-to-oversold close confirms the bounce)
Exit:   Trail stop (Jason McIntosh), TP, hard SL, or band exit
        Band: high drops above premium_k band (mirrors entry logic)
"""

from .utils.data_structures import (
    TradeRecord, BacktestResult, MCSimResult,
    EntryParams, ExitParams,
    RealPosition, PendingSignal,
)
from .utils.position_gate import PositionGate
from .core.indicators import (
    build_indicators,
    compute_entry_signals_raw,
    compute_exit_signals_raw,
    resolve_entry_signals,
    calc_sma,
)
from .core.orders import apply_slippage
from .trading.bybit_client import (
    BybitPrivateClient, rest_request,
    fetch_last_klines, fetch_mark_klines,
    fetch_risk_tiers, fetch_last_price,
    get_instrument_info,
)
from .trading.live_trader import LiveRealTrader, start_live_ws, download_seed_history
from .backtest.backtester import backtest_once, run_monte_carlo, mc_score
from .optimize.optimizer import optimise_params, optimise_bayesian

__version__ = "6.0"
__author__  = "PahtrikProper"
__license__ = "MIT"
