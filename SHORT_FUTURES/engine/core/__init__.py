"""Core trading logic — Mean Reversion Strategy indicators and signals."""

from .indicators import (
    rma,
    ema,
    build_bands,
    calculate_adx,
    calculate_rsi,
    calc_sma,
    build_indicators,
    crossover,
    crossunder,
    compute_entry_signals_raw,
    compute_exit_signals_raw,
    resolve_entry_signals,
    ADX_PERIOD,
    ADX_THRESHOLD,
    RSI_PERIOD,
    RSI_NEUTRAL_LO,
)
from .orders import apply_slippage

__all__ = [
    "rma", "ema",
    "build_bands",
    "calculate_adx", "calculate_rsi",
    "calc_sma",
    "build_indicators",
    "crossover", "crossunder",
    "compute_entry_signals_raw",
    "compute_exit_signals_raw",
    "resolve_entry_signals",
    "ADX_PERIOD", "ADX_THRESHOLD",
    "RSI_PERIOD", "RSI_NEUTRAL_LO",
    "apply_slippage",
]
