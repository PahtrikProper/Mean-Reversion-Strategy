"""Unit tests for engine/core/orders.py — apply_slippage()

Run with:
    python -m pytest tests/test_orders.py -v
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.core.orders import apply_slippage
from engine.utils import constants as C
import engine.core.orders as _orders_mod


class TestApplySlippage:
    def test_sell_reduces_price(self):
        """SHORT entry (sell) receives price - delta (worse fill for seller)."""
        price = 1.00000
        result = apply_slippage(price, "sell")
        if C.SLIPPAGE_TICKS > 0:
            assert result < price
        else:
            assert result == pytest.approx(price)

    def test_buy_increases_price(self):
        """Cover exit (buy) receives price + delta (worse fill for buyer)."""
        price = 1.00000
        result = apply_slippage(price, "buy")
        if C.SLIPPAGE_TICKS > 0:
            assert result > price
        else:
            assert result == pytest.approx(price)

    def test_sell_delta_equals_ticks_times_tick_size(self):
        """Sell fill should be exactly SLIPPAGE_TICKS * TICK_SIZE below input."""
        if C.SLIPPAGE_TICKS <= 0:
            pytest.skip("SLIPPAGE_TICKS is 0 — slippage disabled")
        price = 2.50000
        expected = price - C.SLIPPAGE_TICKS * C.TICK_SIZE
        assert apply_slippage(price, "sell") == pytest.approx(expected)

    def test_buy_delta_equals_ticks_times_tick_size(self):
        """Buy fill should be exactly SLIPPAGE_TICKS * TICK_SIZE above input."""
        if C.SLIPPAGE_TICKS <= 0:
            pytest.skip("SLIPPAGE_TICKS is 0 — slippage disabled")
        price = 2.50000
        expected = price + C.SLIPPAGE_TICKS * C.TICK_SIZE
        assert apply_slippage(price, "buy") == pytest.approx(expected)

    def test_zero_ticks_no_change(self):
        """With SLIPPAGE_TICKS=0, price is returned unchanged for both sides."""
        # Patch the module-level constant used inside apply_slippage
        original = _orders_mod.SLIPPAGE_TICKS
        try:
            _orders_mod.SLIPPAGE_TICKS = 0
            assert apply_slippage(1.5, "sell") == pytest.approx(1.5)
            assert apply_slippage(1.5, "buy")  == pytest.approx(1.5)
        finally:
            _orders_mod.SLIPPAGE_TICKS = original

    def test_returns_float(self):
        """Result should always be a float regardless of input type."""
        result = apply_slippage(1, "sell")
        assert isinstance(result, float)
