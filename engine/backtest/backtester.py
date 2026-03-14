"""Backtesting Engine — Mean Reversion Strategy

Uses LAST-price OHLCV for signal generation (bands, ADX gate, RSI gate, TP).
Uses MARK-price OHLCV for liquidation checks (mark high >= liq_price).

Entry:
    high drops back below premium_k band (band crossover)
    AND ADX < 25  (range-bound regime)
    AND RSI >= 50 (neutral-to-overbought close confirms the fade)

Exit priority per candle:
  1. Liquidation  (mark high >= liq_price)
  2. Take-profit  (last low  <= tp_price)                       [fixed TP, optimised]
  3. Stop-Loss    (last high >= entry * (1 + sl_pct))           [wide, pre-liquidation guard]
  4. Band exit    (last low drops below discount_k band)        [mirrors entry logic]

Stop-loss fires when last high >= entry * (1 + sl_pct) — wide guard before liquidation.
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
)
from ..utils.data_structures import TradeRecord, BacktestResult, MCSimResult, EntryParams, ExitParams, MC_SIMS, MC_MIN_TRADES
from ..core.indicators import (
    build_indicators,
    compute_entry_signals_raw,
    resolve_entry_signals,
    compute_exit_signals_raw,
)
from ..core.orders import apply_slippage
from ..trading.liquidation import liquidation_price_short_isolated, pick_risk_tier


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
    """Backtest Mean Reversion Strategy.

    Args:
        df_last_raw:         Last-price OHLCV (ts, open, high, low, close, volume)
        df_mark_raw:         Mark-price OHLCV (ts, open, high, low, close)
        risk_df:             Bybit risk tier table
        entry_params:        EntryParams — includes adx_period, rsi_period
        exit_params:         ExitParams  — includes leverage (optimised 2–14×), tp_pct, sl_pct
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
    leverage = float(exit_params.leverage)

    min_len = max(entry_params.ma_len, exit_params.exit_ma_len) + 20

    if len(df_last_raw) < min_len + 20:
        return None

    # ── Align last-price and mark-price by timestamp ──────────────────────────
    dfl = df_last_raw.set_index("ts")
    dfm = df_mark_raw.set_index("ts")
    common = dfl.index.intersection(dfm.index)
    if len(common) < min_len + 20:
        return None

    dfl = dfl.loc[common].reset_index()
    dfm = dfm.loc[common].reset_index()

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
    liquidated           = False

    # Time-based TP tightening — tracks when entry occurred and whether applied
    entry_candle_idx:   int  = 0
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
        mrow = dfm.iloc[i]

        close      = float(row["close"])
        low_last   = float(row["low"])
        high_last  = float(row["high"])
        mark_high  = float(mrow["high"])
        mark_close = float(mrow["close"])

        exited = False

        # ── Position management ───────────────────────────────────────────────
        if in_position and pos_qty != 0.0:
            qty_abs = abs(pos_qty)
            tier    = pick_risk_tier(risk_df, qty_abs * mark_close)

            # 1. Liquidation (mark price)
            liq = liquidation_price_short_isolated(
                entry_price_bt, pos_qty, leverage, mark_close, tier, fee_rate
            )
            if mark_high >= liq:
                pnl_gross = (entry_price_bt - liq) * qty_abs
                exit_fee  = (qty_abs * liq) * fee_rate
                wallet   += pnl_gross - exit_fee
                wallet    = max(0.0, wallet)
                pnl_net   = pnl_gross - entry_fee - exit_fee
                trade_pnls.append(pnl_net)
                trade_records.append(TradeRecord(
                    side="SHORT", entry_price=entry_price_bt, exit_price=liq,
                    qty=qty_abs, entry_fee=entry_fee, exit_fee=exit_fee,
                    pnl_gross=pnl_gross, pnl_net=pnl_net,
                    reason="LIQUIDATION", wallet_at_entry=wallet_at_entry,
                ))
                wallet_history.append(wallet)
                liquidated = True
                break

            # 2. Take-profit (fixed or data-driven time override)
            # After TIME_TP_HOURS of hold, switch to the tighter time-based TP.
            _active_tp_pct = float(exit_params.tp_pct)
            if (time_tp_pct > 0
                    and _time_tp_candles > 0
                    and (i - entry_candle_idx) >= _time_tp_candles):
                time_tp_applied = True
                _active_tp_pct  = time_tp_pct
            tp_price = entry_price_bt * (1.0 - _active_tp_pct)
            if low_last <= tp_price:
                fill      = apply_slippage(tp_price, "buy")
                pnl_gross = (entry_price_bt - fill) * qty_abs
                exit_fee  = (qty_abs * fill) * fee_rate
                wallet   += pnl_gross - exit_fee
                pnl_net   = pnl_gross - entry_fee - exit_fee
                trade_pnls.append(pnl_net)
                trade_records.append(TradeRecord(
                    side="SHORT", entry_price=entry_price_bt, exit_price=fill,
                    qty=qty_abs, entry_fee=entry_fee, exit_fee=exit_fee,
                    pnl_gross=pnl_gross, pnl_net=pnl_net,
                    reason="TIME_TP" if time_tp_applied else "TP",
                    wallet_at_entry=wallet_at_entry,
                ))
                pos_qty = 0.0; entry_price_bt = 0.0; entry_fee = 0.0
                wallet_at_entry = 0.0; in_position = False
                entry_candle_idx = 0; time_tp_applied = False
                exited = True

            # 3. Hard stop-loss (price rises above entry * (1 + sl_pct))
            # Wide by design — fires before liquidation, rarely triggered in
            # normal conditions.  Optimised alongside TP.
            if not exited and pos_qty != 0.0:
                sl_price = entry_price_bt * (1.0 + float(exit_params.sl_pct))
                if high_last >= sl_price:
                    fill      = apply_slippage(close, "buy")
                    pnl_gross = (entry_price_bt - fill) * qty_abs
                    exit_fee  = (qty_abs * fill) * fee_rate
                    wallet   += pnl_gross - exit_fee
                    pnl_net   = pnl_gross - entry_fee - exit_fee
                    trade_pnls.append(pnl_net)
                    trade_records.append(TradeRecord(
                        side="SHORT", entry_price=entry_price_bt, exit_price=fill,
                        qty=qty_abs, entry_fee=entry_fee, exit_fee=exit_fee,
                        pnl_gross=pnl_gross, pnl_net=pnl_net,
                        reason="STOP_LOSS", wallet_at_entry=wallet_at_entry,
                    ))
                    pos_qty = 0.0; entry_price_bt = 0.0; entry_fee = 0.0
                    wallet_at_entry = 0.0; in_position = False
                    entry_candle_idx = 0; time_tp_applied = False
                    exited = True

            # 4. Band exit (low drops below discount_k — mirrors premium band entry)
            if not exited and pos_qty != 0.0:
                _raw_exit = compute_exit_signals_raw(
                    current_row=row, prev_row=prev,
                    current_low=low_last,
                )
                if _raw_exit > 0:
                    fill      = apply_slippage(close, "buy")
                    pnl_gross = (entry_price_bt - fill) * qty_abs
                    exit_fee  = (qty_abs * fill) * fee_rate
                    wallet   += pnl_gross - exit_fee
                    pnl_net   = pnl_gross - entry_fee - exit_fee
                    trade_pnls.append(pnl_net)
                    trade_records.append(TradeRecord(
                        side="SHORT", entry_price=entry_price_bt, exit_price=fill,
                        qty=qty_abs, entry_fee=entry_fee, exit_fee=exit_fee,
                        pnl_gross=pnl_gross, pnl_net=pnl_net,
                        reason="BAND_EXIT",
                        wallet_at_entry=wallet_at_entry,
                    ))
                    pos_qty = 0.0; entry_price_bt = 0.0; entry_fee = 0.0
                    wallet_at_entry = 0.0; in_position = False
                    entry_candle_idx = 0; time_tp_applied = False
                    exited = True

        # ── Entry ─────────────────────────────────────────────────────────────
        if not exited and not in_position and wallet > 0.0:
            _raw_short = compute_entry_signals_raw(
                current_row=row, prev_row=prev,
                current_high=high_last,
            )
            _rsi_val = float(row["rsi"]) if not pd.isna(row["rsi"]) else 100.0
            if resolve_entry_signals(
                _raw_short, float(row["adx"]), _rsi_val,
                adx_threshold=entry_params.adx_threshold,
                rsi_neutral_lo=entry_params.rsi_neutral_lo,
            ) > 0:
                # Volume liquidity filter — skip if our notional > VOL_FILTER_MAX_PCT
                # of the candle's USDT volume (catches pathologically thin candles).
                _candle_usdt_vol = float(row.get("volume", 0)) * close
                _pos_notional    = wallet * leverage
                if _candle_usdt_vol > 0 and (_pos_notional / _candle_usdt_vol) > VOL_FILTER_MAX_PCT:
                    pass  # insufficient liquidity — skip entry
                else:
                    fill            = apply_slippage(close, "sell")
                    wallet_at_entry = wallet
                    notional        = wallet * leverage
                    qty             = notional / fill
                    fee             = notional * maker_fee_rate
                    wallet         -= fee
                    pos_qty              = -qty
                    entry_price_bt       = fill
                    entry_fee            = fee
                    in_position      = True
                    entry_candle_idx = i          # record candle index for time TP
                    time_tp_applied      = False

        # ── Equity snapshot ───────────────────────────────────────────────────
        if pos_qty != 0.0:
            wallet_history.append(wallet + (entry_price_bt - close) * abs(pos_qty))
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

    return BacktestResult(
        final_wallet=float(wallet),
        pnl_usdt=float(pnl_u),
        pnl_pct=float(pnl_p),
        trades=int(n),
        winrate=float(wr),
        liquidated=bool(liquidated),
        sharpe_ratio=float(sharpe),
        max_drawdown_pct=float(max_dd),
        wallet_history=wallet_history,
        trade_records=trade_records,
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
