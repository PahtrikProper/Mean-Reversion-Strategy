"""Helper Functions"""

import time
import threading
from typing import List, Dict, Any

# Import constants
from .constants import (
    LEVERAGE_BY_SYMBOL,
    DEFAULT_LEVERAGE,
    TAKER_FEE_BY_SYMBOL,
    MAKER_FEE_BY_SYMBOL,
    FEE_RATE,
    MAKER_FEE_RATE,
    REST_MIN_INTERVAL_SEC,
    SLIPPAGE_TICKS,
    TICK_SIZE,
)

# Global rate limiting
_REST_LAST_CALL = 0.0
_REST_RATE_LOCK = threading.Lock()

def interval_minutes(interval: str) -> int:
    """Convert a candle interval string (e.g. "3") to integer minutes.
    Raises ValueError if the string is not a positive integer."""
    try:
        minutes = int(interval)
    except ValueError as exc:
        raise ValueError(f"Interval must be an integer string, got {interval}.") from exc
    if minutes <= 0:
        raise ValueError(f"Interval must be positive minutes, got {interval}.")
    return minutes

def supported_intervals(intervals: List[str], max_minutes: int = 60) -> List[str]:
    """Filter and validate candle intervals against Bybit's supported set {1, 3, 5, 15, 30, 60}.
    Returns sorted list of valid interval strings that are <= max_minutes.
    Used at startup to determine which intervals to test during optimization."""
    allowed = {1, 3, 5, 15, 30, 60}
    unique = sorted({interval_minutes(i) for i in intervals})
    limited = [str(i) for i in unique if i <= max_minutes and i in allowed]
    if not limited:
        raise ValueError(f"No supported intervals <= {max_minutes}m in {intervals}.")
    return limited



def now_ms() -> int:
    """Current UTC time as milliseconds since epoch.  Used for Bybit API timestamps."""
    return int(time.time() * 1000)

def leverage_for(symbol: str) -> float:
    """Look up the configured leverage for a symbol, falling back to DEFAULT_LEVERAGE."""
    return float(LEVERAGE_BY_SYMBOL.get(symbol, DEFAULT_LEVERAGE))

def fee_for(symbol: str) -> float:
    """Look up the taker fee rate for a symbol (backward-compat alias)."""
    return taker_fee_for(symbol)

def taker_fee_for(symbol: str) -> float:
    """Look up the taker fee rate for a symbol.  Returns the Bybit-queried rate if available
    (populated at startup), otherwise falls back to the hardcoded FEE_RATE."""
    return float(TAKER_FEE_BY_SYMBOL.get(symbol, FEE_RATE))

def maker_fee_for(symbol: str) -> float:
    """Look up the maker fee rate for a symbol.  Returns the Bybit-queried rate if available
    (populated at startup), otherwise falls back to MAKER_FEE_RATE."""
    return float(MAKER_FEE_BY_SYMBOL.get(symbol, MAKER_FEE_RATE))



def _rate_limit_rest():
    """Global REST rate limiter.  Ensures at least REST_MIN_INTERVAL_SEC seconds
    between consecutive REST API calls to avoid Bybit rate-limit bans.
    Thread-safe via _REST_RATE_LOCK."""
    global _REST_LAST_CALL
    if REST_MIN_INTERVAL_SEC <= 0:
        return
    with _REST_RATE_LOCK:
        now = time.monotonic()
        wait = _REST_LAST_CALL + REST_MIN_INTERVAL_SEC - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _REST_LAST_CALL = now
