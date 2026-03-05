#!/usr/bin/env python3
"""
Mean Reversion Trader — Mean Reversion Strategy (SHORT only)

Entry:  high drops back below premium_k band (crossover)
        AND ADX < 25  (range-bound regime)
        AND RSI >= 40 (not deeply oversold)
Exit:   TP (fixed), time (holding_days), or band exit
        Band: low drops below discount_k band (mirrors entry logic)

Usage:
    python3 main.py                      # Start automated live trading
    python3 main.py --paper              # Paper trading (no API keys required)
    python3 main.py --config <path>      # Use custom configuration file
    python3 main.py --symbols XRPUSDT    # Override trading symbols
"""

import os
import sys
import json
import logging
import argparse
from pathlib import Path

# In dev mode, ensure the project root is importable.
# In a PyInstaller frozen bundle all modules are already embedded — skip this.
if not getattr(sys, 'frozen', False):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Cross-platform setup ──────────────────────────────────────────────────────
def _setup_platform() -> None:
    """Configure platform-specific settings so the bot runs on Windows, macOS, and Linux."""
    if sys.platform == "win32":
        # Reconfigure stdout/stderr to UTF-8 so Unicode box-drawing and emoji
        # characters render correctly in Windows Terminal / PowerShell / cmd.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, Exception):
                pass  # Python < 3.7 or stream already wrapped

    # colorama translates ANSI escape codes to Win32 console calls on Windows
    # and is a no-op on macOS/Linux, so it is always safe to initialise.
    try:
        import colorama
        colorama.init()
    except ImportError:
        pass  # colorama not installed — colors still work on macOS/Linux

_setup_platform()

import POLE_POSITION as bot_module

from POLE_POSITION.utils import constants as const_module
from POLE_POSITION.utils.constants import (
    SYMBOLS, CANDLE_INTERVALS, DAYS_BACK_SEED,
    DEFAULT_TP_PCT, MAX_ACTIVE_SYMBOLS,
    PAPER_SYMBOLS, PAPER_STARTING_BALANCE,
)
from POLE_POSITION.utils.api_key_prompt import ensure_api_credentials
from POLE_POSITION.utils.helpers import (
    leverage_for, taker_fee_for, maker_fee_for, supported_intervals,
)
from POLE_POSITION.utils.data_structures import EntryParams, ExitParams
from POLE_POSITION.trading.paper_trader import _download_seed as _paper_download_seed


class Config:
    """Load and manage configuration from POLE_POSITION/config/default_config.json"""

    @staticmethod
    def load(config_path=None):
        if config_path is None:
            # PyInstaller extracts bundled files to sys._MEIPASS; in dev mode
            # the config sits alongside main.py in the project root.
            if getattr(sys, 'frozen', False):
                script_dir = sys._MEIPASS
            else:
                script_dir = os.path.dirname(os.path.abspath(__file__))
            config_path = os.path.join(script_dir, "POLE_POSITION", "config", "default_config.json")
        if not os.path.exists(config_path):
            print(f"  Config not found at {config_path}  — using defaults")
            return None
        try:
            with open(config_path, "r") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            print(f"  Config parse error: {e}  — using defaults")
            return None

    @staticmethod
    def apply(cfg):
        if not cfg:
            return
        if "symbol"         in cfg: const_module.SYMBOLS          = [cfg["symbol"]]
        if "leverage"       in cfg: const_module.DEFAULT_LEVERAGE = float(cfg["leverage"])
        if "starting_wallet"in cfg: const_module.STARTING_WALLET  = float(cfg["starting_wallet"])
        if "entry" in cfg:
            ec = cfg["entry"]
            if "ma_len"    in ec: const_module.DEFAULT_MA_LEN    = int(ec["ma_len"])
            if "band_mult" in ec: const_module.DEFAULT_BAND_MULT = float(ec["band_mult"])
        if "exit" in cfg:
            xc = cfg["exit"]
            if "holding_days" in xc: const_module.DEFAULT_HOLDING_DAYS = int(xc["holding_days"])
            if "tp_pct"       in xc: const_module.DEFAULT_TP_PCT       = float(xc["tp_pct"])
        if "optimizer" in cfg:
            oc = cfg["optimizer"]
            if "n_trials" in oc: const_module.INIT_TRIALS = int(oc["n_trials"])


def run_live_trading():
    """
    Full automated flow:
      1. For each (symbol, interval) pair: download seed history + optimise
      2. Rank all pairs by score (win_rate × profit_factor / (1 + max_drawdown))
      3. Launch LiveRealTrader for the top-ranked pair per symbol
      4. Start WebSocket live trading
    """
    print("=" * 65)
    print(f"  Mean Reversion Trader  |  Mean Reversion Strategy  |  {', '.join(const_module.SYMBOLS)}")
    print("=" * 65)

    symbols   = const_module.SYMBOLS
    intervals = supported_intervals(const_module.CANDLE_INTERVALS)
    n_top     = const_module.MAX_ACTIVE_SYMBOLS

    client = bot_module.BybitPrivateClient()
    gate   = bot_module.PositionGate()

    # ── Sync leverage from Bybit before anything else ────────────────────────
    print("\n  Checking leverage on Bybit...")
    for symbol in symbols:
        live_lev = client.get_leverage(symbol)
        const_module.LEVERAGE_BY_SYMBOL[symbol] = live_lev
        const_module.DEFAULT_LEVERAGE = live_lev
        print(f"    {symbol}  leverage = {live_lev:.0f}x  (read from Bybit)")

    print(f"\n  Symbols   : {symbols}")
    print(f"  Intervals : {intervals}m")
    print(f"  Leverage  : {const_module.DEFAULT_LEVERAGE:.0f}x")
    print(f"  Seed days : {const_module.DAYS_BACK_SEED}")
    print(f"  Trials    : {const_module.INIT_TRIALS}")
    print(f"  Top live  : {n_top} trader(s)\n")

    # ── Phase 1: Optimise every (symbol, interval) pair ──────────────────────
    all_results = {}

    for symbol in symbols:
        risk_df = bot_module.fetch_risk_tiers(symbol)

        for interval in intervals:
            print(f"  Downloading seed history  {symbol} {interval}m ...")
            df_last, df_mark = bot_module.download_seed_history(
                symbol, const_module.DAYS_BACK_SEED, interval
            )
            print(f"    last-price candles : {len(df_last)}")
            print(f"    mark-price candles : {len(df_mark)}")

            print(f"\n  Optimising {symbol} {interval}m ...")
            opt = bot_module.optimise_bayesian(
                df_last         = df_last,
                df_mark         = df_mark,
                risk_df         = risk_df,
                trials          = const_module.INIT_TRIALS,
                lookback_candles= min(len(df_last), len(df_mark)),
                event_name      = f"INIT_{symbol}_{interval}m",
                leverage        = leverage_for(symbol),
                fee_rate        = taker_fee_for(symbol),
                maker_fee_rate  = maker_fee_for(symbol),
                interval_minutes= int(interval),
            )

            ep  = opt["entry_params"]
            xp  = opt["exit_params"]
            br  = opt["best_result"]
            pf  = br.pnl_pct / (1.0 + max(br.max_drawdown_pct, 0.001))

            print(
                f"  {symbol} {interval}m  "
                f"MA={ep.ma_len} BandMult={ep.band_mult:.2f}%  "
                f"Hold={xp.holding_days}d  "
                f"TP={xp.tp_pct*100:.4f}%  "
                f"WR={br.winrate:.1f}%  PnL={br.pnl_pct:.2f}%  DD={br.max_drawdown_pct:.1f}%"
            )

            all_results[(symbol, interval)] = {
                "entry_params": ep,
                "exit_params":  xp,
                "best_result":  br,
                "df_last":      df_last,
                "df_mark":      df_mark,
                "risk_df":      risk_df,
                "interval":     interval,
                "score":        pf,
            }

    # ── Phase 2: Rank and display ─────────────────────────────────────────────
    ranked = sorted(all_results.items(), key=lambda x: x[1]["score"], reverse=True)

    print(f"\n{'═'*80}")
    print("  Full Results — all (symbol, interval) pairs ranked by score")
    print(f"  Score = PnL% / (1 + DD%)  ←  higher is better")
    print(f"{'═'*80}")
    print(
        f"  {'Rank':<5} {'Symbol':<12} {'Int':>4}  "
        f"{'PnL%':>8}  {'DD%':>7}  {'Score':>9}  {'Trades':>7}  {'WR%':>6}  "
        f"{'MA':>5} {'BandMult':>9} {'Hold':>5} {'TP%':>6}"
    )
    print(f"  {'─'*4}  {'─'*11} {'─'*4}  {'─'*8}  {'─'*7}  {'─'*9}  {'─'*7}  {'─'*6}  "
          f"{'─'*5} {'─'*9} {'─'*5} {'─'*6}")
    for rank, ((sym, iv), d) in enumerate(ranked, 1):
        br = d["best_result"]
        ep = d["entry_params"]
        xp = d["exit_params"]
        print(
            f"  #{rank:<4} {sym:<12} {iv:>3}m  "
            f"{br.pnl_pct:>8.2f}%  {br.max_drawdown_pct:>6.1f}%  "
            f"{d['score']:>9.4f}  {br.trades:>7}  {br.winrate:>5.1f}%  "
            f"{ep.ma_len:>5} {ep.band_mult:>8.2f}% "
            f"{xp.holding_days:>5} {xp.tp_pct*100:>5.3f}%"
        )
    print(f"{'═'*80}\n")

    # ── Phase 3: Select top N (one best interval per symbol) ──────────────────
    selected = []
    seen_sym = set()
    for (sym, iv), data in ranked:
        if sym not in seen_sym:
            selected.append((sym, data))
            seen_sym.add(sym)
        if len(selected) == n_top:
            break

    print(f"  Top {n_top} selected for live trading:")
    for i, (sym, d) in enumerate(selected, 1):
        br = d["best_result"]
        ep = d["entry_params"]
        xp = d["exit_params"]
        print(
            f"    #{i}  {sym}  [{d['interval']}m]  "
            f"Score={d['score']:.4f}  PnL={br.pnl_pct:.2f}%  DD={br.max_drawdown_pct:.1f}%  "
            f"MA={ep.ma_len} BandMult={ep.band_mult:.2f}%  "
            f"Hold={xp.holding_days}d  "
            f"TP={xp.tp_pct*100:.4f}%"
        )
    print()

    # ── Phase 4: Instantiate live traders and start WebSocket ─────────────────
    traders = {}
    for sym, data in selected:
        print(f"  Initializing live trader  {sym}  [{data['interval']}m] ...")
        trader = bot_module.LiveRealTrader(
            symbol        = sym,
            df_last_seed  = data["df_last"],
            df_mark_seed  = data["df_mark"],
            risk_df       = data["risk_df"],
            entry_params  = data["entry_params"],
            exit_params   = data["exit_params"],
            client        = client,
            gate          = gate,
            interval      = data["interval"],
        )
        traders[sym] = trader

    print(f"\n  {len(traders)} trader(s) ready.  Starting WebSocket...\n")
    bot_module.start_live_ws(traders)
    print("\n  Live trading stopped.")


def run_paper_trading():
    """
    Paper trading flow — no API keys required.
    Uses public REST and WebSocket only.  Simulates fills with a virtual
    500 USDT wallet.  Behaviour is identical to live trading.

      1. For each (symbol, interval) pair: download public seed history + optimise
      2. Rank all pairs by score
      3. Launch PaperTrader for the top-ranked pair per symbol
      4. Start WebSocket (public feed — no auth)
    """
    print("=" * 65)
    print("  Mean Reversion Trader  |  PAPER TRADING MODE  |  No real orders")
    print(f"  Virtual wallet: ${PAPER_STARTING_BALANCE:.0f} USDT  |  Bybit public data only")
    print("=" * 65)

    symbols   = const_module.PAPER_SYMBOLS
    intervals = supported_intervals(const_module.CANDLE_INTERVALS)
    n_top     = const_module.MAX_ACTIVE_SYMBOLS

    gate = bot_module.PositionGate()

    # Apply the configured leverage to all paper symbols.
    # Always overwrite so --symbols overrides are respected correctly.
    for symbol in symbols:
        const_module.LEVERAGE_BY_SYMBOL[symbol] = const_module.DEFAULT_LEVERAGE

    print(f"\n  Symbols   : {symbols}")
    print(f"  Intervals : {intervals}m")
    print(f"  Leverage  : {const_module.DEFAULT_LEVERAGE:.0f}x (default — no Bybit auth)")
    print(f"  Seed days : {const_module.DAYS_BACK_SEED}")
    print(f"  Trials    : {const_module.INIT_TRIALS}\n")

    all_results = {}

    for symbol in symbols:
        # Per-symbol error handling so one invalid symbol (e.g. ESPUSDT if unlisted)
        # doesn't block the rest
        try:
            risk_df    = bot_module.fetch_risk_tiers(symbol)
            instrument = bot_module.get_instrument_info(symbol)
        except Exception as exc:
            print(f"  SKIP {symbol}: {exc}")
            continue

        for interval in intervals:
            try:
                print(f"  Downloading seed history  {symbol} {interval}m ...")
                df_last, df_mark = _paper_download_seed(symbol, const_module.DAYS_BACK_SEED, interval)
                print(f"    last-price candles : {len(df_last)}")
                print(f"    mark-price candles : {len(df_mark)}")

                print(f"\n  Optimising {symbol} {interval}m ...")
                opt = bot_module.optimise_bayesian(
                    df_last          = df_last,
                    df_mark          = df_mark,
                    risk_df          = risk_df,
                    trials           = const_module.INIT_TRIALS,
                    lookback_candles = min(len(df_last), len(df_mark)),
                    event_name       = f"PAPER_INIT_{symbol}_{interval}m",
                    leverage         = leverage_for(symbol),
                    fee_rate         = taker_fee_for(symbol),
                    maker_fee_rate   = maker_fee_for(symbol),
                    interval_minutes = int(interval),
                )

                ep  = opt["entry_params"]
                xp  = opt["exit_params"]
                br  = opt["best_result"]
                pf  = br.pnl_pct / (1.0 + max(br.max_drawdown_pct, 0.001))

                print(
                    f"  {symbol} {interval}m  "
                    f"MA={ep.ma_len} BandMult={ep.band_mult:.2f}%  "
                    f"Hold={xp.holding_days}d  "
                    f"TP={xp.tp_pct*100:.4f}%  "
                    f"WR={br.winrate:.1f}%  PnL={br.pnl_pct:.2f}%  DD={br.max_drawdown_pct:.1f}%"
                )

                all_results[(symbol, interval)] = {
                    "entry_params": ep,
                    "exit_params":  xp,
                    "best_result":  br,
                    "df_last":      df_last,
                    "df_mark":      df_mark,
                    "risk_df":      risk_df,
                    "instrument":   instrument,
                    "interval":     interval,
                    "score":        pf,
                }
            except Exception as exc:
                print(f"  SKIP {symbol} {interval}m: {exc}")

    if not all_results:
        print("\n  No valid (symbol, interval) pairs found. Exiting.")
        return

    # ── Rank ──────────────────────────────────────────────────────────────────
    ranked = sorted(all_results.items(), key=lambda x: x[1]["score"], reverse=True)

    print(f"\n{'═'*80}")
    print("  Full Results — all (symbol, interval) pairs ranked by score")
    print(f"  Score = PnL% / (1 + DD%)  ←  higher is better")
    print(f"{'═'*80}")
    print(
        f"  {'Rank':<5} {'Symbol':<12} {'Int':>4}  "
        f"{'PnL%':>8}  {'DD%':>7}  {'Score':>9}  {'Trades':>7}  {'WR%':>6}  "
        f"{'MA':>5} {'BandMult':>9} {'Hold':>5} {'TP%':>6}"
    )
    print(f"  {'─'*4}  {'─'*11} {'─'*4}  {'─'*8}  {'─'*7}  {'─'*9}  {'─'*7}  {'─'*6}  "
          f"{'─'*5} {'─'*9} {'─'*5} {'─'*6}")
    for rank, ((sym, iv), d) in enumerate(ranked, 1):
        br = d["best_result"]
        ep = d["entry_params"]
        xp = d["exit_params"]
        print(
            f"  #{rank:<4} {sym:<12} {iv:>3}m  "
            f"{br.pnl_pct:>8.2f}%  {br.max_drawdown_pct:>6.1f}%  "
            f"{d['score']:>9.4f}  {br.trades:>7}  {br.winrate:>5.1f}%  "
            f"{ep.ma_len:>5} {ep.band_mult:>8.2f}% "
            f"{xp.holding_days:>5} {xp.tp_pct*100:>5.3f}%"
        )
    print(f"{'═'*80}\n")

    # ── Select top N (one best interval per symbol) ───────────────────────────
    selected = []
    seen_sym = set()
    for (sym, iv), data in ranked:
        if sym not in seen_sym:
            selected.append((sym, data))
            seen_sym.add(sym)
        if len(selected) == n_top:
            break

    print(f"  Top {n_top} selected for paper trading:")
    for i, (sym, d) in enumerate(selected, 1):
        br = d["best_result"]
        ep = d["entry_params"]
        xp = d["exit_params"]
        print(
            f"    #{i}  {sym}  [{d['interval']}m]  "
            f"Score={d['score']:.4f}  PnL={br.pnl_pct:.2f}%  DD={br.max_drawdown_pct:.1f}%  "
            f"MA={ep.ma_len} BandMult={ep.band_mult:.2f}%  "
            f"Hold={xp.holding_days}d  "
            f"TP={xp.tp_pct*100:.4f}%"
        )
    print()

    # ── Instantiate PaperTraders and start WebSocket ──────────────────────────
    traders = {}
    for sym, data in selected:
        print(f"  Initializing paper trader  {sym}  [{data['interval']}m] ...")
        trader = bot_module.PaperTrader(
            symbol       = sym,
            df_last_seed = data["df_last"],
            df_mark_seed = data["df_mark"],
            risk_df      = data["risk_df"],
            entry_params = data["entry_params"],
            exit_params  = data["exit_params"],
            gate         = gate,
            interval     = data["interval"],
            instrument   = data["instrument"],
        )
        traders[sym] = trader

    print(f"\n  {len(traders)} paper trader(s) ready.  Starting WebSocket...\n")
    bot_module.start_live_ws(traders)
    print("\n  Paper trading stopped.")


def main():
    parser = argparse.ArgumentParser(
        description="Mean Reversion Trader — Automated Bybit Short-only Trading Bot"
    )
    parser.add_argument("--config",  type=str,   default=None, help="Path to config JSON")
    parser.add_argument("--symbols", type=str,   nargs="+",    help="Override symbols (e.g. XRPUSDT)")
    parser.add_argument("--paper",   action="store_true",      help="Paper trading mode — no API keys required")
    args = parser.parse_args()

    print("  Loading configuration...")
    cfg = Config.load(args.config)
    if cfg:
        Config.apply(cfg)
        src = args.config or "POLE_POSITION/config/default_config.json"
        print(f"    Config loaded from {src}")
    else:
        print("    Using default constants")

    if args.symbols:
        if args.paper:
            const_module.PAPER_SYMBOLS = args.symbols
        else:
            const_module.SYMBOLS = args.symbols
        print(f"    Symbols overridden: {args.symbols}")

    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(message)s",
        handlers= [logging.StreamHandler(sys.stdout)],
    )

    if args.paper:
        print("\n" + "=" * 65)
        run_paper_trading()
        return

    print("\n  Checking API credentials...")
    ensure_api_credentials()

    print("\n" + "=" * 65)
    run_live_trading()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Stopped by user")
        sys.exit(0)
    except Exception as e:
        print(f"\n  Error: {e}")
        raise
