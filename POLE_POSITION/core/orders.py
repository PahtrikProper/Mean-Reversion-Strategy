"""Order Execution"""

from ..utils.constants import SLIPPAGE_TICKS, TICK_SIZE


def apply_slippage(price: float, side: str) -> float:
    """
    TradingView slippage model (simple):
      - SHORT entry (sell): worse price = price - tick
      - COVER exit (buy):  worse price = price + tick
    """
    if SLIPPAGE_TICKS <= 0:
        return float(price)
    delta = SLIPPAGE_TICKS * TICK_SIZE
    if side == "sell":
        return float(price) - delta
    if side == "buy":
        return float(price) + delta
    raise ValueError("side must be 'sell' or 'buy'")
