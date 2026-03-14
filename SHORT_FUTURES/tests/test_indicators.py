"""Unit tests for engine/core/indicators.py

Tests cover: RMA, EMA, crossover, compute_entry_signals_raw,
resolve_entry_signals (ADX/RSI gates).

Run with:
    python -m pytest tests/test_indicators.py -v
"""

import sys
import os
import math
import pytest
import numpy as np
import pandas as pd

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.core.indicators import (
    rma,
    ema,
    crossover,
    compute_entry_signals_raw,
    resolve_entry_signals,
    ADX_THRESHOLD,
    RSI_NEUTRAL_LO,
    build_indicators,
)


# ── RMA tests ─────────────────────────────────────────────────────────────────

class TestRma:
    def test_seed_equals_first_value(self):
        """RMA[0] must equal series[0] (Wilder seeding)."""
        data = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0])
        result = rma(data, length=3)
        assert result[0] == pytest.approx(data.iloc[0])

    def test_constant_series_stays_constant(self):
        """RMA of a constant series must equal that constant at every bar."""
        data = pd.Series([5.0] * 50)
        result = rma(data, length=10)
        for val in result:
            assert val == pytest.approx(5.0, rel=1e-6)

    def test_length_1_equals_series(self):
        """RMA with length=1 (alpha=1) must track series exactly."""
        data = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        result = rma(data, length=1)
        for i, val in enumerate(result):
            assert val == pytest.approx(data.iloc[i])

    def test_output_length_matches_input(self):
        data = pd.Series(list(range(1, 21)))
        result = rma(data, length=5)
        assert len(result) == len(data)

    def test_smoothing_decreases_volatility(self):
        """RMA should produce a smoother series than the raw input."""
        import random
        random.seed(42)
        raw_list = [100.0 + random.gauss(0, 5) for _ in range(100)]
        data     = pd.Series(raw_list)
        result   = rma(data, length=14)
        raw_std  = float(np.std(raw_list))
        rma_std  = float(np.std(result))
        assert rma_std < raw_std


# ── EMA tests ─────────────────────────────────────────────────────────────────

class TestEma:
    def test_seed_equals_first_value(self):
        data = [10.0, 11.0, 12.0]
        result = ema(data, length=3)
        assert result[0] == pytest.approx(data[0])

    def test_constant_series_stays_constant(self):
        data = [7.0] * 30
        result = ema(data, length=5)
        for val in result:
            assert val == pytest.approx(7.0, rel=1e-6)

    def test_length_1_tracks_series(self):
        """EMA with length=1 (alpha=1.0) must equal series exactly."""
        data = [3.0, 6.0, 9.0, 12.0]
        result = ema(data, length=1)
        for i, val in enumerate(result):
            assert val == pytest.approx(data[i])


# ── Crossover tests ───────────────────────────────────────────────────────────
# crossover(a_prev, a_cur, b_prev, b_cur) — True when A crosses above B

class TestCrossover:
    def test_crossover_detected(self):
        """a_prev <= b_prev AND a_cur > b_cur → True (A crosses above B)."""
        assert crossover(a_prev=9.0, a_cur=11.0, b_prev=10.0, b_cur=10.0) is True

    def test_no_crossover_when_already_above(self):
        """Both bars a > b → no crossover."""
        assert crossover(a_prev=11.0, a_cur=12.0, b_prev=10.0, b_cur=10.0) is False

    def test_no_crossover_when_always_below(self):
        assert crossover(a_prev=8.0, a_cur=9.0, b_prev=10.0, b_cur=10.0) is False

    def test_exactly_equal_prev_is_crossover(self):
        """a_prev == b_prev satisfies a_prev <= b_prev; if a_cur > b_cur it fires."""
        assert crossover(a_prev=10.0, a_cur=11.0, b_prev=10.0, b_cur=10.0) is True


# ── compute_entry_signals_raw tests ──────────────────────────────────────────
# The function reads "high" from the row dicts/Series and premium_1..8.
# Signal fires when: premium_k crosses ABOVE price high
# (prev_band <= prev_high  AND  curr_band > curr_high)

class TestComputeEntrySignalsRaw:
    def _make_rows(self, band_val, prev_high, curr_high):
        """Make prev_row and curr_row Series with all premium bands + high."""
        bands = {f"premium_{k}": band_val for k in range(1, 9)}
        prev_row = pd.Series({**bands, "high": prev_high})
        curr_row = pd.Series({**bands, "high": float("nan")})  # high unused from curr_row
        return prev_row, curr_row

    def test_signal_fires_on_band_crossover(self):
        """Band was below prev_high; after move band is above curr_high → signal."""
        # band = 100, prev_high = 102 (band <= high), curr_high = 98 (band > high)
        prev_row, curr_row = self._make_rows(band_val=100.0, prev_high=102.0, curr_high=100.0)
        result = compute_entry_signals_raw(
            current_row=curr_row, prev_row=prev_row,
            current_high=98.0,
        )
        assert result > 0
        assert 1 <= result <= 8

    def test_no_signal_when_high_stays_below_band(self):
        """If price never reached the band (prev_high < band), no signal."""
        # band = 100, prev_high = 95 (band > high always) → crossover condition fails
        prev_row, curr_row = self._make_rows(band_val=100.0, prev_high=95.0, curr_high=100.0)
        result = compute_entry_signals_raw(
            current_row=curr_row, prev_row=prev_row,
            current_high=90.0,
        )
        assert result == 0

    def test_returns_highest_band_number(self):
        """Should return an int in range 0–8."""
        prev_row = pd.Series({f"premium_{k}": 100.0 + k for k in range(1, 9)},
                             dtype=float)
        prev_row["high"] = 112.0   # above band_8 (100+8=108)
        curr_row = pd.Series({f"premium_{k}": 100.0 + k for k in range(1, 9)},
                             dtype=float)
        curr_row["high"] = float("nan")
        result = compute_entry_signals_raw(
            current_row=curr_row, prev_row=prev_row,
            current_high=95.0,
        )
        assert isinstance(result, int)
        assert 0 <= result <= 8


# ── resolve_entry_signals tests ──────────────────────────────────────────────

class TestResolveEntrySignals:
    def test_adx_blocks_when_trending(self):
        """ADX >= 25 should block signal regardless of RSI."""
        raw = 3  # band 3 fired
        result = resolve_entry_signals(raw_short=raw, adx=ADX_THRESHOLD, rsi=60.0)
        assert result == 0

    def test_adx_just_below_threshold_passes(self):
        """ADX < 25 should pass the ADX gate."""
        raw = 3
        result = resolve_entry_signals(raw_short=raw, adx=ADX_THRESHOLD - 0.01, rsi=60.0)
        # RSI gate (>= 50) also passes — signal should fire
        assert result > 0

    def test_rsi_blocks_when_below_neutral(self):
        """RSI < 50 should block signal (close not confirming overbought)."""
        raw = 3
        result = resolve_entry_signals(raw_short=raw, adx=20.0, rsi=RSI_NEUTRAL_LO - 0.01)
        assert result == 0

    def test_rsi_at_threshold_passes(self):
        """RSI == 50 (at exact threshold) should pass."""
        raw = 3
        result = resolve_entry_signals(raw_short=raw, adx=20.0, rsi=RSI_NEUTRAL_LO)
        assert result > 0

    def test_both_gates_pass(self):
        """Both ADX < 25 and RSI >= 50 pass → signal returned unchanged."""
        raw = 5
        result = resolve_entry_signals(raw_short=raw, adx=15.0, rsi=55.0)
        assert result == raw

    def test_no_raw_signal_returns_zero(self):
        """If raw_short == 0, gates are irrelevant — output is 0."""
        result = resolve_entry_signals(raw_short=0, adx=10.0, rsi=50.0)
        assert result == 0
