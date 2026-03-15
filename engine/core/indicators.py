"""Mean Reversion Strategy — Indicator Calculations

Entry (LONG only):
  1. Build discount bands: main = RMA(close, ma_len),
     discount_k = EMA(main * (1 - band_mult% * k), 5),  k in 1..8
  2. Raw signal: low[prev] <= discount_k[prev]  AND  low[now] > discount_k[now]
     (low bounces back above discount band after touching/exceeding it)
  3. Gates: ADX < 25 (range-bound market) AND RSI <= 50 (neutral-to-oversold close)

Exit:
  TP:          high >= entry * (1 + tp_pct)
  Stop-Loss:   low  <= entry * (1 - sl_pct)   [wide, pre-liquidation guard]
  Band exit:   high rises above premium_k band (mirrors entry logic on premium side)
"""

import pandas as pd
import numpy as np
from typing import Optional

from ..utils.constants import ADX_THRESHOLD, RSI_NEUTRAL_LO, BAND_EMA_LENGTH, ADX_PERIOD, RSI_PERIOD
# ADX_THRESHOLD, RSI_NEUTRAL_LO, BAND_EMA_LENGTH, ADX_PERIOD, RSI_PERIOD imported from constants
# (re-exported here so existing imports like `from indicators import ADX_THRESHOLD` still work)


# ─── RMA (Relative Moving Average) ──────────────────────────────────────────────

def rma(series: pd.Series, length: int) -> np.ndarray:
    """RMA = EMA with alpha = 1/length, seeded at first value.

    rma[0] = series[0]
    rma[i] = alpha * series[i] + (1 - alpha) * rma[i-1]
    where alpha = 1 / length
    """
    n = len(series)
    out = np.zeros(n, dtype=float)
    alpha = 1.0 / float(length)
    out[0] = float(series.iloc[0])
    for i in range(1, n):
        out[i] = alpha * float(series.iloc[i]) + (1.0 - alpha) * out[i - 1]
    return out


# ─── EMA ────────────────────────────────────────────────────────────────────────

def ema(series: np.ndarray, length: int) -> np.ndarray:
    """Standard EMA, alpha = 2 / (length + 1), seeded at first value.

    ema[0] = series[0]
    ema[i] = alpha * series[i] + (1 - alpha) * ema[i-1]
    where alpha = 2 / (length + 1)
    """
    n = len(series)
    out = np.zeros(n, dtype=float)
    alpha = 2.0 / (float(length) + 1.0)
    out[0] = float(series[0])
    for i in range(1, n):
        out[i] = alpha * float(series[i]) + (1.0 - alpha) * out[i - 1]
    return out


# ─── Premium / Discount Bands ────────────────────────────────────────────────────

def build_bands(
    df: pd.DataFrame,
    ma_len: int,
    band_mult: float,
    band_ema_len: int = BAND_EMA_LENGTH,
) -> pd.DataFrame:
    """Construct 8 premium + 8 discount bands.

    main        = RMA(close, ma_len)
    premium_k   = EMA(main * (1 + band_mult * 0.01 * k), band_ema_len)
    discount_k  = EMA(main * (1 - band_mult * 0.01 * k), band_ema_len)
    where k in [1, 8]

    Example with band_mult=2.5, band_ema_len=5:
      Band 1: main * 1.025 (premium), main * 0.975 (discount)
      Band 8: main * 1.200 (premium), main * 0.800 (discount)
    """
    df = df.copy()
    df["main"] = rma(df["close"], int(ma_len))
    main_values = df["main"].to_numpy(dtype=float)
    _ema_len = max(1, int(band_ema_len))

    for k in range(1, 9):
        premium_raw = main_values * (1.0 + float(band_mult) * 0.01 * k)
        df[f"premium_{k}"] = ema(premium_raw, length=_ema_len)

        discount_raw = main_values * (1.0 - float(band_mult) * 0.01 * k)
        df[f"discount_{k}"] = ema(discount_raw, length=_ema_len)

    return df


# ─── ATR (Wilder's method) ───────────────────────────────────────────────────────

def _calculate_atr(df: pd.DataFrame, period: int = 14) -> np.ndarray:
    """Wilder's Average True Range using RMA smoothing. Private — used internally by calculate_adx."""
    high  = df["high"].to_numpy(dtype=float)
    low   = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    n = len(df)

    tr_list = []
    for i in range(n):
        if i == 0:
            tr = high[0] - low[0]
        else:
            tr = max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i] - close[i - 1]),
            )
        tr_list.append(tr)

    return rma(pd.Series(tr_list), period)


# ─── ADX (Wilder's method) ───────────────────────────────────────────────────────

def calculate_adx(df: pd.DataFrame, period: int = ADX_PERIOD) -> np.ndarray:
    """Calculate ADX using Wilder's method.

    Measures trend strength (0-100); not direction.
    ADX < 25  = range-bound  (suitable for mean-reversion)
    ADX >= 25 = trending     (avoid mean-reversion entries)
    """
    high  = df["high"].to_numpy(dtype=float)
    low   = df["low"].to_numpy(dtype=float)
    n = len(df)

    atr = _calculate_atr(df, period)

    # Directional Movement
    plus_dm_list  = []
    minus_dm_list = []
    for i in range(n):
        if i == 0:
            plus_dm = 0.0
            minus_dm = 0.0
        else:
            plus_dm  = max(high[i] - high[i - 1], 0) if high[i] > high[i - 1] else 0.0
            minus_dm = max(low[i - 1] - low[i], 0)   if low[i] < low[i - 1]  else 0.0
            if plus_dm > 0 and minus_dm > 0:
                if plus_dm > minus_dm:
                    minus_dm = 0.0
                else:
                    plus_dm = 0.0
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)

    plus_dm_smooth  = rma(pd.Series(plus_dm_list),  period)
    minus_dm_smooth = rma(pd.Series(minus_dm_list), period)

    plus_di  = (plus_dm_smooth  / atr) * 100.0
    minus_di = (minus_dm_smooth / atr) * 100.0

    dx_list = []
    for i in range(len(plus_di)):
        di_sum = plus_di[i] + minus_di[i]
        dx = 100.0 * abs(plus_di[i] - minus_di[i]) / di_sum if di_sum != 0.0 else 0.0
        dx_list.append(dx)

    adx = rma(pd.Series(dx_list), period)
    return adx


# ─── RSI (Wilder's method) ───────────────────────────────────────────────────────

def calculate_rsi(df: pd.DataFrame, period: int = RSI_PERIOD) -> np.ndarray:
    """Calculate RSI using Wilder's method.

    RSI < 30 = oversold  (confirms LONG dip-buy)
    RSI > 50 = close above neutral — blocks LONG (price not confirmed oversold at close)

    Returns array aligned with df rows; first value is np.nan (lost due to diff).
    """
    close = df["close"].to_numpy(dtype=float)
    n = len(close)

    gains  = []
    losses = []
    for i in range(1, n):
        chg = close[i] - close[i - 1]
        if chg > 0:
            gains.append(chg)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-chg)

    avg_gain = rma(pd.Series(gains),  period)
    avg_loss = rma(pd.Series(losses), period)

    rs_list = []
    for i in range(len(avg_gain)):
        if avg_loss[i] == 0.0:
            rs = 100.0 if avg_gain[i] > 0.0 else 0.0
        else:
            rs = avg_gain[i] / avg_loss[i]
        rs_list.append(rs)

    rsi_vals    = np.array([100.0 - (100.0 / (1.0 + rs)) for rs in rs_list], dtype=float)
    rsi_aligned = np.concatenate([[np.nan], rsi_vals])
    return rsi_aligned


# ─── SMA (exit only) ─────────────────────────────────────────────────────────────

def calc_sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average — rolling mean over `period` bars. Used for Trend exit."""
    return series.rolling(window=period).mean()


# ─── Main indicator builder ──────────────────────────────────────────────────────

def build_indicators(
    df_raw: pd.DataFrame,
    ma_len: int,
    band_mult: float,
    exit_ma_len: Optional[int] = None,
    exit_band_mult: Optional[float] = None,
    band_ema_len: int = BAND_EMA_LENGTH,
    adx_period: int = ADX_PERIOD,
    rsi_period: int = RSI_PERIOD,
) -> pd.DataFrame:
    """Build all indicators needed for entry and exit.

    Required columns: ts, open, high, low, close, volume

    Adds:
        main           — RMA(close, ma_len)                              (centre line)
        premium_1..8   — EMA-smoothed premium bands   (exit signals;   always uses ma_len + band_mult)
        discount_1..8  — EMA-smoothed discount bands  (entry signals;  uses exit_ma_len + exit_band_mult
                          when provided, otherwise falls back to ma_len + band_mult)
        adx            — ADX(adx_period) Wilder (entry gate)
        rsi            — RSI(rsi_period) Wilder (entry gate)

    band_ema_len controls EMA smoothing applied to all 8 bands (both premium + discount).
    adx_period / rsi_period are optimised at runtime (default 14).
    """
    # Build entry (premium) bands — and discount bands from same params as baseline
    df = build_bands(df_raw.copy(), ma_len, band_mult, band_ema_len=band_ema_len)

    # Overwrite discount_k columns with exit-specific params when they differ
    _exit_ma = exit_ma_len    if exit_ma_len    is not None else ma_len
    _exit_bm = exit_band_mult if exit_band_mult is not None else band_mult
    if _exit_ma != ma_len or _exit_bm != band_mult or band_ema_len != BAND_EMA_LENGTH:
        df_exit = build_bands(df_raw.copy(), _exit_ma, _exit_bm, band_ema_len=band_ema_len)
        for k in range(1, 9):
            df[f"discount_{k}"] = df_exit[f"discount_{k}"].values

    df["adx"] = calculate_adx(df, adx_period)
    df["rsi"] = calculate_rsi(df, rsi_period)
    return df


# ─── Crossover / crossunder helpers ─────────────────────────────────────────────

def crossover(a_prev: float, a_cur: float, b_prev: float, b_cur: float) -> bool:
    """Detect when line A crosses ABOVE line B.

    Condition 1: a_prev <= b_prev  (A was at or below B previous bar)
    Condition 2: a_cur > b_cur     (A is above B current bar)
    """
    return (a_prev <= b_prev) and (a_cur > b_cur)


def crossunder(a_prev: float, a_cur: float, b_prev: float, b_cur: float) -> bool:
    """Detect when line A crosses BELOW line B.

    Condition 1: a_prev >= b_prev  (A was at or above B previous bar)
    Condition 2: a_cur < b_cur     (A is below B current bar)
    """
    return (a_prev >= b_prev) and (a_cur < b_cur)


# ─── Raw entry signal (band crossover scan) ─────────────────────────────────────

def compute_entry_signals_raw(
    current_row,    # dict or Series: must contain discount_1..8 and "low"
    prev_row,       # dict or Series: must contain discount_1..8 and "low"
    current_low: float,
) -> int:
    """Scan discount bands 8→1 for LONG entry signals.

    Signal fires when:
      price low crosses ABOVE discount_band
      (i.e. prev_low <= prev_band  AND  curr_low > curr_band)
      → LOW touched/exceeded the discount zone then bounced back; buy the dip.

    Returns 0 (no signal) or band level 1-8 that triggered (highest wins).
    Higher level = price was more extended below mean = stronger dip setup.
    """
    for band_level in range(8, 0, -1):
        key = f"discount_{band_level}"
        if crossover(
            float(prev_row["low"]),    # a_prev = prev candle low
            current_low,               # a_cur  = current candle low
            float(prev_row[key]),      # b_prev = prev band value
            float(current_row[key]),   # b_cur  = current band value
        ):
            return band_level
    return 0


# ─── Raw exit signal (discount band crossover scan — mirror of entry) ───────────

def compute_exit_signals_raw(
    current_row,    # dict or Series: must contain premium_1..8 and "high"
    prev_row,       # dict or Series: must contain premium_1..8 and "high"
    current_high: float,
) -> int:
    """Scan premium bands 8→1 for LONG exit signals.

    Exact mirror of compute_entry_signals_raw, applied to premium bands + HIGH:
        Entry: LOW  bounces back above discount_k → LONG (oversold dip)
        Exit:  HIGH crosses above premium_k       → CLOSE LONG (mean reversion complete)

    Signal fires when:
        price high crosses ABOVE premium_band
        (i.e. prev_high <= prev_premium  AND  curr_high > curr_premium)
        → HIGH has entered the premium zone; price has reverted enough to exit.

    Scans 8→1 (most extended first). Returns 0 (no signal) or band level 1-8.
    Higher level = larger move in our favor = more extended premium zone touch.
    No gates applied — exits are unconditional on band signal.
    """
    for band_level in range(8, 0, -1):
        key = f"premium_{band_level}"
        if crossover(
            float(prev_row["high"]),   # a_prev = prev candle high
            current_high,              # a_cur  = current candle high
            float(prev_row[key]),      # b_prev = prev premium band value
            float(current_row[key]),   # b_cur  = current premium band value
        ):
            return band_level
    return 0


# ─── Signal quality gates ────────────────────────────────────────────────────────

def resolve_entry_signals(
    raw_long: int,
    adx: float,
    rsi: float,
    adx_threshold: float = ADX_THRESHOLD,
    rsi_neutral_lo: float = RSI_NEUTRAL_LO,
) -> int:
    """Apply ADX and RSI gates to filter raw band-crossover entry signals.

    Gate 1 — ADX regime (checked first):
        ADX >= adx_threshold → trending market → block ALL LONG entries
    Gate 2 — RSI confirmation:
        RSI > rsi_neutral_lo → close above neutral → block LONG (close must confirm
                    oversold; don't buy a dip where the candle already closed strongly)

    adx_threshold and rsi_neutral_lo default to the module-level constants but can be
    overridden per-trial by the optimizer for parameter search.

    Returns final signal (0 = no trade, 1-8 = band level).
    """
    # Gate 1: ADX regime filter
    if adx >= adx_threshold:
        return 0

    # Gate 2: RSI confirmation
    final_long = raw_long
    if final_long != 0 and rsi > rsi_neutral_lo:
        final_long = 0

    return final_long


# Note: is_exit_signal (ADX/SMA-based) has been removed.
# Exits now use compute_exit_signals_raw (premium band crossovers), mirroring
# how compute_entry_signals_raw uses discount band crossovers for entries.
