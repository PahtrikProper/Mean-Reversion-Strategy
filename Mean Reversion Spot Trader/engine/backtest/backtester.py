"""Backtesting Engine — Mean Reversion Strategy (LONG spot only)

Uses LAST-price OHLCV for all signal generation (bands, ADX gate, RSI gate, TP, SL).
No mark price or liquidation — spot trading has no forced liquidation.

Entry:
    low bounces back above discount_k band (band crossover)
    AND ADX < 25  (range-bound regime)
    AND RSI <= 50 (neutral-to-oversold close confirms the bounce)

Exit priority per candle:
  1. Trail Stop   (last low  <= highest_high_since_entry * (1 - trail_pct))     [Jason McIntosh; 0 = off]
  2. Take-profit  (last high >= tp_price = entry * (1 + tp_pct))                [fixed TP, optimised]
  3. Stop-Loss    (last low  <= sl_price = entry * (1 - sl_pct))                [hard floor guard]
  4. Band exit    (last high drops below premium_k band)                        [mirrors entry logic]

Trail stop tracks the highest candle-high since entry; fires when the low
falls more than trail_pct below that peak.  Never moves down.
"""

import pandas as pd
import numpy as np
import random
from typing import List, Optional

from ..utils.constants import (
    FEE_RATE,
    STARTING_WALLET,
    SLIPPAGE_TICKS,
    TICK_SIZE,
    TIME_TP_HOURS,
    VOL_FILTER_MAX_PCT,
    MAX_SYMBOL_FRACTION,
)
from ..utils.data_structures import TradeRecord, BacktestResult, MCSimResult, EntryParams, ExitParams, MC_SIMS, MC_MIN_TRADES
from ..core.indicators import (
    build_indicators,
    compute_entry_signals_raw,
    resolve_entry_signals,
    compute_exit_signals_raw,
)
from ..core.orders import apply_slippage


# ─── Single backtest run ────────────────────────────────────────────────────────

def backtest_once(
    df_last_raw: pd.DataFrame,
    df_mark_raw: pd.DataFrame,
    risk_df: pd.DataFrame,
    entry_params: EntryParams,
    exit_params: ExitParams,
    fee_rate: float = FEE_RATE,
    maker_fee_rate: Optional[float] = None,
    time_tp_pct: float = 0.0,
    interval_minutes_bt: int = 5,
) -> Optional[BacktestResult]:
    """Backtest Mean Reversion Strategy (LONG spot).

    Args:
        df_last_raw:         Last-price OHLCV (ts, open, high, low, close, volume)
        df_mark_raw:         Unused for spot (no liquidation); kept for API compat
        risk_df:             Unused for spot (no liquidation); kept for API compat
        entry_params:        EntryParams — includes adx_period, rsi_period
        exit_params:         ExitParams  — leverage fixed at 1.0 for spot
        fee_rate:            Taker fee rate (exits)
        maker_fee_rate:      Maker fee rate (entries); defaults to fee_rate
        time_tp_pct:         If > 0, override TP with this fraction after TIME_TP_HOURS
                             of position hold time (data-driven tighter exit).
                             0.0 disables the time-based TP override.
        interval_minutes_bt: Duration of each candle in minutes — used to convert
                             hold-candle-count into hours for the time TP check.

    Returns:
        BacktestResult or None if insufficient data.
    """
    if maker_fee_rate is None:
        maker_fee_rate = fee_rate

    min_len = max(entry_params.ma_len, exit_params.exit_ma_len) + 20

    if len(df_last_raw) < min_len + 20:
        return None

    dfl = df_last_raw.copy()

    if len(dfl) < 2:
        return None

    # ── Build all indicators on last-price data ───────────────────────────────
    dfl = build_indicators(
        dfl,
        ma_len=entry_params.ma_len,
        band_mult=entry_params.band_mult,
        exit_ma_len=exit_params.exit_ma_len,
        exit_band_mult=exit_params.exit_band_mult,
        band_ema_len=entry_params.band_ema_len,
        adx_period=entry_params.adx_period,
        rsi_period=entry_params.rsi_period,
    )

    # ── Backtest loop ─────────────────────────────────────────────────────────
    wallet               = float(STARTING_WALLET)
    pos_qty              = 0.0
    entry_price_bt       = 0.0
    entry_fee            = 0.0
    wallet_at_entry      = 0.0
    in_position          = False
    _highest_high_bt     = 0.0   # Jason McIntosh trailing stop — highest candle-high since entry

    # Time-based TP tightening — tracks when entry occurred and whether applied
    entry_candle_idx:   int  = 0
    entry_ts_ms_bt:     int  = 0   # ms timestamp of entry candle (for chart markers)
    time_tp_applied:    bool = False
    _time_tp_candles:   int  = (
        int(TIME_TP_HOURS * 60 / max(interval_minutes_bt, 1))
        if time_tp_pct > 0 else 0
    )

    trade_pnls:    List[float]       = []
    trade_records: List[TradeRecord] = []
    wallet_history: List[float]      = [wallet]

    for i in range(1, len(dfl)):
        row  = dfl.iloc[i]
        prev = dfl.iloc[i - 1]

        close      = float(row["close"])
        low_last   = float(row["low"])
        high_last  = float(row["high"])

        exited = False

        # ── Position management ───────────────────────────────────────────────
        if in_position and pos_qty != 0.0:
            qty_abs = abs(pos_qty)

            # Update McIntosh trailing stop tracker — never moves down
            _highest_high_bt = max(_highest_high_bt, high_last)

            # 1. Trailing stop (Jason McIntosh — trails below highest candle-high since entry)
            if exit_params.trail_pct > 0:
                trail_price = _highest_high_bt * (1.0 - float(exit_params.trail_pct))
                if low_last <= trail_price:
                    fill      = apply_slippage(trail_price, "sell")
                    pnl_gross = (fill - entry_price_bt) * qty_abs
                    exit_fee  = (qty_abs * fill) * fee_rate
                    wallet   += pnl_gross - exit_fee
                    pnl_net   = pnl_gross - entry_fee - exit_fee
                    trade_pnls.append(pnl_net)
                    _exit_ts_raw = dfl.iloc[i]["ts"]
                    _exit_ts_ms  = int(_exit_ts_raw.value) // 1_000_000 if hasattr(_exit_ts_raw, "value") else int(_exit_ts_raw)
                    trade_records.append(TradeRecord(
                        side="LONG", entry_price=entry_price_bt, exit_price=fill,
                        qty=qty_abs, entry_fee=entry_fee, exit_fee=exit_fee,
                        pnl_gross=pnl_gross, pnl_net=pnl_net,
                        reason="TRAIL_STOP", wallet_at_entry=wallet_at_entry,
                        hold_candles=i - entry_candle_idx,
                        entry_ts_ms=entry_ts_ms_bt, exit_ts_ms=_exit_ts_ms,
                    ))
                    pos_qty = 0.0; entry_price_bt = 0.0; entry_fee = 0.0
                    wallet_at_entry = 0.0; in_position = False
                    entry_candle_idx = 0; time_tp_applied = False
                    _highest_high_bt = 0.0
                    exited = True

            # 2. Take-profit (price moves up to TP target)
            # After TIME_TP_HOURS of hold, switch to the tighter time-based TP.
            if not exited and pos_qty != 0.0:
                _active_tp_pct = float(exit_params.tp_pct)
                if (time_tp_pct > 0
                        and _time_tp_candles > 0
                        and (i - entry_candle_idx) >= _time_tp_candles):
                    time_tp_applied = True
                    _active_tp_pct  = time_tp_pct
                tp_price = entry_price_bt * (1.0 + _active_tp_pct)
                if high_last >= tp_price:
                    fill      = apply_slippage(tp_price, "sell")
                    pnl_gross = (fill - entry_price_bt) * qty_abs
                    exit_fee  = (qty_abs * fill) * fee_rate
                    wallet   += pnl_gross - exit_fee
                    pnl_net   = pnl_gross - entry_fee - exit_fee
                    trade_pnls.append(pnl_net)
                    _exit_ts_raw = dfl.iloc[i]["ts"]
                    _exit_ts_ms  = int(_exit_ts_raw.value) // 1_000_000 if hasattr(_exit_ts_raw, "value") else int(_exit_ts_raw)
                    trade_records.append(TradeRecord(
                        side="LONG", entry_price=entry_price_bt, exit_price=fill,
                        qty=qty_abs, entry_fee=entry_fee, exit_fee=exit_fee,
                        pnl_gross=pnl_gross, pnl_net=pnl_net,
                        reason="TIME_TP" if time_tp_applied else "TP",
                        wallet_at_entry=wallet_at_entry,
                        hold_candles=i - entry_candle_idx,
                        entry_ts_ms=entry_ts_ms_bt, exit_ts_ms=_exit_ts_ms,
                    ))
                    pos_qty = 0.0; entry_price_bt = 0.0; entry_fee = 0.0
                    wallet_at_entry = 0.0; in_position = False
                    entry_candle_idx = 0; time_tp_applied = False
                    _highest_high_bt = 0.0
                    exited = True

            # 3. Hard stop-loss (price falls below entry * (1 - sl_pct))
            if not exited and pos_qty != 0.0:
                sl_price = entry_price_bt * (1.0 - float(exit_params.sl_pct))
                if low_last <= sl_price:
                    fill      = apply_slippage(close, "sell")
                    pnl_gross = (fill - entry_price_bt) * qty_abs
                    exit_fee  = (qty_abs * fill) * fee_rate
                    wallet   += pnl_gross - exit_fee
                    pnl_net   = pnl_gross - entry_fee - exit_fee
                    trade_pnls.append(pnl_net)
                    _exit_ts_raw = dfl.iloc[i]["ts"]
                    _exit_ts_ms  = int(_exit_ts_raw.value) // 1_000_000 if hasattr(_exit_ts_raw, "value") else int(_exit_ts_raw)
                    trade_records.append(TradeRecord(
                        side="LONG", entry_price=entry_price_bt, exit_price=fill,
                        qty=qty_abs, entry_fee=entry_fee, exit_fee=exit_fee,
                        pnl_gross=pnl_gross, pnl_net=pnl_net,
                        reason="STOP_LOSS", wallet_at_entry=wallet_at_entry,
                        hold_candles=i - entry_candle_idx,
                        entry_ts_ms=entry_ts_ms_bt, exit_ts_ms=_exit_ts_ms,
                    ))
                    pos_qty = 0.0; entry_price_bt = 0.0; entry_fee = 0.0
                    wallet_at_entry = 0.0; in_position = False
                    entry_candle_idx = 0; time_tp_applied = False
                    _highest_high_bt = 0.0
                    exited = True

            # 4. Band exit (high drops below premium_k — mirrors discount band entry)
            if not exited and pos_qty != 0.0:
                _raw_exit = compute_exit_signals_raw(
                    current_row=row, prev_row=prev,
                    current_high=high_last,
                )
                if _raw_exit > 0:
                    fill      = apply_slippage(close, "sell")
                    pnl_gross = (fill - entry_price_bt) * qty_abs
                    exit_fee  = (qty_abs * fill) * fee_rate
                    wallet   += pnl_gross - exit_fee
                    pnl_net   = pnl_gross - entry_fee - exit_fee
                    trade_pnls.append(pnl_net)
                    _exit_ts_raw = dfl.iloc[i]["ts"]
                    _exit_ts_ms  = int(_exit_ts_raw.value) // 1_000_000 if hasattr(_exit_ts_raw, "value") else int(_exit_ts_raw)
                    trade_records.append(TradeRecord(
                        side="LONG", entry_price=entry_price_bt, exit_price=fill,
                        qty=qty_abs, entry_fee=entry_fee, exit_fee=exit_fee,
                        pnl_gross=pnl_gross, pnl_net=pnl_net,
                        reason="BAND_EXIT",
                        wallet_at_entry=wallet_at_entry,
                        hold_candles=i - entry_candle_idx,
                        entry_ts_ms=entry_ts_ms_bt, exit_ts_ms=_exit_ts_ms,
                    ))
                    pos_qty = 0.0; entry_price_bt = 0.0; entry_fee = 0.0
                    wallet_at_entry = 0.0; in_position = False
                    entry_candle_idx = 0; time_tp_applied = False
                    _highest_high_bt = 0.0
                    exited = True

        # ── Entry ─────────────────────────────────────────────────────────────
        if not exited and not in_position and wallet > 0.0:
            _raw_long = compute_entry_signals_raw(
                current_row=row, prev_row=prev,
                current_low=low_last,
            )
            _rsi_val = float(row["rsi"]) if not pd.isna(row["rsi"]) else 0.0
            if resolve_entry_signals(
                _raw_long, float(row["adx"]), _rsi_val,
                adx_threshold=entry_params.adx_threshold,
                rsi_neutral_lo=entry_params.rsi_neutral_lo,
            ) > 0:
                # Volume liquidity filter — skip if our notional > VOL_FILTER_MAX_PCT
                # of the candle's USDT volume (catches pathologically thin candles).
                _candle_usdt_vol = float(row.get("volume", 0)) * close
                _pos_notional    = wallet * MAX_SYMBOL_FRACTION
                if _candle_usdt_vol > 0 and (_pos_notional / _candle_usdt_vol) > VOL_FILTER_MAX_PCT:
                    pass  # insufficient liquidity — skip entry
                else:
                    fill            = apply_slippage(close, "buy")
                    wallet_at_entry = wallet
                    notional        = wallet * MAX_SYMBOL_FRACTION
                    qty             = notional / fill
                    fee             = notional * maker_fee_rate
                    wallet         -= fee
                    pos_qty              = qty
                    entry_price_bt       = fill
                    entry_fee            = fee
                    in_position      = True
                    _highest_high_bt = fill   # start trail at entry fill price
                    entry_candle_idx = i
                    _ts_raw          = dfl.iloc[i]["ts"]
                    entry_ts_ms_bt   = int(_ts_raw.value) // 1_000_000 if hasattr(_ts_raw, "value") else int(_ts_raw)
                    time_tp_applied      = False

        # ── Same-bar exit — "Recalculate: After order is filled" (TradingView) ──
        # After entry fills on bar N, immediately re-check all exit conditions
        # on the same bar before advancing to bar N+1.
        if in_position and entry_candle_idx == i and not exited:
            qty_abs_sb = abs(pos_qty)
            # 1. Trailing stop (same-bar — Jason McIntosh)
            if exit_params.trail_pct > 0:
                _sb_trail_price = max(_highest_high_bt, high_last) * (1.0 - float(exit_params.trail_pct))
                if low_last <= _sb_trail_price:
                    fill      = apply_slippage(_sb_trail_price, "sell")
                    pnl_gross = (fill - entry_price_bt) * qty_abs_sb
                    exit_fee  = (qty_abs_sb * fill) * fee_rate
                    wallet   += pnl_gross - exit_fee
                    pnl_net   = pnl_gross - entry_fee - exit_fee
                    trade_pnls.append(pnl_net)
                    _exit_ts_raw = dfl.iloc[i]["ts"]
                    _exit_ts_ms  = int(_exit_ts_raw.value) // 1_000_000 if hasattr(_exit_ts_raw, "value") else int(_exit_ts_raw)
                    trade_records.append(TradeRecord(
                        side="LONG", entry_price=entry_price_bt, exit_price=fill,
                        qty=qty_abs_sb, entry_fee=entry_fee, exit_fee=exit_fee,
                        pnl_gross=pnl_gross, pnl_net=pnl_net,
                        reason="TRAIL_STOP", wallet_at_entry=wallet_at_entry,
                        hold_candles=0, entry_ts_ms=entry_ts_ms_bt, exit_ts_ms=_exit_ts_ms,
                    ))
                    pos_qty = 0.0; entry_price_bt = 0.0; entry_fee = 0.0
                    wallet_at_entry = 0.0; in_position = False
                    entry_candle_idx = 0; time_tp_applied = False
                    _highest_high_bt = 0.0
                    exited = True
            # 2. Take-profit (same-bar high)
            if not exited and pos_qty != 0.0:
                _sb_tp_price = entry_price_bt * (1.0 + float(exit_params.tp_pct))
                if high_last >= _sb_tp_price:
                    fill      = apply_slippage(_sb_tp_price, "sell")
                    pnl_gross = (fill - entry_price_bt) * qty_abs_sb
                    exit_fee  = (qty_abs_sb * fill) * fee_rate
                    wallet   += pnl_gross - exit_fee
                    pnl_net   = pnl_gross - entry_fee - exit_fee
                    trade_pnls.append(pnl_net)
                    _exit_ts_raw = dfl.iloc[i]["ts"]
                    _exit_ts_ms  = int(_exit_ts_raw.value) // 1_000_000 if hasattr(_exit_ts_raw, "value") else int(_exit_ts_raw)
                    trade_records.append(TradeRecord(
                        side="LONG", entry_price=entry_price_bt, exit_price=fill,
                        qty=qty_abs_sb, entry_fee=entry_fee, exit_fee=exit_fee,
                        pnl_gross=pnl_gross, pnl_net=pnl_net,
                        reason="TP", wallet_at_entry=wallet_at_entry,
                        hold_candles=0, entry_ts_ms=entry_ts_ms_bt, exit_ts_ms=_exit_ts_ms,
                    ))
                    pos_qty = 0.0; entry_price_bt = 0.0; entry_fee = 0.0
                    wallet_at_entry = 0.0; in_position = False
                    entry_candle_idx = 0; time_tp_applied = False
                    _highest_high_bt = 0.0
                    exited = True
            # 3. Stop-loss (same-bar low)
            if not exited and pos_qty != 0.0:
                sl_price_sb = entry_price_bt * (1.0 - float(exit_params.sl_pct))
                if low_last <= sl_price_sb:
                    fill      = apply_slippage(close, "sell")
                    pnl_gross = (fill - entry_price_bt) * qty_abs_sb
                    exit_fee  = (qty_abs_sb * fill) * fee_rate
                    wallet   += pnl_gross - exit_fee
                    pnl_net   = pnl_gross - entry_fee - exit_fee
                    trade_pnls.append(pnl_net)
                    _exit_ts_raw = dfl.iloc[i]["ts"]
                    _exit_ts_ms  = int(_exit_ts_raw.value) // 1_000_000 if hasattr(_exit_ts_raw, "value") else int(_exit_ts_raw)
                    trade_records.append(TradeRecord(
                        side="LONG", entry_price=entry_price_bt, exit_price=fill,
                        qty=qty_abs_sb, entry_fee=entry_fee, exit_fee=exit_fee,
                        pnl_gross=pnl_gross, pnl_net=pnl_net,
                        reason="STOP_LOSS", wallet_at_entry=wallet_at_entry,
                        hold_candles=0, entry_ts_ms=entry_ts_ms_bt, exit_ts_ms=_exit_ts_ms,
                    ))
                    pos_qty = 0.0; entry_price_bt = 0.0; entry_fee = 0.0
                    wallet_at_entry = 0.0; in_position = False
                    entry_candle_idx = 0; time_tp_applied = False
                    _highest_high_bt = 0.0
                    exited = True
            # 4. Band exit (same-bar signal)
            if not exited and pos_qty != 0.0:
                _raw_exit_sb = compute_exit_signals_raw(
                    current_row=row, prev_row=prev, current_high=high_last,
                )
                if _raw_exit_sb > 0:
                    fill      = apply_slippage(close, "sell")
                    pnl_gross = (fill - entry_price_bt) * qty_abs_sb
                    exit_fee  = (qty_abs_sb * fill) * fee_rate
                    wallet   += pnl_gross - exit_fee
                    pnl_net   = pnl_gross - entry_fee - exit_fee
                    trade_pnls.append(pnl_net)
                    _exit_ts_raw = dfl.iloc[i]["ts"]
                    _exit_ts_ms  = int(_exit_ts_raw.value) // 1_000_000 if hasattr(_exit_ts_raw, "value") else int(_exit_ts_raw)
                    trade_records.append(TradeRecord(
                        side="LONG", entry_price=entry_price_bt, exit_price=fill,
                        qty=qty_abs_sb, entry_fee=entry_fee, exit_fee=exit_fee,
                        pnl_gross=pnl_gross, pnl_net=pnl_net,
                        reason="BAND_EXIT", wallet_at_entry=wallet_at_entry,
                        hold_candles=0, entry_ts_ms=entry_ts_ms_bt, exit_ts_ms=_exit_ts_ms,
                    ))
                    pos_qty = 0.0; entry_price_bt = 0.0; entry_fee = 0.0
                    wallet_at_entry = 0.0; in_position = False
                    entry_candle_idx = 0; time_tp_applied = False
                    _highest_high_bt = 0.0
                    exited = True

        # ── Equity snapshot ───────────────────────────────────────────────────
        if pos_qty != 0.0:
            wallet_history.append(wallet + (close - entry_price_bt) * abs(pos_qty))
        else:
            wallet_history.append(wallet)

    # ── Stats ─────────────────────────────────────────────────────────────────
    n      = len(trade_pnls)
    wr     = (sum(1 for x in trade_pnls if x > 0) / n * 100.0) if n else 0.0
    pnl_u  = wallet - float(STARTING_WALLET)
    pnl_p  = (pnl_u / float(STARTING_WALLET)) * 100.0 if STARTING_WALLET else 0.0

    sharpe = 0.0
    if len(wallet_history) >= 2:
        wh   = np.array(wallet_history, dtype=np.float64)
        rets = np.diff(wh) / wh[:-1]
        std  = float(np.std(rets))
        if std > 1e-12:
            sharpe = float(np.mean(rets) / std)

    max_dd = 0.0
    peak   = wallet_history[0] if wallet_history else 1.0
    for w in wallet_history:
        if w > peak:
            peak = w
        if peak > 0:
            dd = (peak - w) / peak * 100.0
            if dd > max_dd:
                max_dd = dd

    # ── Hold time stats ────────────────────────────────────────────────────
    hold_mins = [tr.hold_candles * interval_minutes_bt for tr in trade_records]
    avg_hold  = float(sum(hold_mins) / len(hold_mins)) if hold_mins else 0.0
    min_hold  = float(min(hold_mins))                  if hold_mins else 0.0
    max_hold  = float(max(hold_mins))                  if hold_mins else 0.0

    return BacktestResult(
        final_wallet=float(wallet),
        pnl_usdt=float(pnl_u),
        pnl_pct=float(pnl_p),
        trades=int(n),
        winrate=float(wr),
        liquidated=False,
        sharpe_ratio=float(sharpe),
        max_drawdown_pct=float(max_dd),
        wallet_history=wallet_history,
        trade_records=trade_records,
        avg_hold_minutes=avg_hold,
        min_hold_minutes=min_hold,
        max_hold_minutes=max_hold,
    )


# ─── Monte Carlo ───────────────────────────────────────────────────────────────


def _mc_simulate_one(trades, starting_wallet, n_trades):
    # MCSimResult is already imported at module level; no need for a local import.
    wallet = starting_wallet
    peak = wallet; max_dd = 0.0
    losing_streak = max_losing_streak = 0
    wins = 0; equity = [wallet]
    for t in random.choices(trades, k=n_trades):
        if wallet <= 0:
            break
        pnl = wallet * t.return_pct
        wallet = max(0.0, wallet + pnl)
        wins += (pnl > 0)
        if pnl < 0:
            losing_streak += 1
            max_losing_streak = max(max_losing_streak, losing_streak)
        else:
            losing_streak = 0
        if wallet > peak:
            peak = wallet
        if peak > 0:
            max_dd = max(max_dd, (peak - wallet) / peak * 100.0)
        equity.append(wallet)
    actual = len(equity) - 1
    pnl_u  = wallet - starting_wallet
    pnl_p  = (pnl_u / starting_wallet * 100.0) if starting_wallet > 0 else 0.0
    wr     = (wins / actual * 100.0) if actual > 0 else 0.0
    sharpe = 0.0
    if len(equity) >= 2:
        eq   = np.array(equity, dtype=np.float64)
        mask = eq[:-1] > 0
        if np.sum(mask) >= 2:
            rets = np.diff(eq)[mask] / eq[:-1][mask]
            std  = float(np.std(rets))
            if std > 1e-12:
                sharpe = float(np.mean(rets) / std)
    return MCSimResult(
        final_wallet=wallet, pnl_usdt=pnl_u, pnl_pct=pnl_p,
        max_drawdown_pct=max_dd, max_losing_streak=max_losing_streak,
        trades=actual, wins=wins, winrate=wr, sharpe=sharpe, ruined=wallet <= 0,
    )


def run_monte_carlo(trade_records, starting_wallet, n_sims=MC_SIMS, n_trades=None):
    if not trade_records or len(trade_records) < MC_MIN_TRADES:
        return None
    if n_trades is None:
        n_trades = len(trade_records)
    return [_mc_simulate_one(trade_records, starting_wallet, n_trades) for _ in range(n_sims)]


def mc_score(results):
    if not results:
        return float("-inf")
    pnl_p = np.array([r.pnl_pct for r in results])
    dds   = np.array([r.max_drawdown_pct for r in results])
    pnls  = np.array([r.pnl_usdt for r in results])
    pp    = float(np.mean(pnls > 0))
    if pp <= 0:
        return float("-inf")
    return float(np.median(pnl_p)) * pp / (1.0 + float(np.percentile(dds, 95)))
