"""Parameter Optimizer — Mean Reversion Strategy

Searches for the best combination of:
  EntryParams: ma_len, band_mult
  ExitParams:  holding_days, tp_pct

ADX_THRESHOLD (25) and RSI_NEUTRAL_LO (40) are fixed — not optimised.

band_mult is stored as an integer × 10 (3 = 0.3, 100 = 10.0) during search
for efficient integer arithmetic, then converted to float for EntryParams.

tp_pct is optimised in the range OPT_TP_MIN_BP–OPT_TP_MAX_BP (basis points * 0.0001).
  e.g. 18 bp = 0.18% price move before leverage.  No stop-loss.

Uses randomised search with exploitation/exploration split:
  EXPLOIT_RATIO of trials are sampled near the previously saved best params.
  The remainder are fully random within the configured search space.

Scoring: sort by (n_losses ASC, return_pct DESC) — fewest losing trades first.
"""

import numpy as np
import logging
import sys
import math
import pandas as pd
from typing import Dict, Any, Optional
from tqdm import tqdm

from ..utils.constants import (
    STARTING_WALLET,
    DEFAULT_TP_PCT,
    OPT_MA_LEN_MIN,        OPT_MA_LEN_MAX,
    OPT_BAND_MULT_X10_MIN, OPT_BAND_MULT_X10_MAX,
    OPT_HOLDING_MIN,       OPT_HOLDING_MAX,
    OPT_TP_MIN_BP,         OPT_TP_MAX_BP,
    OPT_N_RANDOM,
    OPT_MIN_TRADES,
    RANDOM_SEED,
    EXPLOIT_RATIO,
    EXPLOIT_MA_LEN_RADIUS,
    EXPLOIT_BAND_MULT_RADIUS_X10,
    EXPLOIT_HOLDING_RADIUS,
    EXPLOIT_TP_RADIUS_BP,
    PARAMS_CSV_PATH,
)
from ..utils.data_structures import EntryParams, ExitParams
from ..utils.logger import csv_append
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
) -> Dict[str, Any]:
    """Random search over (ma_len, band_mult, holding_days, tp_pct).

    band_mult is searched as integer × 10 for efficiency, converted to float for params.
    ADX_THRESHOLD=25 and RSI_NEUTRAL_LO=40 are fixed constants (not optimised).
    tp_pct is optimised in range OPT_TP_MIN_BP–OPT_TP_MAX_BP (0.18–0.38% price move).
    No stop-loss.

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
        b_ma    = int(saved_best.get("ma_len", 100))
        b_bm_x10 = int(np.clip(
            round(float(saved_best.get("band_mult", 2.5)) * 10),
            OPT_BAND_MULT_X10_MIN, OPT_BAND_MULT_X10_MAX))
        b_hd    = int(saved_best.get("holding_days", 30))
        b_tp_bp = int(np.clip(
            round(float(saved_best.get("tp_pct", DEFAULT_TP_PCT)) * 10000),
            OPT_TP_MIN_BP, OPT_TP_MAX_BP))

        attempts = 0
        while len(combos) < n_exploit and attempts < n_exploit * 20:
            attempts += 1
            ma    = int(np.clip(
                rng.integers(b_ma - EXPLOIT_MA_LEN_RADIUS, b_ma + EXPLOIT_MA_LEN_RADIUS + 1),
                OPT_MA_LEN_MIN, OPT_MA_LEN_MAX))
            bm_x10 = int(np.clip(
                rng.integers(b_bm_x10 - EXPLOIT_BAND_MULT_RADIUS_X10,
                             b_bm_x10 + EXPLOIT_BAND_MULT_RADIUS_X10 + 1),
                OPT_BAND_MULT_X10_MIN, OPT_BAND_MULT_X10_MAX))
            hd    = int(np.clip(
                rng.integers(b_hd - EXPLOIT_HOLDING_RADIUS, b_hd + EXPLOIT_HOLDING_RADIUS + 1),
                OPT_HOLDING_MIN, OPT_HOLDING_MAX))
            tp_bp = int(np.clip(
                rng.integers(b_tp_bp - EXPLOIT_TP_RADIUS_BP, b_tp_bp + EXPLOIT_TP_RADIUS_BP + 1),
                OPT_TP_MIN_BP, OPT_TP_MAX_BP))
            key = (ma, bm_x10, hd, tp_bp)
            if key not in seen:
                seen.add(key)
                combos.append(key)

    # Exploration: fully random
    attempts = 0
    while len(combos) < trials and attempts < trials * 20:
        attempts += 1
        ma     = int(rng.integers(OPT_MA_LEN_MIN,        OPT_MA_LEN_MAX        + 1))
        bm_x10 = int(rng.integers(OPT_BAND_MULT_X10_MIN, OPT_BAND_MULT_X10_MAX + 1))
        hd     = int(rng.integers(OPT_HOLDING_MIN,        OPT_HOLDING_MAX       + 1))
        tp_bp  = int(rng.integers(OPT_TP_MIN_BP,          OPT_TP_MAX_BP         + 1))
        key = (ma, bm_x10, hd, tp_bp)
        if key not in seen:
            seen.add(key)
            combos.append(key)

    total = len(combos)
    if verbose:
        print(f"\nTesting {total} param combos  [{event_name}]")
        print(f"  Entry — MA-len {OPT_MA_LEN_MIN}-{OPT_MA_LEN_MAX}  "
              f"BandMult {OPT_BAND_MULT_X10_MIN/10:.1f}-{OPT_BAND_MULT_X10_MAX/10:.1f}%")
        print(f"  Exit  — Hold {OPT_HOLDING_MIN}-{OPT_HOLDING_MAX}d  "
              f"TP {OPT_TP_MIN_BP*0.01:.2f}%-{OPT_TP_MAX_BP*0.01:.2f}% (band exit, no SL)")
        if saved_best:
            print(f"  Mode: {n_exploit} exploitation + {len(combos)-n_exploit} exploration")
        else:
            print(f"  Mode: {total} fully random")
        print(f"  Min trades filter: {OPT_MIN_TRADES}\n")

    results = []
    milestone = max(1, total // 10)
    pbar = tqdm(total=total, desc="Optimising", unit="trial", leave=False) if verbose else None

    for idx, (ma, bm_x10, hd, tp_bp) in enumerate(combos, 1):
        band_mult = bm_x10 / 10.0
        tp = tp_bp * 0.0001
        ep = EntryParams(ma_len=ma, band_mult=band_mult)
        xp = ExitParams(tp_pct=tp, holding_days=hd)
        res = backtest_once(dfl, dfm, risk_df, ep, xp, leverage, fee_rate, maker_fee_rate)
        if pbar:
            pbar.update(1)
        if progress_callback:
            try:
                progress_callback(idx, total)
            except Exception:
                pass

        if res is None or res.liquidated or res.trades < OPT_MIN_TRADES:
            continue

        wins   = [t for t in res.trade_records if t.pnl_net > 0]
        losses = [t for t in res.trade_records if t.pnl_net < 0]
        avg_win   = sum(t.pnl_net for t in wins)   / len(wins)   if wins   else 0.0
        avg_loss  = sum(t.pnl_net for t in losses)  / len(losses) if losses else 0.0
        gross_win  = sum(t.pnl_net for t in wins)
        gross_loss = abs(sum(t.pnl_net for t in losses))
        pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
        if math.isnan(pf):
            pf = 0.0

        results.append({
            "ma_len":      ma,
            "band_mult":   band_mult,
            "holding_days": hd,
            "tp_pct":      tp,
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
        })

        if verbose and idx % milestone == 0:
            print(f"  {idx/total*100:5.1f}%  ({idx}/{total})  valid: {len(results)}", flush=True)

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
        holding_days=best["holding_days"],
    )
    best_res = best["_result_obj"]

    if verbose:
        pf_str = f"{best['profit_factor']:.2f}" if best["profit_factor"] != float("inf") else "inf"
        print(
            f"\n✓ OPTIMISATION COMPLETE [{event_name}]:\n"
            f"  Entry — MA-len={best_entry.ma_len}  BandMult={best_entry.band_mult:.2f}%\n"
            f"  Exit  — Hold={best_exit.holding_days}d  TP={best_exit.tp_pct*100:.2f}%\n"
            f"  Wins={best['n_wins']}  Losses={best['n_losses']}  WinRate={best['win_rate']:.1f}%  "
            f"PF={pf_str}  Return={best['return_pct']:.2f}%  Trades={best['trades']}\n"
        )

    if verbose and best_res.wallet_history:
        plot_pnl_chart(best_res.wallet_history, float(STARTING_WALLET),
                       interval_minutes=interval_minutes)

    ts_utc = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M:%S")
    csv_append(PARAMS_CSV_PATH, [
        ts_utc, event_name,
        best_entry.ma_len, round(best_entry.band_mult, 4),
        best_exit.holding_days,
        round(best_exit.tp_pct, 6),
        round(best_res.final_wallet, 6),
        round(best_res.sharpe_ratio, 6),
    ])

    return {
        "entry_params": best_entry,
        "exit_params":  best_exit,
        "best_result":  best_res,
        "all_results":  results,
    }


# Alias for compatibility
optimise_bayesian = optimise_params
