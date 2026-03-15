#!/usr/bin/env python3
"""
Scheduled Market Analysis — reads symbol from engine/config/default_config.json,
runs full optimisation + signal scan across all configured intervals, prints a
ranked summary report, and saves the best params so the bot warm-starts from them.

Usage:
    python scripts/run_analysis.py                # one-shot
    python scripts/run_analysis.py --loop         # loop every 8h (or --hours N)
    python scripts/run_analysis.py --symbol ETHUSDT  # override symbol

Cron (every 8 hours):
    0 */8 * * * cd /path/to/Mean-Reversion-Spot-LONG-Margin-Trader &&
        python3 scripts/run_analysis.py >> data/analysis.log 2>&1
"""

import sys
import os
import argparse
import json
import time
from datetime import datetime, timezone

# ── project root ────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# ── engine imports ───────────────────────────────────────────────────────────
from engine.trading.bybit_client import fetch_last_klines, fetch_mark_klines, fetch_risk_tiers
from engine.core.indicators import (
    build_indicators, compute_entry_signals_raw, resolve_entry_signals,
    ADX_THRESHOLD, RSI_NEUTRAL_LO,
)
from engine.optimize.optimizer import optimise_params
from engine.utils.helpers import leverage_for, taker_fee_for, maker_fee_for
import pandas as pd
import numpy as np


# ── paths ────────────────────────────────────────────────────────────────────
CONFIG_PATH      = os.path.join(PROJECT_ROOT, "engine", "config", "default_config.json")
BEST_PARAMS_PATH = os.path.join(PROJECT_ROOT, "data", "best_params.json")


# ── config helpers ────────────────────────────────────────────────────────────
def load_config():
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"  [WARN] Could not read config: {e}  — using defaults")
        return {}


def get_symbol_from_config():
    return load_config().get("symbol", "BTCUSDT")


def get_intervals_from_config():
    from engine.utils import constants as C
    return getattr(C, "CANDLE_INTERVALS", ["1", "3", "5"])


def save_best_params(best: dict):
    """
    Write best params to data/best_params.json AND update default_config.json
    entry/exit/interval sections so the bot warm-starts from them.
    """
    os.makedirs(os.path.dirname(BEST_PARAMS_PATH), exist_ok=True)

    payload = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "symbol":    best["symbol"],
        "interval":  best["interval"],
        "ma_len":    best["ma_len"],
        "band_mult": best["band_mult"],
        "tp_pct":    best["tp_pct"],
        "trades":    best["trades"],
        "win_rate":  best["win_rate"],
        "pnl_pct":   best["pnl_pct"],
        "max_dd":    best["max_dd"],
        "score":     best.get("score", 0),
        "avg_adx":   best.get("avg_adx", 0),
        "max_adx":   best.get("max_adx", 0),
        "fired":     best.get("fired", 0),
        "adx_blocked": best.get("adx_blocked", 0),
    }
    with open(BEST_PARAMS_PATH, "w") as f:
        json.dump(payload, f, indent=2)

    # ── also patch default_config.json so bot fallback defaults are up to date ─
    try:
        cfg = load_config()
        cfg.setdefault("entry", {})["ma_len"]    = best["ma_len"]
        cfg.setdefault("entry", {})["band_mult"]  = round(best["band_mult"], 4)
        cfg.setdefault("exit",  {})["tp_pct"]     = round(best["tp_pct"], 6)
        cfg["interval"] = best["interval"]
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
        print(f"  ✔  Params saved → data/best_params.json  +  default_config.json updated")
        print(f"     interval={best['interval']}m  MA={best['ma_len']}  "
              f"BandMult={best['band_mult']:.4f}%  TP={best['tp_pct']*100:.4f}%")
    except Exception as e:
        print(f"  [WARN] Could not update config: {e}")


# ── single-interval analysis ─────────────────────────────────────────────────
def analyse_interval(symbol: str, interval: str, days: int = 1, trials: int = 4000):
    end_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - int(days * 24 * 60 * 60 * 1000)

    print(f"    → Downloading {symbol} {interval}m  ({days}d) …", flush=True)
    try:
        df_last = fetch_last_klines(symbol, interval, start_ms, end_ms)
        time.sleep(0.5)
        df_mark = fetch_mark_klines(symbol, interval, start_ms, end_ms)
        time.sleep(0.5)
        risk_df = fetch_risk_tiers(symbol)
        time.sleep(0.3)
    except Exception as e:
        print(f"    [ERROR] Data fetch failed: {e}")
        return None

    if df_last is None or len(df_last) < 30:
        print(f"    [SKIP] Insufficient candles ({len(df_last) if df_last is not None else 0})")
        return None

    candle_count = len(df_last)
    _ts0    = df_last["ts"].iloc[0]
    _ts1    = df_last["ts"].iloc[-1]
    ts_from = _ts0.strftime("%Y-%m-%d %H:%M") if hasattr(_ts0, "strftime") else str(_ts0)
    ts_to   = _ts1.strftime("%Y-%m-%d %H:%M") if hasattr(_ts1, "strftime") else str(_ts1)

    # ── warm-start from previous best if same symbol/interval ───────────────
    saved_best = None
    try:
        if os.path.exists(BEST_PARAMS_PATH):
            with open(BEST_PARAMS_PATH) as f:
                prev = json.load(f)
            if prev.get("symbol") == symbol and prev.get("interval") == interval:
                saved_best = prev
                print(f"    → Warm-starting from previous best (MA={prev['ma_len']} BM={prev['band_mult']:.4f}%)")
    except Exception:
        pass

    print(f"    → Optimising {trials} trials …", flush=True)
    try:
        opt = optimise_params(
            df_last=df_last, df_mark=df_mark, risk_df=risk_df,
            trials=trials, lookback_candles=len(df_last),
            event_name=f"SCHED_{symbol}_{interval}m",
            fee_rate=taker_fee_for(symbol),
            maker_fee_rate=maker_fee_for(symbol),
            interval_minutes=int(interval),
            saved_best=saved_best,
            verbose=False,
        )
    except RuntimeError as e:
        print(f"    [ERROR] Optimisation failed: {e}")
        return {
            "symbol": symbol, "interval": interval,
            "candles": candle_count, "ts_from": ts_from, "ts_to": ts_to,
            "error": str(e),
        }

    ep = opt["entry_params"]
    xp = opt["exit_params"]
    br = opt["best_result"]

    # ── signal scan with best params ────────────────────────────────────────
    df = build_indicators(df_last.copy(), ma_len=ep.ma_len, band_mult=ep.band_mult)
    raw_signals = adx_blocked = rsi_blocked = fired = 0
    adx_vals = []

    for i in range(1, len(df)):
        row  = df.iloc[i]
        prev = df.iloc[i - 1]
        adx  = float(row["adx"])
        rsi  = float(row["rsi"]) if not pd.isna(row["rsi"]) else 100.0
        adx_vals.append(adx)

        raw = compute_entry_signals_raw(
            current_row=row, prev_row=prev,
            current_low=float(row["low"]),
        )
        if raw > 0:
            raw_signals += 1
            f = resolve_entry_signals(raw, adx, rsi)
            if f > 0:
                fired += 1
            elif adx >= ADX_THRESHOLD:
                adx_blocked += 1
            else:
                rsi_blocked += 1

    avg_adx = float(np.mean(adx_vals)) if adx_vals else 0.0
    max_adx = float(np.max(adx_vals))  if adx_vals else 0.0

    return {
        "symbol":      symbol,
        "interval":    interval,
        "candles":     candle_count,
        "ts_from":     ts_from,
        "ts_to":       ts_to,
        "ma_len":      ep.ma_len,
        "band_mult":   ep.band_mult,
        "tp_pct":      xp.tp_pct,
        "trades":      br.trades,
        "win_rate":    br.winrate,
        "pnl_pct":     br.pnl_pct,
        "max_dd":      br.max_drawdown_pct,
        "score":       br.pnl_pct / (1.0 + br.max_drawdown_pct) if br.max_drawdown_pct >= 0 else 0.0,
        "raw_signals": raw_signals,
        "adx_blocked": adx_blocked,
        "rsi_blocked": rsi_blocked,
        "fired":       fired,
        "avg_adx":     avg_adx,
        "max_adx":     max_adx,
    }


# ── full run (all intervals) ─────────────────────────────────────────────────
def run_analysis(symbol: str, days: int = 1, trials: int = 4000):
    intervals = get_intervals_from_config()
    run_ts    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print()
    print("=" * 65)
    print(f"  SCHEDULED MARKET ANALYSIS  |  {symbol}  |  {run_ts}")
    print("=" * 65)
    print(f"  Intervals : {', '.join(intervals)}m")
    print(f"  Days back : {days}")
    print(f"  Trials    : {trials}")
    print()

    results = []
    for iv in intervals:
        print(f"  ── Interval {iv}m ──────────────────────────────")
        res = analyse_interval(symbol, iv, days=days, trials=trials)
        if res:
            results.append(res)
        print()
        time.sleep(1.0)

    # ── ranked summary ───────────────────────────────────────────────────────
    print("=" * 65)
    print("  SUMMARY  (ranked by backtest score)")
    print("=" * 65)
    print(f"  {'IV':>3}  {'MA':>4}  {'BM%':>5}  {'TP%':>6}  {'Tr':>3}  {'WR%':>6}  {'PnL%':>7}  {'DD%':>6}  {'Sig':>4}  {'ADXblk':>7}  {'Fired':>6}")
    print("  " + "-" * 62)

    valid  = [r for r in results if "error" not in r]
    failed = [r for r in results if "error" in r]

    valid.sort(key=lambda r: r.get("score", -999), reverse=True)

    for r in valid:
        print(
            f"  {r['interval']:>3}m "
            f"  {r['ma_len']:>4}  "
            f"  {r['band_mult']:.2f}  "
            f"  {r['tp_pct']*100:.2f}%  "
            f"  {r['trades']:>3}  "
            f"  {r['win_rate']*100:.1f}%  "
            f"  {r['pnl_pct']*100:>+6.2f}%  "
            f"  {r['max_dd']*100:.1f}%  "
            f"  {r['raw_signals']:>4}  "
            f"  {r['adx_blocked']:>7}  "
            f"  {r['fired']:>6}"
        )

    if valid:
        best = valid[0]
        print()
        print(f"  ★  BEST INTERVAL: {best['interval']}m  |  "
              f"MA={best['ma_len']}  BandMult={best['band_mult']:.4f}%  TP={best['tp_pct']*100:.4f}%")
        print(f"     Trades={best['trades']}  WR={best['win_rate']*100:.1f}%  "
              f"PnL={best['pnl_pct']*100:+.2f}%  MaxDD={best['max_dd']*100:.1f}%")
        print(f"     Signals fired={best['fired']} / raw={best['raw_signals']}  "
              f"(ADX blocked={best['adx_blocked']}, RSI blocked={best['rsi_blocked']})")
        print(f"     Avg ADX={best['avg_adx']:.1f}  Max ADX={best['max_adx']:.1f}")
        print()

        # ── save best params so bot can warm-start from them ────────────────
        save_best_params(best)

    for r in failed:
        print(f"  ✗  {r['interval']}m  FAILED: {r['error']}")

    print()
    print("=" * 65)
    print()

    return results


# ── entry point ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Scheduled market analysis")
    parser.add_argument("--symbol",  default=None,  help="Symbol override (default: read from config)")
    parser.add_argument("--days",    type=int, default=1,    help="Lookback days (default: 1)")
    parser.add_argument("--trials",  type=int, default=4000, help="Optimiser trials (default: 4000)")
    parser.add_argument("--loop",    action="store_true",    help="Run in a loop every --hours hours")
    parser.add_argument("--hours",   type=float, default=8.0, help="Loop interval in hours (default: 8)")
    args = parser.parse_args()

    symbol = args.symbol or get_symbol_from_config()
    print(f"  Symbol read from {'argument' if args.symbol else 'config'}: {symbol}")

    if args.loop:
        print(f"  Loop mode: every {args.hours:.1f} hours  (Ctrl-C to stop)")
        while True:
            run_analysis(symbol, days=args.days, trials=args.trials)
            symbol    = args.symbol or get_symbol_from_config()
            sleep_sec = int(args.hours * 3600)
            wake_at   = datetime.fromtimestamp(
                time.time() + sleep_sec, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M UTC")
            print(f"  Next run at {wake_at}  (sleeping {args.hours:.1f}h) …\n")
            time.sleep(sleep_sec)
    else:
        run_analysis(symbol, days=args.days, trials=args.trials)


if __name__ == "__main__":
    main()
