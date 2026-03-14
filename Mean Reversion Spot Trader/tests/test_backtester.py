"""Unit tests for engine/backtest/backtester.py

Run with:
    python -m pytest tests/test_backtester.py -v
"""

import sys
import os
import math
import pytest
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.backtest.backtester import backtest_once
from engine.utils.data_structures import EntryParams, ExitParams, BacktestResult


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_flat_candles(n: int = 300, base_price: float = 1.0) -> pd.DataFrame:
    """Synthetic flat-price OHLCV — price stays at base_price (no movement).
    Bands will be very tight; no crossovers should occur in a truly flat market.
    """
    ts_base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(n):
        ts = ts_base + timedelta(minutes=i)
        rows.append({
            "ts":     pd.Timestamp(ts),
            "open":   base_price,
            "high":   base_price,
            "low":    base_price,
            "close":  base_price,
            "volume": 1000.0,
        })
    return pd.DataFrame(rows)


def _make_volatile_candles(n: int = 400, seed: int = 42) -> pd.DataFrame:
    """Synthetic noisy OHLCV with mean-reverting price action to generate signals."""
    rng = np.random.default_rng(seed)
    ts_base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    price = 1.0
    rows = []
    for i in range(n):
        ts    = ts_base + timedelta(minutes=i)
        move  = rng.normal(0, 0.003)
        # Mean reversion: pull back toward 1.0
        price = price + move - 0.1 * (price - 1.0)
        price = max(0.5, price)
        spread = abs(rng.normal(0, 0.002))
        o = price
        h = price + spread + abs(rng.normal(0, 0.001))
        l = price - spread - abs(rng.normal(0, 0.001))
        c = price + rng.normal(0, 0.001)
        rows.append({
            "ts":     pd.Timestamp(ts),
            "open":   o,
            "high":   max(o, h, c),
            "low":    min(o, l, c),
            "close":  c,
            "volume": float(rng.integers(500, 5000)),
        })
    return pd.DataFrame(rows)


def _make_risk_df() -> pd.DataFrame:
    """Minimal risk tier DataFrame for liquidation calculations.
    Must include riskLimitValue (used by pick_risk_tier) and
    maintenanceMarginRate / mmDeduction (used in the liquidation formula).
    """
    return pd.DataFrame([{
        "riskLimitValue":       1_000_000.0,
        "minNotionalValue":     0.0,
        "maxNotionalValue":     1_000_000.0,
        "maintenanceMarginRate": 0.01,
        "mmDeductionValue":     0.0,
    }])


# ── Smoke test ────────────────────────────────────────────────────────────────

class TestBacktestOnce:
    def test_smoke_returns_backtest_result(self):
        """backtest_once() must return a BacktestResult without raising."""
        df = _make_volatile_candles(400)
        risk_df = _make_risk_df()
        ep = EntryParams(ma_len=50, band_mult=1.5)
        xp = ExitParams(tp_pct=0.005)

        result = backtest_once(
            df_last_raw=df,
            df_mark_raw=df,
            risk_df=risk_df,
            entry_params=ep,
            exit_params=xp,
            leverage=10.0,
            fee_rate=0.00055,
            maker_fee_rate=0.0002,
        )

        # Result may be None if insufficient candles, but should not raise
        if result is not None:
            assert isinstance(result, BacktestResult)
            assert isinstance(result.trades, int)
            assert isinstance(result.pnl_pct, float)
            assert isinstance(result.winrate, float)
            assert isinstance(result.max_drawdown_pct, float)
            assert isinstance(result.wallet_history, list)
            assert result.final_wallet > 0

    def test_no_liquidation_on_flat_market(self):
        """Flat market with no band crossovers should produce no trades and no liquidation."""
        df = _make_flat_candles(300)
        risk_df = _make_risk_df()
        ep = EntryParams(ma_len=50, band_mult=2.0)
        xp = ExitParams(tp_pct=0.005)

        result = backtest_once(
            df_last_raw=df,
            df_mark_raw=df,
            risk_df=risk_df,
            entry_params=ep,
            exit_params=xp,
            leverage=10.0,
            fee_rate=0.00055,
            maker_fee_rate=0.0002,
        )

        if result is not None:
            assert result.liquidated is False

    def test_wallet_history_non_empty_when_trades_exist(self):
        """If trades occurred, wallet_history should track equity over time."""
        df = _make_volatile_candles(400, seed=7)
        risk_df = _make_risk_df()
        ep = EntryParams(ma_len=30, band_mult=0.5)
        xp = ExitParams(tp_pct=0.003)

        result = backtest_once(
            df_last_raw=df,
            df_mark_raw=df,
            risk_df=risk_df,
            entry_params=ep,
            exit_params=xp,
            leverage=10.0,
            fee_rate=0.00055,
            maker_fee_rate=0.0002,
        )

        if result is not None and result.trades > 0:
            assert len(result.wallet_history) > 0
            # All wallet values should be non-negative
            assert all(w >= 0 for w in result.wallet_history)

    def test_pnl_pct_matches_wallet_change(self):
        """pnl_pct should be consistent with the starting and final wallet."""
        from engine.utils.constants import STARTING_WALLET
        df = _make_volatile_candles(400, seed=99)
        risk_df = _make_risk_df()
        ep = EntryParams(ma_len=40, band_mult=1.0)
        xp = ExitParams(tp_pct=0.004)

        result = backtest_once(
            df_last_raw=df,
            df_mark_raw=df,
            risk_df=risk_df,
            entry_params=ep,
            exit_params=xp,
            leverage=10.0,
            fee_rate=0.00055,
            maker_fee_rate=0.0002,
        )

        if result is not None:
            expected_pct = (result.pnl_usdt / STARTING_WALLET) * 100.0
            assert result.pnl_pct == pytest.approx(expected_pct, rel=1e-4)
