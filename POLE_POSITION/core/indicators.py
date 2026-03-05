"""Mean Reversion Strategy — Indicator Calculations

Entry (SHORT only):
  1. Build premium bands: main = RMA(close, ma_len),
     premium_k = EMA(main * (1 + band_mult% * k), 5),  k in 1..8
  2. Raw signal: high[prev] >= premium_k[prev]  AND  high[now] < premium_k[now]
     (high drops back below premium band after touching/exceeding it)
  3. Gates: ADX < 25 (range-bound market) AND RSI >= 40 (not oversold)

Exit:
  TP:          low  <= entry * (1 - tp_pct)
  Trail Stop:  high >= min_low_since_entry + trail_atr_mult * ATR(trail_atr_period)
               (Jason McIntosh ATR trailing stop — SHORT version)
  Band exit:   low  drops below discount_k band (mirrors entry logic)
"""

import pandas as pd
import numpy as np

# ─── Fixed gate constants ────────────────────────────────────────────────────────

ADX_PERIOD     = 14
ADX_THRESHOLD  = 25.0
RSI_PERIOD     = 14
RSI_NEUTRAL_LO = 40.0
BAND_EMA_LENGTH = 5


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

def build_bands(df: pd.DataFrame, ma_len: int, band_mult: float) -> pd.DataFrame:
    """Construct 8 premium + 8 discount bands.

    main        = RMA(close, ma_len)
    premium_k   = EMA(main * (1 + band_mult * 0.01 * k), 5)
    discount_k  = EMA(main * (1 - band_mult * 0.01 * k), 5)
    where k in [1, 8]

    Example with band_mult=2.5:
      Band 1: main * 1.025 (premium), main * 0.975 (discount)
      Band 8: main * 1.200 (premium), main * 0.800 (discount)
    """
    df = df.copy()
    df["main"] = rma(df["close"], int(ma_len))
    main_values = df["main"].to_numpy(dtype=float)

    for k in range(1, 9):
        premium_raw = main_values * (1.0 + float(band_mult) * 0.01 * k)
        df[f"premium_{k}"] = ema(premium_raw, length=BAND_EMA_LENGTH)

        discount_raw = main_values * (1.0 - float(band_mult) * 0.01 * k)
        df[f"discount_{k}"] = ema(discount_raw, length=BAND_EMA_LENGTH)

    return df


# ─── ATR (Wilder's method) ───────────────────────────────────────────────────────

def calculate_atr(df: pd.DataFrame, period: int = 14) -> np.ndarray:
    """Wilder's Average True Range using RMA smoothing.

    Used for the Jason McIntosh ATR trailing stop:
        SHORT exit stop = min_low_since_entry + trail_atr_mult * ATR(trail_atr_period)
    Also reused internally by calculate_adx.
    """
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

    # Reuse calculate_atr for True Range smoothing
    atr = calculate_atr(df, period)

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

    RSI > 70 = overbought (confirms SHORT fade)
    RSI < 40 = deeply oversold (blocks SHORT — don't fade exhausted moves)

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
    trail_atr_period: int = 14,
) -> pd.DataFrame:
    """Build all indicators needed for entry and exit.

    Required columns: ts, open, high, low, close, volume

    Adds:
        main           — RMA(close, ma_len)
        premium_1..8   — EMA-smoothed premium bands   (entry signals)
        discount_1..8  — EMA-smoothed discount bands  (exit signals)
        adx            — ADX(14) Wilder (entry gate)
        rsi            — RSI(14) Wilder (entry gate)
        atr            — ATR(trail_atr_period) Wilder (Jason McIntosh trail stop)
    """
    df = build_bands(df_raw.copy(), ma_len, band_mult)
    df["adx"] = calculate_adx(df, ADX_PERIOD)
    df["rsi"] = calculate_rsi(df, RSI_PERIOD)
    df["atr"] = calculate_atr(df, trail_atr_period)
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
    current_row,    # dict or Series: must contain premium_1..8 and "high"
    prev_row,       # dict or Series: must contain premium_1..8 and "high"
    current_high: float,
    current_low: float,
) -> int:
    """Scan premium bands 8→1 for SHORT entry signals.

    Signal fires when:
      premium_band crosses ABOVE price high
      (i.e. prev_high >= prev_band  AND  curr_high < curr_band)

    Returns 0 (no signal) or band level 1-8 that triggered (highest wins).
    Higher level = price was more extended from mean = stronger fade setup.
    """
    for band_level in range(8, 0, -1):
        key = f"premium_{band_level}"
        if crossover(
            float(prev_row[key]),      # a_prev = prev band value
            float(current_row[key]),   # a_cur  = current band value
            float(prev_row["high"]),   # b_prev = prev candle high
            current_high,              # b_cur  = current candle high
        ):
            return band_level
    return 0


# ─── Raw exit signal (discount band crossover scan — mirror of entry) ───────────

def compute_exit_signals_raw(
    current_row,    # dict or Series: must contain discount_1..8 and "low"
    prev_row,       # dict or Series: must contain discount_1..8 and "low"
    current_low: float,
    current_high: float,
) -> int:
    """Scan discount bands 8→1 for SHORT exit signals.

    Exact mirror of compute_entry_signals_raw, applied to discount bands + LOW:
        Entry: HIGH drops back below premium_k  → SHORT (overextended upside)
        Exit:  LOW  drops back below discount_k → CLOSE SHORT (overextended downside)

    Signal fires when:
        discount_band crosses ABOVE price low
        (i.e. prev_low >= prev_discount  AND  curr_low < curr_discount)
        → LOW has entered the discount zone; price has reverted enough to exit.

    Scans 8→1 (most extended first). Returns 0 (no signal) or band level 1-8.
    Higher level = larger move in our favor = more extended discount zone touch.
    No gates applied — exits are unconditional on band signal.
    """
    for band_level in range(8, 0, -1):
        key = f"discount_{band_level}"
        if crossover(
            float(prev_row[key]),      # a_prev = prev discount band value
            float(current_row[key]),   # a_cur  = current discount band value
            float(prev_row["low"]),    # b_prev = prev candle low
            current_low,               # b_cur  = current candle low
        ):
            return band_level
    return 0


# ─── Signal quality gates ────────────────────────────────────────────────────────

def resolve_entry_signals(raw_short: int, adx: float, rsi: float) -> int:
    """Apply ADX and RSI gates to filter raw band-crossover entry signals.

    Gate 1 — ADX regime (checked first):
        ADX >= 25 → trending market → block ALL SHORT entries
    Gate 2 — RSI confirmation:
        RSI < 40  → already deeply oversold → block SHORT (don't chase)

    Returns final signal (0 = no trade, 1-8 = band level).
    """
    # Gate 1: ADX regime filter
    if adx >= ADX_THRESHOLD:
        return 0

    # Gate 2: RSI confirmation
    final_short = raw_short
    if final_short != 0 and rsi < RSI_NEUTRAL_LO:
        final_short = 0

    return final_short


# Note: is_exit_signal (ADX/SMA-based) has been removed.
# Exits now use compute_exit_signals_raw (discount band crossovers), mirroring
# how compute_entry_signals_raw uses premium band crossovers for entries.
