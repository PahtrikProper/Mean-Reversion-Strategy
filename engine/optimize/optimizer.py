"""Parameter Optimizer — Mean Reversion Strategy

Searches for the best combination of:
  EntryParams: ma_len, band_mult
  ExitParams:  sl_pct  (tp_pct is fixed at DEFAULT_TP_PCT — not optimised)

ADX_THRESHOLD (25) and RSI_NEUTRAL_LO (40) are fixed — not optimised.

band_mult is stored as an integer × 10 (3 = 0.3, 100 = 10.0) during search
for efficient integer arithmetic, then converted to float for EntryParams.

tp_pct is FIXED at DEFAULT_TP_PCT (0.30%) — not part of the search space.

sl_pct is optimised in OPT_SL_MIN_BP–OPT_SL_MAX_BP (basis points × 0.0001).
  e.g. 500 bp = 5.00% above entry.  Wide by design — pre-liquidation guard.

Uses randomised search with exploitation/exploration split:
  EXPLOIT_RATIO of trials are sampled near the previously saved best params.
  The remainder are fully random within the configured search space.

Scoring: sort by (n_losses ASC, return_pct DESC) — fewest losing trades first.
"""

import numpy as np
import logging
import sys
import math
import time
import uuid
import os
import threading
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, Optional
from tqdm import tqdm

from ..utils.constants import (
    STARTING_WALLET,
    STOP_LOSS_PCT,
    DEFAULT_EXIT_MA_LEN,
    DEFAULT_EXIT_BAND_MULT,
    OPT_MA_LEN_MIN,             OPT_MA_LEN_MAX,
    OPT_BAND_MULT_X10_MIN,      OPT_BAND_MULT_X10_MAX,
    OPT_EXIT_MA_LEN_MIN,        OPT_EXIT_MA_LEN_MAX,
    OPT_EXIT_BAND_MULT_X10_MIN, OPT_EXIT_BAND_MULT_X10_MAX,
    OPT_SL_MIN_BP,              OPT_SL_MAX_BP,
    OPT_N_RANDOM,
    OPT_MIN_TRADES,
    RANDOM_SEED,
    EXPLOIT_RATIO,
    EXPLOIT_MA_LEN_RADIUS,
    EXPLOIT_BAND_MULT_RADIUS_X10,
    EXPLOIT_SL_RADIUS_BP,
    EXPLOIT_EXIT_MA_LEN_RADIUS,
    EXPLOIT_EXIT_BAND_MULT_RADIUS_X10,
    TIME_TP_HOURS,
    TIME_TP_FALLBACK_PCT,
    TIME_TP_SCALE,
)
from ..utils import constants as _C
from ..utils.data_structures import EntryParams, ExitParams
from ..utils import db_logger as _db
from ..utils.plotting import plot_pnl_chart
from ..backtest.backtester import backtest_once

log = logging.getLogger("optimizer")


def optimise_params(
    df_last: pd.DataFrame,
    df_mark: pd.DataFrame,
    risk_df: pd.DataFrame,
    trials: int,
    lookback_candles: int,
    event_name: str,
    leverage: float,
    fee_rate: float,
    maker_fee_rate: Optional[float] = None,
    interval_minutes: int = 5,
    saved_best: Optional[Dict[str, Any]] = None,
    exit_method_lock: Optional[str] = None,  # kept for API compat, unused
    progress_callback=None,
    verbose: bool = True,
    db_symbol: Optional[str] = None,
    db_interval: Optional[str] = None,
    db_trigger: str = "STARTUP",
) -> Dict[str, Any]:
    """Random search over (ma_len, band_mult, sl_pct, exit_ma_len, exit_band_mult).

    band_mult / exit_band_mult are searched as integer × 10 for efficiency.
    ADX_THRESHOLD=25 and RSI_NEUTRAL_LO=40 are fixed constants (not optimised).
    tp_pct is FIXED at DEFAULT_TP_PCT (0.30%) — not part of the search space.
    sl_pct is optimised in range OPT_SL_MIN_BP–OPT_SL_MAX_BP (0.50–9.00% above entry).
    exit_ma_len / exit_band_mult control the discount band geometry independently.

    Returns dict:
        {
          "entry_params": EntryParams,
          "exit_params":  ExitParams,
          "best_result":  BacktestResult,
          "all_results":  list of result dicts sorted by (n_losses ASC, return_pct DESC)
        }
    Raises RuntimeError if no valid runs found.
    """
    if maker_fee_rate is None:
        maker_fee_rate = fee_rate

    if lookback_candles > 0:
        dfl = df_last.iloc[-lookback_candles:].copy()
        dfm = df_mark.iloc[-lookback_candles:].copy()
    else:
        dfl = df_last.copy()
        dfm = df_mark.copy()

    rng = np.random.default_rng(RANDOM_SEED)

    # ── Build combo list ───────────────────────────────────────────────────────
    n_exploit = int(trials * EXPLOIT_RATIO) if saved_best else 0

    seen   = set()
    combos = []

    # Exploitation: sample near saved best
    if saved_best and n_exploit > 0:
        b_ma     = int(saved_best.get("ma_len", 100))
        b_bm_x10 = int(np.clip(
            round(float(saved_best.get("band_mult", 2.5)) * 10),
            OPT_BAND_MULT_X10_MIN, OPT_BAND_MULT_X10_MAX))
        b_sl_bp  = int(np.clip(
            round(float(saved_best.get("sl_pct", STOP_LOSS_PCT)) * 10000),
            OPT_SL_MIN_BP, OPT_SL_MAX_BP))
        b_exit_ma    = int(np.clip(
            saved_best.get("exit_ma_len", DEFAULT_EXIT_MA_LEN),
            OPT_EXIT_MA_LEN_MIN, OPT_EXIT_MA_LEN_MAX))
        b_exit_bm_x10 = int(np.clip(
            round(float(saved_best.get("exit_band_mult", DEFAULT_EXIT_BAND_MULT)) * 10),
            OPT_EXIT_BAND_MULT_X10_MIN, OPT_EXIT_BAND_MULT_X10_MAX))

        attempts = 0
        while len(combos) < n_exploit and attempts < n_exploit * 20:
            attempts += 1
            ma     = int(np.clip(
                rng.integers(b_ma - EXPLOIT_MA_LEN_RADIUS, b_ma + EXPLOIT_MA_LEN_RADIUS + 1),
                OPT_MA_LEN_MIN, OPT_MA_LEN_MAX))
            bm_x10 = int(np.clip(
                rng.integers(b_bm_x10 - EXPLOIT_BAND_MULT_RADIUS_X10,
                             b_bm_x10 + EXPLOIT_BAND_MULT_RADIUS_X10 + 1),
                OPT_BAND_MULT_X10_MIN, OPT_BAND_MULT_X10_MAX))
            sl_bp  = int(np.clip(
                rng.integers(b_sl_bp - EXPLOIT_SL_RADIUS_BP, b_sl_bp + EXPLOIT_SL_RADIUS_BP + 1),
                OPT_SL_MIN_BP, OPT_SL_MAX_BP))
            exit_ma = int(np.clip(
                rng.integers(b_exit_ma - EXPLOIT_EXIT_MA_LEN_RADIUS,
                             b_exit_ma + EXPLOIT_EXIT_MA_LEN_RADIUS + 1),
                OPT_EXIT_MA_LEN_MIN, OPT_EXIT_MA_LEN_MAX))
            exit_bm_x10 = int(np.clip(
                rng.integers(b_exit_bm_x10 - EXPLOIT_EXIT_BAND_MULT_RADIUS_X10,
                             b_exit_bm_x10 + EXPLOIT_EXIT_BAND_MULT_RADIUS_X10 + 1),
                OPT_EXIT_BAND_MULT_X10_MIN, OPT_EXIT_BAND_MULT_X10_MAX))
            key = (ma, bm_x10, sl_bp, exit_ma, exit_bm_x10)
            if key not in seen:
                seen.add(key)
                combos.append(key)

    # Exploration: fully random
    attempts = 0
    while len(combos) < trials and attempts < trials * 20:
        attempts += 1
        ma          = int(rng.integers(OPT_MA_LEN_MIN,             OPT_MA_LEN_MAX             + 1))
        bm_x10      = int(rng.integers(OPT_BAND_MULT_X10_MIN,      OPT_BAND_MULT_X10_MAX      + 1))
        sl_bp       = int(rng.integers(OPT_SL_MIN_BP,              OPT_SL_MAX_BP              + 1))
        exit_ma     = int(rng.integers(OPT_EXIT_MA_LEN_MIN,        OPT_EXIT_MA_LEN_MAX        + 1))
        exit_bm_x10 = int(rng.integers(OPT_EXIT_BAND_MULT_X10_MIN, OPT_EXIT_BAND_MULT_X10_MAX + 1))
        key = (ma, bm_x10, sl_bp, exit_ma, exit_bm_x10)
        if key not in seen:
            seen.add(key)
            combos.append(key)

    total = len(combos)
    if verbose:
        print(f"\nTesting {total} param combos  [{event_name}]")
        print(f"  Entry — MA-len {OPT_MA_LEN_MIN}-{OPT_MA_LEN_MAX}  "
              f"BandMult {OPT_BAND_MULT_X10_MIN/10:.1f}-{OPT_BAND_MULT_X10_MAX/10:.1f}%")
        print(f"  Exit  — TP fixed {_C.DEFAULT_TP_PCT*100:.2f}%  "
              f"SL {OPT_SL_MIN_BP*0.01:.2f}%-{OPT_SL_MAX_BP*0.01:.2f}%")
        print(f"  ExitBand — MA-len {OPT_EXIT_MA_LEN_MIN}-{OPT_EXIT_MA_LEN_MAX}  "
              f"BandMult {OPT_EXIT_BAND_MULT_X10_MIN/10:.1f}-{OPT_EXIT_BAND_MULT_X10_MAX/10:.1f}%")
        if saved_best:
            print(f"  Mode: {n_exploit} exploitation + {len(combos)-n_exploit} exploration")
        else:
            print(f"  Mode: {total} fully random")
        print(f"  Min trades filter: {OPT_MIN_TRADES}\n")

    _run_id    = str(uuid.uuid4())
    _t_start   = time.time()

    # Compute data-driven time TP once for the entire optimisation run.
    # Uses the same DB query that the live/paper traders call at 20h.
    _time_tp_pct: float = TIME_TP_FALLBACK_PCT
    if db_symbol:
        try:
            _time_tp_pct = _db.compute_time_tp_pct(
                symbol=db_symbol,
                min_hold_hours=TIME_TP_HOURS,
                fallback_pct=TIME_TP_FALLBACK_PCT,
                scale=TIME_TP_SCALE,
            )
        except Exception as _tte:
            log.debug(f"[OPT] compute_time_tp_pct failed: {_tte} — using fallback")
    if verbose:
        print(f"  Time TP: {_time_tp_pct*100:.3f}% after {TIME_TP_HOURS:.0f}h hold\n")

    results      = []
    results_lock = threading.Lock()
    _done_count  = [0]                 # mutable counter for progress callback
    pbar         = tqdm(total=total, desc="Optimising", unit="trial", leave=False) if verbose else None
    # Limit workers to 2 — more threads compete for the GIL during indicator
    # computation, starving the Tkinter main thread and causing the macOS
    # spinning beach-ball.  2 workers give a good speed/responsiveness trade-off.
    n_workers    = min(os.cpu_count() or 2, 2)

    def _run_trial(combo):
        ma, bm_x10, sl_bp, exit_ma, exit_bm_x10 = combo
        band_mult      = bm_x10      / 10.0
        exit_band_mult = exit_bm_x10 / 10.0
        tp = _C.DEFAULT_TP_PCT  # read at runtime so GUI changes take effect
        sl = sl_bp * 0.0001
        ep = EntryParams(ma_len=ma, band_mult=band_mult)
        xp = ExitParams(tp_pct=tp, sl_pct=sl,
                        exit_ma_len=exit_ma, exit_band_mult=exit_band_mult)
        res = backtest_once(
            dfl, dfm, risk_df, ep, xp, leverage, fee_rate, maker_fee_rate,
            time_tp_pct=_time_tp_pct,
            interval_minutes_bt=interval_minutes,
        )
        return ma, bm_x10, sl_bp, exit_ma, exit_bm_x10, band_mult, exit_band_mult, tp, sl, res

    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(_run_trial, combo): combo for combo in combos}
        for future in as_completed(futures):
            try:
                ma, bm_x10, sl_bp, exit_ma, exit_bm_x10, band_mult, exit_band_mult, tp, sl, res = future.result()
            except Exception as _exc:
                log.debug(f"[OPT] Trial raised: {_exc}")
                res = None

            with results_lock:
                _done_count[0] += 1
                idx = _done_count[0]

            if pbar:
                pbar.update(1)
            if progress_callback:
                try:
                    progress_callback(idx, total)
                except Exception:
                    pass

            if res is None or res.liquidated or res.trades < OPT_MIN_TRADES:
                continue

            wins      = [t for t in res.trade_records if t.pnl_net > 0]
            losses    = [t for t in res.trade_records if t.pnl_net < 0]
            avg_win   = sum(t.pnl_net for t in wins)   / len(wins)   if wins   else 0.0
            avg_loss  = sum(t.pnl_net for t in losses) / len(losses) if losses else 0.0
            gross_win  = sum(t.pnl_net for t in wins)
            gross_loss = abs(sum(t.pnl_net for t in losses))
            pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
            if math.isnan(pf):
                pf = 0.0

            row = {
                "ma_len":           ma,
                "band_mult":        band_mult,
                "tp_pct":           tp,
                "sl_pct":           sl,
                "exit_ma_len":      exit_ma,
                "exit_band_mult":   exit_band_mult,
                "trades":           res.trades,
                "n_wins":           len(wins),
                "n_losses":         len(losses),
                "win_rate":         res.winrate,
                "profit_factor":    pf,
                "return_pct":       res.pnl_pct,
                "pnl_usdt":         res.pnl_usdt,
                "avg_win":          avg_win,
                "avg_loss":         avg_loss,
                "max_drawdown_pct": res.max_drawdown_pct,
                "sharpe":           res.sharpe_ratio,
                "_result_obj":      res,
            }
            with results_lock:
                results.append(row)

    if pbar:
        pbar.close()
    sys.stdout.flush()

    if not results:
        raise RuntimeError("Optimiser: no valid runs found (insufficient data or no trades)")

    # Sort: fewest losing trades first; break ties by highest return
    results.sort(key=lambda r: (r["n_losses"], -r["return_pct"]))
    best = results[0]

    best_entry = EntryParams(
        ma_len=best["ma_len"],
        band_mult=best["band_mult"],
    )
    best_exit = ExitParams(
        tp_pct=best["tp_pct"],
        sl_pct=best["sl_pct"],
        exit_ma_len=best["exit_ma_len"],
        exit_band_mult=best["exit_band_mult"],
    )
    best_res = best["_result_obj"]

    if verbose:
        pf_str = f"{best['profit_factor']:.2f}" if best["profit_factor"] != float("inf") else "inf"
        print(
            f"\n✓ OPTIMISATION COMPLETE [{event_name}]:\n"
            f"  Entry    — MA-len={best_entry.ma_len}  BandMult={best_entry.band_mult:.2f}%\n"
            f"  ExitBand — MA-len={best_exit.exit_ma_len}  BandMult={best_exit.exit_band_mult:.2f}%\n"
            f"  TP={best_exit.tp_pct*100:.2f}%  SL={best_exit.sl_pct*100:.2f}%\n"
            f"  Wins={best['n_wins']}  Losses={best['n_losses']}  WinRate={best['win_rate']:.1f}%  "
            f"PF={pf_str}  Return={best['return_pct']:.2f}%  Trades={best['trades']}\n"
        )

    if verbose and best_res.wallet_history:
        plot_pnl_chart(best_res.wallet_history, float(STARTING_WALLET),
                       interval_minutes=interval_minutes)

    ts_utc = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M:%S")

    # ── DB: log optimization run + trials ─────────────────────────────────────
    _duration = time.time() - _t_start
    if db_symbol:
        try:
            _db.log_optimization_run(
                run_id=_run_id, ts_utc=ts_utc,
                symbol=db_symbol, interval=db_interval or "",
                trigger=db_trigger,
                total_trials=total, valid_trials=len(results),
                duration_sec=_duration,
                best_ma_len=best_entry.ma_len,
                best_band_mult=best_entry.band_mult,
                best_tp_pct=best_exit.tp_pct,
                best_pnl_pct=best_res.pnl_pct,
                best_n_losses=best.get("n_losses", 0),
                accepted=True,
            )
            _db.log_optimization_trials(_run_id, results)
        except Exception as _dbe:
            log.warning(f"[DB] Optimization logging failed: {_dbe}")

    return {
        "entry_params": best_entry,
        "exit_params":  best_exit,
        "best_result":  best_res,
        "all_results":  results,
        "_run_id":      _run_id,
    }


# Alias for compatibility
optimise_bayesian = optimise_params
