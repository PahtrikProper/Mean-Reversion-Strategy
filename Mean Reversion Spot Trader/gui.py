#!/usr/bin/env python3
"""
Mean Reversion Trader — Graphical Interface
Run:   python gui.py
Build: python build.py --gui
"""

import csv
import json
import logging
import os
import queue
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# ── Frozen / path setup ───────────────────────────────────────────────────────
if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Platform setup (mirrors main.py) ─────────────────────────────────────────
def _setup_platform() -> None:
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    try:
        import colorama
        colorama.init()
    except ImportError:
        pass

_setup_platform()

# ── GUI framework ─────────────────────────────────────────────────────────────
try:
    import customtkinter as ctk
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
except ImportError:
    print("customtkinter is required.  Run:  pip install customtkinter")
    sys.exit(1)

import tkinter as tk
from tkinter import messagebox, ttk

# ── Bot imports ───────────────────────────────────────────────────────────────
import engine as bot
from engine.utils import constants as C
from engine.utils.constants import LIVE_TP_SCALE, REOPT_INTERVAL_SEC
from engine.utils.api_key_prompt import (
    CREDS_FILE,
    _load_credentials,
    _save_credentials,
    validate_api_credentials,
)
from engine.utils.helpers import (
    maker_fee_for,
    supported_intervals,
    taker_fee_for,
)
from engine.utils import db_logger as _db

# ── Config helpers (mirrors main.py Config) ───────────────────────────────────
def _load_config() -> Optional[dict]:
    base = sys._MEIPASS if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "engine", "config", "default_config.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _apply_config(cfg: dict) -> None:
    """Apply non-symbol config values at bot-run time."""
    if not cfg:
        return
    # NOTE: "symbol" key is intentionally NOT applied here — seeded at init.
    if "starting_wallet" in cfg: C.STARTING_WALLET  = float(cfg["starting_wallet"])
    if "entry" in cfg:
        ec = cfg["entry"]
        if "ma_len"    in ec: C.DEFAULT_MA_LEN    = int(ec["ma_len"])
        if "band_mult" in ec: C.DEFAULT_BAND_MULT = float(ec["band_mult"])
    if "exit" in cfg:
        xc = cfg["exit"]
        if "tp_pct"       in xc: C.DEFAULT_TP_PCT       = float(xc["tp_pct"])
    if "days_back_seed"   in cfg: C.DAYS_BACK_SEED      = int(cfg["days_back_seed"])
    C.CANDLE_INTERVALS = ["5"]    # fixed — 5m only
    if "risk_pct"         in cfg: C.MAX_SYMBOL_FRACTION  = float(cfg["risk_pct"])
    if "optimizer" in cfg:
        if "n_trials" in cfg["optimizer"]:
            C.INIT_TRIALS = int(cfg["optimizer"]["n_trials"])


def _save_config(updates: dict) -> None:
    """Persist a partial settings update into default_config.json.

    Merges `updates` into the existing file with one level of deep-merge for
    nested dicts (e.g. ``{"exit": {"tp_pct": 0.005}}`` correctly updates only
    that key inside the "exit" block, leaving other keys intact).

    Called by the GUI Apply buttons so that user changes to TP / leverage are
    picked up on the next bot start (or restart), not silently reverted by
    ``_apply_config()`` which reads the file on every ``_run()`` invocation.

    Errors are silently swallowed so a read-only filesystem never crashes the GUI.
    """
    base = sys._MEIPASS if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "engine", "config", "default_config.json")
    try:
        cfg: dict = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        for key, val in updates.items():
            if isinstance(val, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(val)
            else:
                cfg[key] = val
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


# ── Agent best-params warm-start helper ──────────────────────────────────────
_BEST_PARAMS_PATH   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "best_params.json")
_AGENT_PARAMS_MAX_AGE = 12 * 3600   # treat agent analysis as fresh for 12 h

def _load_agent_best_params(symbol: str, interval: str) -> Optional[dict]:
    """Return saved best-params if file exists, is < 12 h old, and matches symbol+interval."""
    try:
        if not os.path.exists(_BEST_PARAMS_PATH):
            return None
        with open(_BEST_PARAMS_PATH) as f:
            bp = json.load(f)
        if bp.get("symbol") != symbol or bp.get("interval") != interval:
            return None
        from datetime import timezone as _tz
        saved_at = datetime.fromisoformat(bp["timestamp_utc"])
        if (datetime.now(_tz.utc) - saved_at).total_seconds() > _AGENT_PARAMS_MAX_AGE:
            return None
        return bp
    except Exception:
        return None


# ── Log filter — blocks ALL strategy/IP details from reaching the GUI ─────────
#
# Any log line containing one of these strings is silently dropped.
# The list is intentionally broad so that no indicator value, parameter name,
# or backtest metric can ever leak to the end-user.
_BLOCKED = (
    # Entry parameter field names (appear in log dicts / reopt dumps)
    "ma_len", "band_mult",
    # indicator values printed each candle
    "ADX=", "RSI=",
    # per-candle parameter readout from status monitor
    "MA=", "BandMult=", "Lev=",
    # optimiser output
    "param combos", "Min trades filter", "OPTIMISATION COMPLETE",
    " exploitation", " exploration",
    "  valid: ", "Mode: ",
    # strategy name (internal)
    "Band Crossover", "Band-Crossover",
    # reopt parameter dump
    "new params →",
    # ranking / scoring tables printed by main.py
    "Score=", "PnL=", "DD=",
    # tp percentage
    "tp_pct",
)


def _is_safe(msg: str) -> bool:
    return not any(token in msg for token in _BLOCKED)


class _GUILogHandler(logging.Handler):
    """Routes filtered log lines into the GUI message queue."""

    def __init__(self, q: queue.Queue) -> None:
        super().__init__()
        self._q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            if _is_safe(msg):
                self._q.put(("log", msg))
        except Exception:
            pass


# ── Stats Poller — reads live trader objects from the bot thread ──────────────
class _StatsPoller(threading.Thread):
    """Polls the live traders dict every 2 s and pushes stats to the GUI queue."""

    def __init__(self, traders: Dict[str, Any], q: queue.Queue) -> None:
        super().__init__(daemon=True, name="stats-poller")
        self._traders = traders
        self._q       = q
        self._stop    = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.wait(2.0):
            try:
                self._push()
            except Exception:
                pass

    def _push(self) -> None:
        if not self._traders:
            return
        sample = next(iter(self._traders.values()), None)
        if sample is None:
            return

        total = sum(t.trade_count for t in self._traders.values())
        wins  = sum(t.win_count   for t in self._traders.values())
        wr    = (wins / total * 100) if total else 0.0

        pos_info: Optional[dict] = None
        for sym, t in self._traders.items():
            if t.position is not None:
                p      = t.position
                qty    = abs(p.qty)
                mark   = t.mark_price
                upnl   = (mark - p.entry_price) * qty
                notional = p.entry_price * qty
                pct    = (upnl / notional * 100) if notional else 0.0
                pos_info = {
                    "symbol":      sym,
                    "entry_price": p.entry_price,
                    "tp_price":    p.entry_price * (1.0 + t.exit_params.tp_pct * LIVE_TP_SCALE),
                    "mark_price":  mark,
                    "qty":         qty,
                    "upnl":        upnl,
                    "upnl_pct":    pct,
                }
                break

        signal_info: Optional[dict] = None
        for t in self._traders.values():
            if getattr(t, "last_signal", None) is not None:
                signal_info = dict(t.last_signal)
                break

        # Re-opt countdown — seconds until the next scheduled re-optimisation
        reopt_sec = 0.0
        for t in self._traders.values():
            lr = getattr(t, "last_reopt_time", None)
            if lr is not None:
                reopt_sec = max(0.0, REOPT_INTERVAL_SEC - (time.time() - lr))
                break

        # All traders share the same Bybit Unified account, so balance and
        # account PnL come from a single trader — summing would double-count.
        # Only realized_pnl_net is per-trade and correctly summed across symbols.
        account_pnl     = sample.account_pnl_usdt
        account_pnl_pct = sample.account_pnl_pct
        realized_pnl    = sum(t.realized_pnl_net for t in self._traders.values())

        self._q.put(("stats", {
            "balance":         sample.wallet,
            "realized_pnl":    realized_pnl,
            "account_pnl":     account_pnl,
            "account_pnl_pct": account_pnl_pct,
            "trades":          total,
            "wins":            wins,
            "losses":          total - wins,
            "wr":              wr,
            "position":        pos_info,
            "symbols":         list(self._traders.keys()),
            "signal":          signal_info,
            "next_reopt_sec":  reopt_sec,
        }))


# ── Bot Controller — runs the full bot flow in a background thread ────────────
class _BotController:
    """Owns the bot background thread and exposes start() / stop()."""

    def __init__(self, q: queue.Queue, stop_evt: threading.Event) -> None:
        self._q       = q
        self._stop    = stop_evt
        self._poller: Optional[_StatsPoller] = None

    def start(self) -> None:
        self._stop.clear()
        threading.Thread(target=self._run, daemon=True, name="bot-main").start()

    def stop(self) -> None:
        self._stop.set()
        if self._poller:
            self._poller.stop()

    # internal helpers ────────────────────────────────────────────────────────
    def _emit(self, *msg: Any) -> None:
        self._q.put(msg)

    def _log(self, msg: str) -> None:
        self._emit("log", msg)

    def _run(self) -> None:
        try:
            self._emit("status", "initializing")
            self._log("Starting up…")

            cfg = _load_config()
            if cfg:
                _apply_config(cfg)

            # Credentials
            from engine.utils.api_key_prompt import ensure_api_credentials
            ensure_api_credentials()

            symbols   = C.SYMBOLS
            intervals = supported_intervals(C.CANDLE_INTERVALS)
            client    = bot.BybitPrivateClient()
            gate      = bot.PositionGate()

            # ── Optimisation phase ────────────────────────────────────────────
            n_pairs  = len(symbols) * len(intervals)
            pair_idx = 0
            results: Dict = {}

            self._emit("agent",
                       f"🤖  Agent analysis starting — {', '.join(symbols)}  "
                       f"[{', '.join(intervals)}m]  ·  {C.INIT_TRIALS} trials each",
                       "Initialising…")

            for sym in symbols:
                if self._stop.is_set():
                    break
                risk_df = bot.fetch_risk_tiers(sym)

                for iv in intervals:
                    if self._stop.is_set():
                        break

                    pair_idx += 1
                    self._log(f"Downloading market data for {sym}…")
                    self._emit("agent",
                               f"  Downloading {sym} {iv}m seed data…",
                               f"Downloading {iv}m…")
                    df_last, df_mark = bot.download_seed_history(sym, C.DAYS_BACK_SEED, iv)
                    self._log(f"Market data ready ({len(df_last)} candles)")

                    self._emit("status", "optimizing")
                    self._emit("agent",
                               f"  Optimising {sym} {iv}m  ({len(df_last)} candles)  …",
                               f"Optimising {iv}m…")

                    # warm-start from agent's saved params if fresh
                    _saved = _load_agent_best_params(sym, iv)
                    if _saved:
                        self._emit("agent",
                                   f"  ↳ Warm-starting from previous agent params  "
                                   f"MA={_saved['ma_len']}  BM={_saved['band_mult']:.4f}%")

                    # closure to capture loop vars — throttled to 1 emit per
                    # 5-percentage-point bucket so the queue isn't flooded.
                    # 4000 trials → ≤21 callbacks per pair → ≤63 total for 3 pairs.
                    def _make_cb(pidx: int, n_pairs: int, _sym: str, _iv: str) -> Any:
                        _last = [-1]
                        def cb(done: int, total: int) -> None:
                            bucket = (done * 20) // total  # 0–20 (one per 5%)
                            if bucket == _last[0] and done < total:
                                return
                            _last[0] = bucket
                            pct = min(100, bucket * 5)
                            base = (pidx - 1) / n_pairs
                            frac = (done / total) / n_pairs
                            self._emit("progress", base + frac,
                                       f"Analysing {_sym} {_iv}m… {pct}%")
                        return cb

                    opt = bot.optimise_bayesian(
                        df_last=df_last,
                        df_mark=df_mark,
                        risk_df=risk_df,
                        trials=C.INIT_TRIALS,
                        lookback_candles=min(len(df_last), len(df_mark)),
                        event_name=f"INIT_{sym}_{iv}m",
                        fee_rate=taker_fee_for(sym),
                        maker_fee_rate=maker_fee_for(sym),
                        interval_minutes=int(iv),
                        saved_best=_saved,
                        progress_callback=_make_cb(pair_idx, n_pairs, sym, iv),
                        verbose=False,
                    )

                    ep = opt["entry_params"]
                    xp = opt["exit_params"]
                    br = opt["best_result"]
                    pf = br.pnl_pct / (1.0 + max(br.max_drawdown_pct, 0.001))
                    self._log("Market analysis complete.")
                    self._emit("agent",
                               f"  {iv}m  →  MA={ep.ma_len}  BandMult={ep.band_mult:.2f}%  "
                               f"TP={xp.tp_pct*100:.3f}%  │  "
                               f"Trades={br.trades}  WR={br.winrate:.0f}%  "
                               f"PnL={br.pnl_pct:+.2f}%  Score={pf:.4f}")

                    results[(sym, iv)] = {
                        **opt,
                        "df_last": df_last,
                        "df_mark": df_mark,
                        "risk_df": risk_df,
                        "interval": iv,
                        "score": pf,
                    }

                    # Seed candle_analytics so /api/ready returns true after
                    # first optimisation — chart browser opener polls this table.
                    # Must enrich df with indicators first so band columns are populated.
                    try:
                        _df_ind = bot.build_indicators(
                            df_last,
                            ma_len=ep.ma_len, band_mult=ep.band_mult,
                            exit_ma_len=xp.exit_ma_len, exit_band_mult=xp.exit_band_mult,
                            band_ema_len=ep.band_ema_len,
                            adx_period=ep.adx_period, rsi_period=ep.rsi_period,
                        )
                        _db.bulk_log_seed_analytics(
                            df=_df_ind, symbol=sym, interval=iv,
                            ma_len=ep.ma_len, band_mult=ep.band_mult,
                            exit_ma_len=xp.exit_ma_len,
                            exit_band_mult=float(xp.exit_band_mult),
                            sl_pct=float(xp.sl_pct),
                        )
                    except Exception as _ana_err:
                        self._log(f"[DB] bulk_log_seed_analytics: {_ana_err}")
                    try:
                        _db.bulk_log_backtest_trades(
                            trade_records=getattr(br, "trade_records", []) or [],
                            symbol=sym, interval=iv,
                            entry_params=ep, exit_params=xp,
                        )
                    except Exception as _bt_err:
                        self._log(f"[DB] bulk_log_backtest_trades: {_bt_err}")

            if self._stop.is_set():
                self._emit("status", "idle")
                self._log("Stopped by user.")
                self._emit("done",)
                return

            # ── Select best symbol / interval ────────────────────────────────
            ranked   = sorted(results.items(), key=lambda x: x[1]["score"], reverse=True)
            selected = []
            seen: set = set()
            for (sym, iv), d in ranked:
                if sym not in seen:
                    selected.append((sym, d))
                    seen.add(sym)
                if len(selected) == C.MAX_ACTIVE_SYMBOLS:
                    break

            # ── Emit best strategy params to GUI ─────────────────────────────
            if selected:
                _sym, _d = selected[0]
                _ep = _d["entry_params"]
                _xp = _d["exit_params"]
                _br = _d["best_result"]
                _n_wins   = sum(1 for t in _br.trade_records if t.pnl_net > 0)
                _n_losses = sum(1 for t in _br.trade_records if t.pnl_net < 0)
                self._emit("agent",
                           f"★  BEST: {_d['interval']}m  │  "
                           f"MA={_ep.ma_len}  BandMult={_ep.band_mult:.2f}%  "
                           f"TP={_xp.tp_pct*100:.3f}%  │  "
                           f"Trades={_br.trades}  WR={_br.winrate:.0f}%  "
                           f"PnL={_br.pnl_pct:+.2f}%  Score={_d['score']:.4f}",
                           f"Best: {_d['interval']}m")
                self._emit("best_params", {
                    "symbol":        _sym,
                    "interval":      _d["interval"],
                    "leverage":      _xp.leverage,
                    "ma_len":        _ep.ma_len,
                    "band_mult":     _ep.band_mult,
                    "tp_pct":        _xp.tp_pct * 100.0,
                    "sl_pct":        _xp.sl_pct * 100.0,
                    "exit_ma_len":   _xp.exit_ma_len,
                    "exit_band_mult": _xp.exit_band_mult,
                    "n_wins":        _n_wins,
                    "n_losses":      _n_losses,
                    "trades":        _br.trades,
                    "winrate":       _br.winrate,
                    "return_pct":    _br.pnl_pct,
                    "sharpe":        _br.sharpe_ratio,
                    "max_dd":        _br.max_drawdown_pct,
                })

            # ── Initialize live traders ───────────────────────────────────────
            traders: Dict[str, Any] = {}
            for sym, d in selected:
                self._log(f"Initializing trader for {sym}…")
                traders[sym] = bot.LiveRealTrader(
                    symbol=sym,
                    df_last_seed=d["df_last"],
                    df_mark_seed=d["df_mark"],
                    risk_df=d["risk_df"],
                    entry_params=d["entry_params"],
                    exit_params=d["exit_params"],
                    client=client,
                    gate=gate,
                    interval=d["interval"],
                )

            # ── Start stats poller ────────────────────────────────────────────
            self._poller = _StatsPoller(traders, self._q)
            self._poller.start()

            self._emit("status", "trading")
            self._emit("progress", 1.0, "")
            syms_str = ", ".join(traders.keys())
            self._log(f"Live trading active — {syms_str}")
            self._emit("agent",
                       f"🟢  Trading live — {syms_str}  │  Agent monitoring active",
                       "Trading")

            # ── Blocking WebSocket loop ───────────────────────────────────────
            bot.start_live_ws(traders, stop_event=self._stop)

        except Exception as exc:
            self._emit("error", str(exc))
            self._emit("status", "error")
        finally:
            if self._poller:
                self._poller.stop()
            self._emit("done",)





# ── Main Application ──────────────────────────────────────────────────────────
class App(ctk.CTk):
    _STATUS_MAP = {
        "idle":         ("#6e7681", "● STOPPED"),
        "initializing": ("#d29922", "● STARTING"),
        "optimizing":   ("#388bfd", "● ANALYZING"),
        "trading":      ("#3fb950", "● LIVE"),
        "stopping":     ("#d29922", "● STOPPING"),
        "error":        ("#f85149", "● ERROR"),
    }

    def __init__(self) -> None:
        super().__init__()
        self.title("Mean Reversion Trader")
        # Auto-fit to screen — leave room for the macOS/Windows taskbar
        _sw = self.winfo_screenwidth()
        _sh = self.winfo_screenheight()
        _win_w = min(1020, max(860, _sw - 40))
        _win_h = min(980,  max(780, _sh - 80))
        self.geometry(f"{_win_w}x{_win_h}")
        self.minsize(820, 720)
        self.resizable(True, True)

        self._q        = queue.Queue()
        self._stop_evt = threading.Event()
        self._ctrl: Optional[_BotController] = None
        self._running   = False
        self._api_open  = True
        self._risk_open = False  # Settings start collapsed
        # Line counters — used to trim textboxes and prevent slow inserts
        self._log_lines   = 0
        self._agent_lines = 0

        # Install filtered log handler
        handler = _GUILogHandler(self._q)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)

        # Seed C.SYMBOLS from the config file so the live trader picks up
        # whichever symbol the market-analyst agent last identified as best.
        _early_cfg = _load_config()
        if _early_cfg:
            if "symbol" in _early_cfg:
                C.SYMBOLS = [_early_cfg["symbol"]]
            if "starting_wallet" in _early_cfg: C.STARTING_WALLET  = float(_early_cfg["starting_wallet"])

        self._build_ui()
        self._load_saved_keys()
        self._poll()
        # Auto-start chart server (browser opens only after first optimisation)
        self.after(1500, self._start_chart_server)

    # ── UI Construction ───────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        # ── Outer scrollable container ────────────────────────────────────────
        self._scroll = ctk.CTkScrollableFrame(self, fg_color="#0d1117", corner_radius=0)
        self._scroll.pack(fill="both", expand=True)
        self._scroll.grid_columnconfigure(0, weight=1)

        # ── Header ────────────────────────────────────────────────────────────
        hdr = ctk.CTkFrame(self._scroll, height=60, fg_color="#0d1117", corner_radius=0)
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            hdr, text="Mean Reversion Trader",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).grid(row=0, column=0, padx=20, pady=16, sticky="w")
        self._lbl_status = ctk.CTkLabel(
            hdr, text="● STOPPED",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#6e7681",
        )
        self._lbl_status.grid(row=0, column=2, padx=20, pady=16, sticky="e")

        # ── API Credentials (collapsible) ─────────────────────────────────────
        api_outer = ctk.CTkFrame(self._scroll, fg_color="#161b22", corner_radius=8)
        api_outer.grid(row=1, column=0, sticky="ew", padx=10, pady=(8, 0))
        api_outer.grid_columnconfigure(0, weight=1)

        self._api_toggle_btn = ctk.CTkButton(
            api_outer,
            text="  API Credentials  ▲",
            fg_color="transparent",
            hover_color="#21262d",
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._toggle_api,
        )
        self._api_toggle_btn.grid(row=0, column=0, sticky="ew", padx=4, pady=4)

        self._api_body = ctk.CTkFrame(api_outer, fg_color="transparent")
        self._api_body.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 10))
        self._api_body.grid_columnconfigure(1, weight=1)

        # API Key row
        ctk.CTkLabel(self._api_body, text="API Key:", anchor="w", width=80).grid(
            row=0, column=0, pady=5, sticky="w")
        self._ent_key = ctk.CTkEntry(self._api_body, show="*", placeholder_text="Paste your Bybit API key")
        self._ent_key.grid(row=0, column=1, pady=5, sticky="ew", padx=(0, 6))
        self._btn_show_key = ctk.CTkButton(
            self._api_body, text="Show", width=64,
            command=lambda: self._toggle_show(self._ent_key, self._btn_show_key),
        )
        self._btn_show_key.grid(row=0, column=2, pady=5)

        # API Secret row
        ctk.CTkLabel(self._api_body, text="API Secret:", anchor="w", width=80).grid(
            row=1, column=0, pady=5, sticky="w")
        self._ent_secret = ctk.CTkEntry(self._api_body, show="*", placeholder_text="Paste your Bybit API secret")
        self._ent_secret.grid(row=1, column=1, pady=5, sticky="ew", padx=(0, 6))
        self._btn_show_sec = ctk.CTkButton(
            self._api_body, text="Show", width=64,
            command=lambda: self._toggle_show(self._ent_secret, self._btn_show_sec),
        )
        self._btn_show_sec.grid(row=1, column=2, pady=5)

        # Buttons row
        btn_row = ctk.CTkFrame(self._api_body, fg_color="transparent")
        btn_row.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ctk.CTkButton(btn_row, text="Save Keys", width=110,
                      command=self._save_keys).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btn_row, text="Clear Keys", width=110,
            fg_color="#3d1a1a", hover_color="#5c2a2a",
            command=self._clear_keys,
        ).pack(side="left")
        self._lbl_key_status = ctk.CTkLabel(btn_row, text="", text_color="#3fb950")
        self._lbl_key_status.pack(side="left", padx=14)

        # ── Settings (collapsible) ────────────────────────────────────────────
        risk_outer = ctk.CTkFrame(self._scroll, fg_color="#161b22", corner_radius=8)
        risk_outer.grid(row=2, column=0, sticky="ew", padx=10, pady=(8, 0))
        risk_outer.grid_columnconfigure(0, weight=1)

        self._risk_toggle_btn = ctk.CTkButton(
            risk_outer,
            text="  Settings  ▼",
            fg_color="transparent",
            hover_color="#21262d",
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._toggle_risk,
        )
        self._risk_toggle_btn.grid(row=0, column=0, sticky="ew", padx=4, pady=4)

        self._risk_body = ctk.CTkFrame(risk_outer, fg_color="transparent")
        # starts collapsed — _toggle_risk() will grid it when user clicks
        self._risk_body.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(
            self._risk_body, text="Risk Profile:",
            font=ctk.CTkFont(size=13), text_color="#c9d1d9",
        ).grid(row=0, column=0, padx=(14, 8), pady=10, sticky="w")

        _risk_options = [f"{p}%" for p in range(10, 100, 5)]
        self._risk_var = ctk.StringVar(value="45%")
        self._risk_menu = ctk.CTkOptionMenu(
            self._risk_body,
            values=_risk_options,
            variable=self._risk_var,
            width=90,
        )
        self._risk_menu.grid(row=0, column=1, padx=(0, 8), pady=10)

        self._btn_apply_risk = ctk.CTkButton(
            self._risk_body, text="Apply", width=80,
            command=self._apply_risk,
        )
        self._btn_apply_risk.grid(row=0, column=2, padx=(0, 14), pady=10, sticky="w")

        self._lbl_risk_status = ctk.CTkLabel(
            self._risk_body, text=f"Current: {int(C.MAX_SYMBOL_FRACTION * 100)}% of funds per trade",
            text_color="#8b949e", font=ctk.CTkFont(size=12),
        )
        self._lbl_risk_status.grid(row=0, column=3, padx=14, pady=(10, 5), sticky="w")

        ctk.CTkLabel(
            self._risk_body, text="Testing Period:",
            font=ctk.CTkFont(size=13), text_color="#c9d1d9",
        ).grid(row=1, column=0, padx=(14, 8), pady=(0, 10), sticky="w")
        ctk.CTkLabel(
            self._risk_body, text="5 days  (fixed)",
            font=ctk.CTkFont(size=13), text_color="#8b949e",
        ).grid(row=1, column=1, padx=(0, 8), pady=(0, 10), sticky="w")

        ctk.CTkLabel(
            self._risk_body, text="Number of Tests:",
            font=ctk.CTkFont(size=13), text_color="#c9d1d9",
        ).grid(row=2, column=0, padx=(14, 8), pady=(0, 10), sticky="w")

        _tests_options = ["1000", "4000", "8000", "10000", "20000"]
        self._tests_var = ctk.StringVar(value=str(C.INIT_TRIALS))
        self._tests_menu = ctk.CTkOptionMenu(
            self._risk_body,
            values=_tests_options,
            variable=self._tests_var,
            width=90,
        )
        self._tests_menu.grid(row=2, column=1, padx=(0, 8), pady=(0, 10))

        self._btn_apply_tests = ctk.CTkButton(
            self._risk_body, text="Apply", width=80,
            command=self._apply_tests,
        )
        self._btn_apply_tests.grid(row=2, column=2, padx=(0, 14), pady=(0, 10), sticky="w")

        self._lbl_tests_status = ctk.CTkLabel(
            self._risk_body, text=f"Current: {C.INIT_TRIALS:,} tests",
            text_color="#8b949e", font=ctk.CTkFont(size=12),
        )
        self._lbl_tests_status.grid(row=2, column=3, padx=14, pady=(0, 10), sticky="w")

        # ── Intervals row ─────────────────────────────────────────────────────
        ctk.CTkLabel(
            self._risk_body, text="Intervals:",
            font=ctk.CTkFont(size=13), text_color="#c9d1d9",
        ).grid(row=3, column=0, padx=(14, 8), pady=(0, 12), sticky="w")
        ctk.CTkLabel(
            self._risk_body, text="5m  (fixed)",
            font=ctk.CTkFont(size=13), text_color="#8b949e",
        ).grid(row=3, column=1, padx=(0, 8), pady=(0, 12), sticky="w")

        # ── Take Profit row ───────────────────────────────────────────────────
        ctk.CTkLabel(
            self._risk_body, text="Take Profit:",
            font=ctk.CTkFont(size=13), text_color="#c9d1d9",
        ).grid(row=5, column=0, padx=(14, 8), pady=(0, 12), sticky="w")
        ctk.CTkLabel(
            self._risk_body, text="Optimised automatically",
            font=ctk.CTkFont(size=13), text_color="#8b949e",
        ).grid(row=5, column=1, columnspan=3, padx=(0, 14), pady=(0, 12), sticky="w")


        # ── Controls ──────────────────────────────────────────────────────────
        ctrl = ctk.CTkFrame(self._scroll, fg_color="#161b22", corner_radius=8)
        ctrl.grid(row=3, column=0, sticky="ew", padx=10, pady=8)
        ctrl.grid_columnconfigure(4, weight=1)

        self._btn_start = ctk.CTkButton(
            ctrl, text="▶   START", width=150, height=44,
            fg_color="#1a4731", hover_color="#266046",
            font=ctk.CTkFont(size=15, weight="bold"),
            command=self._start_bot,
        )
        self._btn_start.grid(row=0, column=0, padx=(14, 8), pady=12)

        self._btn_stop = ctk.CTkButton(
            ctrl, text="■   STOP", width=150, height=44,
            fg_color="#4e1f1f", hover_color="#6b2d2d",
            font=ctk.CTkFont(size=15, weight="bold"),
            state="disabled",
            command=self._stop_bot,
        )
        self._btn_stop.grid(row=0, column=1, padx=8, pady=12)

        self._btn_chart = ctk.CTkButton(
            ctrl, text="📊  Chart", width=110, height=44,
            fg_color="#1a2a4a", hover_color="#1e3a6a",
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._open_chart,
        )
        self._btn_chart.grid(row=0, column=3, padx=8, pady=12)

        self._lbl_ctrl_msg = ctk.CTkLabel(
            ctrl, text="Enter your API keys and press START",
            text_color="#8b949e", font=ctk.CTkFont(size=12),
        )
        self._lbl_ctrl_msg.grid(row=0, column=4, padx=14, sticky="w")

        # ── Progress bar (hidden until bot starts) ────────────────────────────
        self._prog_outer = ctk.CTkFrame(self._scroll, fg_color="#161b22", corner_radius=8)
        self._prog_outer.grid(row=4, column=0, sticky="ew", padx=10, pady=(0, 8))
        self._prog_outer.grid_columnconfigure(0, weight=1)
        self._prog_outer.grid_remove()

        self._prog_bar = ctk.CTkProgressBar(self._prog_outer, height=12)
        self._prog_bar.set(0)
        self._prog_bar.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 4))
        self._prog_lbl = ctk.CTkLabel(
            self._prog_outer, text="",
            text_color="#8b949e", font=ctk.CTkFont(size=12),
        )
        self._prog_lbl.grid(row=1, column=0, sticky="w", padx=14, pady=(0, 10))

        # ── Stat cards ────────────────────────────────────────────────────────
        cards = ctk.CTkFrame(self._scroll, fg_color="transparent")
        cards.grid(row=5, column=0, sticky="ew", padx=10, pady=(0, 8))
        for i in range(5):
            cards.grid_columnconfigure(i, weight=1)

        self._card_bal    = self._stat_card(cards, "Account Balance", "--",    0)
        self._card_pnl    = self._stat_card(cards, "P&L  R=Realized  A=Account", "--", 1)
        self._card_wr     = self._stat_card(cards, "Win Rate",         "--",    2)
        self._card_trades = self._stat_card(cards, "Total Trades",     "--",    3)
        self._card_lev    = self._stat_card(cards, "Leverage",         "--",    4)

        # ── Best strategy panel (hidden until first optimisation completes) ──────
        self._best_outer = ctk.CTkFrame(self._scroll, fg_color="#161b22", corner_radius=8)
        self._best_outer.grid(row=6, column=0, sticky="ew", padx=10, pady=(0, 8))
        self._best_outer.grid_columnconfigure(0, weight=1)
        self._best_outer.grid_remove()

        ctk.CTkLabel(
            self._best_outer, text="BEST STRATEGY",
            font=ctk.CTkFont(size=11, weight="bold"), text_color="#8b949e",
        ).grid(row=0, column=0, columnspan=4, sticky="w", padx=14, pady=(10, 2))

        self._lbl_best_entry = ctk.CTkLabel(self._best_outer, text="",
                                             font=ctk.CTkFont(size=13))
        self._lbl_best_entry.grid(row=1, column=0, sticky="w", padx=14, pady=1)

        self._lbl_best_exit = ctk.CTkLabel(self._best_outer, text="",
                                            font=ctk.CTkFont(size=13))
        self._lbl_best_exit.grid(row=2, column=0, sticky="w", padx=14, pady=1)

        self._lbl_best_exit_ind = ctk.CTkLabel(self._best_outer, text="",
                                                font=ctk.CTkFont(size=13))
        self._lbl_best_exit_ind.grid(row=3, column=0, sticky="w", padx=14, pady=1)

        self._lbl_best_stats = ctk.CTkLabel(self._best_outer, text="",
                                             font=ctk.CTkFont(size=13))
        self._lbl_best_stats.grid(row=4, column=0, sticky="w", padx=14, pady=(1, 4))

        self._lbl_reopt_countdown = ctk.CTkLabel(
            self._best_outer, text="",
            font=ctk.CTkFont(size=11), text_color="#8b949e",
        )
        self._lbl_reopt_countdown.grid(row=5, column=0, sticky="w", padx=14, pady=(0, 8))

        # ── Open position panel (hidden when flat) ────────────────────────────
        self._pos_outer = ctk.CTkFrame(self._scroll, fg_color="#161b22", corner_radius=8)
        self._pos_outer.grid(row=7, column=0, sticky="ew", padx=10, pady=(0, 8))
        self._pos_outer.grid_columnconfigure(1, weight=1)
        self._pos_outer.grid_remove()

        ctk.CTkLabel(
            self._pos_outer, text="OPEN POSITION",
            font=ctk.CTkFont(size=11, weight="bold"), text_color="#8b949e",
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=14, pady=(10, 2))

        self._lbl_pos_main  = ctk.CTkLabel(self._pos_outer, text="",
                                            font=ctk.CTkFont(size=14))
        self._lbl_pos_main.grid(row=1, column=0, sticky="w", padx=14, pady=2)
        self._lbl_pos_upnl  = ctk.CTkLabel(self._pos_outer, text="",
                                            font=ctk.CTkFont(size=13))
        self._lbl_pos_upnl.grid(row=2, column=0, sticky="w", padx=14, pady=(0, 10))

        # ── Last signal panel ──────────────────────────────────────────────────
        sig_outer = ctk.CTkFrame(self._scroll, fg_color="#161b22", corner_radius=8)
        sig_outer.grid(row=8, column=0, sticky="ew", padx=10, pady=(0, 8))
        sig_outer.grid_columnconfigure(4, weight=1)

        ctk.CTkLabel(
            sig_outer, text="LAST SIGNAL",
            font=ctk.CTkFont(size=11, weight="bold"), text_color="#8b949e",
        ).grid(row=0, column=0, columnspan=5, sticky="w", padx=14, pady=(8, 2))

        self._lbl_sig_type = ctk.CTkLabel(
            sig_outer, text="—  No signals yet",
            font=ctk.CTkFont(size=13), text_color="#8b949e",
        )
        self._lbl_sig_type.grid(row=1, column=0, sticky="w", padx=(14, 0), pady=(0, 10))

        self._lbl_sig_placed = ctk.CTkLabel(
            sig_outer, text="", font=ctk.CTkFont(size=13),
        )
        self._lbl_sig_placed.grid(row=1, column=1, sticky="w", padx=(28, 0), pady=(0, 10))

        self._lbl_sig_filled = ctk.CTkLabel(
            sig_outer, text="", font=ctk.CTkFont(size=13),
        )
        self._lbl_sig_filled.grid(row=1, column=2, sticky="w", padx=(28, 0), pady=(0, 10))

        self._lbl_sig_band = ctk.CTkLabel(
            sig_outer, text="", font=ctk.CTkFont(size=13), text_color="#d29922",
        )
        self._lbl_sig_band.grid(row=1, column=3, sticky="w", padx=(28, 14), pady=(0, 10))

        # ── Recent trades header ───────────────────────────────────────────────
        trades_hdr = ctk.CTkFrame(self._scroll, fg_color="#161b22", corner_radius=0, height=32)
        trades_hdr.grid(row=9, column=0, sticky="ew", padx=10, pady=(0, 0))
        ctk.CTkLabel(
            trades_hdr, text="RECENT TRADES",
            font=ctk.CTkFont(size=11, weight="bold"), text_color="#8b949e",
        ).pack(anchor="w", padx=14, pady=(8, 4))

        # ── Trades table ──────────────────────────────────────────────────────
        tree_frame = ctk.CTkFrame(self._scroll, fg_color="#161b22", corner_radius=8, height=175)
        tree_frame.grid(row=10, column=0, sticky="ew", padx=10, pady=(0, 8))
        tree_frame.grid_propagate(False)
        tree_frame.grid_columnconfigure(0, weight=1)
        tree_frame.grid_rowconfigure(0, weight=1)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Dark.Treeview",
            background="#0d1117", foreground="#c9d1d9",
            fieldbackground="#0d1117", rowheight=26, borderwidth=0,
        )
        style.configure(
            "Dark.Treeview.Heading",
            background="#161b22", foreground="#8b949e",
            borderwidth=0, relief="flat",
        )
        style.map("Dark.Treeview", background=[("selected", "#1f6feb")])

        cols = ("time", "symbol", "entry", "exit", "pnl", "result")
        self._tree = ttk.Treeview(tree_frame, columns=cols,
                                  show="headings", height=6,
                                  style="Dark.Treeview")
        for col, heading, w, anchor in [
            ("time",   "Date / Time",   148, "w"),
            ("symbol", "Symbol",         80, "center"),
            ("entry",  "Entry Price",    100, "center"),
            ("exit",   "Exit Price",     100, "center"),
            ("pnl",    "P&L",            90,  "center"),
            ("result", "Closed By",      90,  "center"),
        ]:
            self._tree.heading(col, text=heading)
            self._tree.column(col, width=w, anchor=anchor, stretch=True)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.grid(row=0, column=0, sticky="nsew", padx=(6, 0), pady=6)
        vsb.grid(row=0, column=1, sticky="ns", pady=6, padx=(0, 4))

        # ── Bottom tabview: Agent Analysis  +  Activity ───────────────────────
        _mono = ctk.CTkFont(family="Courier New" if sys.platform == "win32" else "Courier", size=11)

        self._bottom_tabs = ctk.CTkTabview(
            self._scroll,
            fg_color="#0d1117",
            segmented_button_fg_color="#161b22",
            segmented_button_selected_color="#1f6feb",
            segmented_button_selected_hover_color="#388bfd",
            segmented_button_unselected_color="#161b22",
            segmented_button_unselected_hover_color="#21262d",
            text_color="#c9d1d9",
            text_color_disabled="#6e7681",
            height=280,
        )
        self._bottom_tabs.grid(row=11, column=0, sticky="ew", padx=10, pady=(4, 8))

        # ── Agent tab ─────────────────────────────────────────────────────────
        self._bottom_tabs.add("🤖  Agent Analysis")
        _agent_tab = self._bottom_tabs.tab("🤖  Agent Analysis")
        _agent_tab.grid_columnconfigure(0, weight=1)
        _agent_tab.grid_rowconfigure(1, weight=1)

        self._lbl_agent_phase = ctk.CTkLabel(
            _agent_tab, text="",
            font=ctk.CTkFont(size=10), text_color="#58a6ff",
        )
        self._lbl_agent_phase.grid(row=0, column=0, sticky="w", padx=4, pady=(2, 0))

        self._agent_box = ctk.CTkTextbox(
            _agent_tab,
            font=_mono,
            fg_color="#0d1117", text_color="#79c0ff",
            corner_radius=6,
        )
        self._agent_box.grid(row=1, column=0, sticky="nsew", padx=0, pady=(2, 0))
        self._agent_box.configure(state="disabled")

        # ── Activity tab ──────────────────────────────────────────────────────
        self._bottom_tabs.add("Activity")
        _act_tab = self._bottom_tabs.tab("Activity")
        _act_tab.grid_columnconfigure(0, weight=1)
        _act_tab.grid_rowconfigure(0, weight=1)

        self._log_box = ctk.CTkTextbox(
            _act_tab,
            font=_mono,
            fg_color="#0d1117", text_color="#c9d1d9",
            corner_radius=6,
        )
        self._log_box.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        self._log_box.configure(state="disabled")

        # ── Equity tab ────────────────────────────────────────────────────────
        self._bottom_tabs.add("📈  Equity")
        _eq_tab = self._bottom_tabs.tab("📈  Equity")
        _eq_tab.grid_columnconfigure(0, weight=1)
        _eq_tab.grid_rowconfigure(0, weight=1)

        self._equity_box = ctk.CTkTextbox(
            _eq_tab,
            font=_mono,
            fg_color="#0d1117", text_color="#3fb950",
            corner_radius=6,
        )
        self._equity_box.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        self._equity_box.configure(state="disabled")

        # Start on Agent tab so analysis is immediately visible
        self._bottom_tabs.set("🤖  Agent Analysis")

    # ── Widget helpers ────────────────────────────────────────────────────────
    def _stat_card(self, parent: ctk.CTkFrame, label: str,
                   value: str, col: int) -> ctk.CTkLabel:
        f = ctk.CTkFrame(parent, fg_color="#161b22", corner_radius=8)
        f.grid(row=0, column=col, padx=4, pady=4, sticky="nsew")
        ctk.CTkLabel(
            f, text=label,
            font=ctk.CTkFont(size=11), text_color="#8b949e",
        ).pack(anchor="w", padx=14, pady=(12, 2))
        lbl = ctk.CTkLabel(f, text=value, font=ctk.CTkFont(size=24, weight="bold"))
        lbl.pack(anchor="w", padx=14, pady=(0, 12))
        return lbl

    @staticmethod
    def _toggle_show(entry: ctk.CTkEntry, btn: ctk.CTkButton) -> None:
        if entry.cget("show") == "*":
            entry.configure(show="")
            btn.configure(text="Hide")
        else:
            entry.configure(show="*")
            btn.configure(text="Show")

    def _toggle_api(self) -> None:
        if self._api_open:
            self._api_body.grid_remove()
            self._api_toggle_btn.configure(text="  API Credentials  ▼")
        else:
            self._api_body.grid()
            self._api_toggle_btn.configure(text="  API Credentials  ▲")
        self._api_open = not self._api_open

    def _toggle_risk(self) -> None:
        if self._risk_open:
            self._risk_body.grid_remove()
            self._risk_toggle_btn.configure(text="  Settings  ▼")
        else:
            self._risk_body.grid(row=1, column=0, sticky="ew", padx=0, pady=(0, 6))
            self._risk_toggle_btn.configure(text="  Settings  ▲")
        self._risk_open = not self._risk_open

    def _batch_log(self, lines: list) -> None:
        """Write one or more log lines using a SINGLE configure(state) pair.

        On macOS each configure(state=) call on a CTkTextbox fires accessibility
        notifications that can take 10-50 ms each.  Batching all lines in one
        normal→disabled toggle reduces 2×N calls to 2, eliminating the source
        of the spinning beach-ball during optimization.
        """
        if not lines:
            return
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self._log_box.configure(state="normal")
        for line in lines:
            self._log_box.insert("end", f"[{ts}]  {line}\n")
            self._log_lines += 1
        if self._log_lines > 400:          # trim: keep last ~300 lines
            to_remove = self._log_lines - 300
            self._log_box.delete("1.0", f"{to_remove + 1}.0")
            self._log_lines = 300
        self._log_box.see("end")
        self._log_box.configure(state="disabled")

    def _batch_agent(self, msgs: list) -> None:
        """Write one or more agent messages using a SINGLE configure(state) pair."""
        if not msgs:
            return
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self._agent_box.configure(state="normal")
        last_phase = ""
        for msg in msgs:
            self._agent_box.insert("end", f"[{ts}]  {msg[1]}\n")
            self._agent_lines += 1
            if len(msg) > 2 and msg[2]:
                last_phase = msg[2]
        if self._agent_lines > 400:        # trim: keep last ~300 lines
            to_remove = self._agent_lines - 300
            self._agent_box.delete("1.0", f"{to_remove + 1}.0")
            self._agent_lines = 300
        self._agent_box.see("end")
        self._agent_box.configure(state="disabled")
        if last_phase:
            self._lbl_agent_phase.configure(text=last_phase)
        # Auto-switch tabs based on the last message in the batch
        last_text = msgs[-1][1] if msgs else ""
        if last_text.startswith("🟢"):
            try:
                self._bottom_tabs.set("Activity")
            except Exception:
                pass
        elif last_phase and not last_phase.startswith("Trading"):
            try:
                self._bottom_tabs.set("🤖  Agent Analysis")
            except Exception:
                pass

    def _set_status(self, key: str) -> None:
        color, text = self._STATUS_MAP.get(key, ("#6e7681", f"● {key.upper()}"))
        self._lbl_status.configure(text=text, text_color=color)

    # ── API key management ────────────────────────────────────────────────────
    def _load_saved_keys(self) -> None:
        creds = _load_credentials()
        if creds:
            k, s = creds
            self._ent_key.insert(0, k)
            self._ent_secret.insert(0, s)
            self._lbl_key_status.configure(text="✓ Keys loaded", text_color="#3fb950")
            # Keys already saved — collapse the panel to save vertical space
            self._toggle_api()

    def _save_keys(self) -> None:
        k = self._ent_key.get().strip()
        s = self._ent_secret.get().strip()
        if not k or not s:
            messagebox.showwarning("Missing Credentials",
                                   "Please enter both your API Key and API Secret.")
            return
        if len(k) < 10 or len(s) < 10:
            messagebox.showwarning("Invalid Credentials",
                                   "The key or secret appears too short. Please double-check.")
            return
        _save_credentials(k, s)
        C.API_KEY    = k
        C.API_SECRET = s
        self._lbl_key_status.configure(text="✓ Keys saved", text_color="#3fb950")

    def _clear_keys(self) -> None:
        if not messagebox.askyesno("Clear API Keys",
                                   "This will delete your saved API credentials. Continue?"):
            return
        try:
            CREDS_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        self._ent_key.delete(0, "end")
        self._ent_secret.delete(0, "end")
        C.API_KEY    = ""
        C.API_SECRET = ""
        self._lbl_key_status.configure(text="Keys cleared", text_color="#f85149")

    # ── Risk profile ──────────────────────────────────────────────────────────
    def _apply_risk(self) -> None:
        raw = self._risk_var.get().replace("%", "").strip()
        try:
            pct = int(raw)
        except ValueError:
            return
        pct = max(10, min(95, pct))
        C.MAX_SYMBOL_FRACTION = pct / 100.0
        _save_config({"risk_pct": pct / 100.0})
        self._lbl_risk_status.configure(
            text=f"Current: {pct}% of funds per trade",
            text_color="#3fb950",
        )

    def _apply_tests(self) -> None:
        try:
            n = int(self._tests_var.get().strip())
        except ValueError:
            return
        n = max(1000, n)
        C.INIT_TRIALS  = n
        C.OPT_N_RANDOM = n
        _save_config({"optimizer": {"n_trials": n}})
        self._lbl_tests_status.configure(
            text=f"Current: {n:,} tests",
            text_color="#3fb950",
        )


    # ── Bot controls ──────────────────────────────────────────────────────────
    def _start_bot(self) -> None:
        k = self._ent_key.get().strip()
        s = self._ent_secret.get().strip()
        if not validate_api_credentials(k, s):
            messagebox.showerror(
                "API Keys Required",
                "Please enter and save your Bybit API credentials before starting.",
            )
            return
        C.API_KEY    = k
        C.API_SECRET = s

        # ── Schema compliance check ────────────────────────────────────────────
        # Silently resets the database if the on-disk schema is out of date
        # (e.g. after a code upgrade that added new columns).
        from engine.utils.db_logger import validate_or_reset_db
        was_reset = validate_or_reset_db(C.DB_PATH)
        if not was_reset:
            messagebox.showinfo(
                "Database Reset",
                "The trading database was out of date and has been reset.\n"
                "A fresh database has been created — previous session data is no longer available.",
            )

        self._btn_start.configure(state="disabled")
        self._btn_stop.configure(state="normal")
        self._lbl_ctrl_msg.configure(text="Bot is starting up…")
        self._risk_menu.configure(state="disabled")
        self._btn_apply_risk.configure(state="disabled")
        self._tests_menu.configure(state="disabled")
        self._btn_apply_tests.configure(state="disabled")
        self._prog_outer.grid()
        self._prog_bar.set(0)

        self._running = True
        self._ctrl = _BotController(self._q, self._stop_evt)
        self._ctrl.start()
        self._refresh_trades()

    def _stop_bot(self) -> None:
        self._btn_stop.configure(state="disabled")
        self._lbl_ctrl_msg.configure(text="Stopping…")
        self._set_status("stopping")
        if self._ctrl:
            self._ctrl.stop()

    # ── Chart server ──────────────────────────────────────────────────────────
    def _start_chart_server(self) -> None:
        """Start the chart server.  Browser opens after /api/ready returns true."""
        try:
            from web.server import start as _start_chart
            import threading as _threading, urllib.request as _ul, json as _json, time as _ti
            port = _start_chart()
            url       = f"http://127.0.0.1:{port}"
            ready_url = f"http://127.0.0.1:{port}/api/ready"

            def _open_when_ready():
                for _ in range(300):    # poll up to ~15 min
                    try:
                        with _ul.urlopen(ready_url, timeout=2) as _r:
                            if _json.loads(_r.read()).get("ready"):
                                break
                    except Exception:
                        pass
                    _ti.sleep(3)
                else:
                    return  # timed out
                self._open_chart_url(url)

            _threading.Thread(target=_open_when_ready, daemon=True,
                               name="chart-browser-opener").start()
        except Exception as exc:
            self._lbl_ctrl_msg.configure(text=f"Chart server error: {exc}")

    def _open_chart(self) -> None:
        """Open the chart in Firefox (called from the 'Open Chart' button)."""
        try:
            from web.server import start as _start_chart
            url = f"http://127.0.0.1:{_start_chart()}"
            self._open_chart_url(url)
        except Exception as exc:
            self._lbl_ctrl_msg.configure(text=f"Chart error: {exc}")

    def _open_chart_url(self, url: str) -> None:
        """Open the given URL in Firefox (or fallback browser)."""
        try:
            import subprocess, sys as _sys
            if _sys.platform == "darwin":
                subprocess.Popen(["open", "-a", "Firefox", url])
            else:
                webbrowser.get("firefox").open(url)
        except Exception:
            webbrowser.open(url)

    # ── Queue polling ─────────────────────────────────────────────────────────
    def _poll(self) -> None:
        """Drain up to 30 queue messages per cycle.

        Consecutive "log" and "agent" messages are batched together so the
        expensive CTkTextbox state-toggle (configure normal→disabled) fires at
        most twice per *group* instead of twice per *message*.  Message ordering
        is preserved; only adjacent same-type messages are coalesced.
        """
        try:
            # Collect up to 30 messages first (fast, no widget work)
            msgs: list = []
            try:
                for _ in range(30):
                    msgs.append(self._q.get_nowait())
            except queue.Empty:
                pass
            except Exception:
                pass

            # Process in arrival order, batching same-type consecutive groups
            i = 0
            while i < len(msgs):
                kind = msgs[i][0]
                if kind == "log":
                    batch: list = []
                    while i < len(msgs) and msgs[i][0] == "log":
                        batch.append(msgs[i][1])
                        i += 1
                    self._batch_log(batch)
                elif kind == "agent":
                    batch = []
                    while i < len(msgs) and msgs[i][0] == "agent":
                        batch.append(msgs[i])
                        i += 1
                    self._batch_agent(batch)
                else:
                    self._handle(msgs[i])
                    i += 1
        except Exception:
            pass
        finally:
            self.after(100, self._poll)

    def _handle(self, msg: tuple) -> None:
        kind = msg[0]

        if kind == "log":
            # Fallback: _poll batches these, but handle individually if needed
            self._batch_log([msg[1]])

        elif kind == "agent":
            # Fallback: _poll batches these, but handle individually if needed
            self._batch_agent([msg])

        elif kind == "status":
            self._set_status(msg[1])
            friendly = {
                "initializing": "Starting up…",
                "optimizing":   "Analyzing market conditions…",
                "trading":      "Live trading — monitoring market",
                "idle":         "Ready to start",
                "error":        "An error occurred",
            }
            self._lbl_ctrl_msg.configure(text=friendly.get(msg[1], msg[1]))

        elif kind == "progress":
            if len(msg) >= 3:
                _, val, label = msg[0], msg[1], msg[2]
                self._prog_bar.set(max(0.0, min(1.0, float(val))))
                if label:
                    self._prog_lbl.configure(text=label)

        elif kind == "stats":
            self._update_stats(msg[1])

        elif kind == "best_params":
            self._update_best_params(msg[1])

        elif kind == "leverage":
            self._card_lev.configure(text=msg[1])

        elif kind == "error":
            # Log the error inline — messagebox.showerror() is a BLOCKING modal
            # dialog that prevents the Tkinter event loop from running, causing
            # macOS to show the spinning beach-ball.  Inline display is safe.
            short = str(msg[1])
            if len(short) > 120:
                short = short[:117] + "…"
            self._batch_log([f"❌ Error: {short}"])
            self._lbl_ctrl_msg.configure(text=f"Error — see Activity log")

        elif kind == "done":
            self._running = False
            self._card_lev.configure(text="--")
            self._btn_start.configure(state="normal")
            self._btn_stop.configure(state="disabled")
            self._risk_menu.configure(state="normal")
            self._btn_apply_risk.configure(state="normal")
            self._tests_menu.configure(state="normal")
            self._btn_apply_tests.configure(state="normal")
            self._set_status("idle")
            self._lbl_ctrl_msg.configure(text="Ready to start")
            self._prog_outer.grid_remove()
            self._batch_log(["Bot stopped."])
            self._refresh_trades()

    # ── Best strategy display ─────────────────────────────────────────────────
    def _update_best_params(self, d: dict) -> None:
        sym      = d.get("symbol", "")
        interval = d.get("interval", "")
        lev      = d.get("leverage", C.DEFAULT_LEVERAGE)
        ep_str = (
            f"{sym}  ·  {interval}m  ·  {int(lev)}×  │  "
            f"Entry MA: {d['ma_len']}  BandMult: {d['band_mult']:.2f}%"
        )
        xp_str = (
            f"Exit  ·  TP: {d['tp_pct']:.2f}%  ·  SL: {d.get('sl_pct', 5.0):.2f}%  ·  Band Exit"
        )
        xi_str = (
            f"Exit Band  ·  MA: {d.get('exit_ma_len', '—')}  "
            f"BandMult: {d.get('exit_band_mult', 0):.2f}%"
        )
        sign = "+" if d["return_pct"] >= 0 else ""
        rc   = "#3fb950" if d["return_pct"] >= 0 else "#f85149"
        st_str = (
            f"Trades: {d['trades']}  ·  W/L: {d['n_wins']}/{d['n_losses']}  ·  "
            f"WR: {d.get('winrate', 0):.0f}%  ·  Return: {sign}{d['return_pct']:.2f}%  ·  "
            f"Sharpe: {d.get('sharpe', 0):.2f}  ·  Max DD: {d.get('max_dd', 0):.1f}%"
        )
        self._lbl_best_entry.configure(text=ep_str)
        self._lbl_best_exit.configure(text=xp_str)
        self._lbl_best_exit_ind.configure(text=xi_str)
        self._lbl_best_stats.configure(text=st_str, text_color=rc)
        self._best_outer.grid()

    # ── Live stats display ────────────────────────────────────────────────────
    def _update_stats(self, d: dict) -> None:
        bal          = d.get("balance",         0.0)
        realized_pnl = d.get("realized_pnl",   0.0)
        account_pnl  = d.get("account_pnl",    0.0)
        account_pct  = d.get("account_pnl_pct", 0.0)
        trades = d.get("trades",  0)
        wins   = d.get("wins",    0)
        losses = d.get("losses",  0)
        wr     = d.get("wr",      0.0)
        pos    = d.get("position")

        r_sign = "+" if realized_pnl >= 0 else ""
        a_sign = "+" if account_pnl  >= 0 else ""
        pcolor = "#3fb950" if realized_pnl >= 0 else "#f85149"

        self._card_bal.configure(text=f"${bal:,.2f}")
        self._card_pnl.configure(
            text=(
                f"R: {r_sign}${realized_pnl:.2f}\n"
                f"A: {a_sign}${account_pnl:.2f} ({a_sign}{account_pct:.2f}%)"
            ),
            text_color=pcolor,
        )
        self._card_wr.configure(text=f"{wr:.1f}%")
        self._card_trades.configure(
            text=f"{trades}",
        )

        if pos:
            self._pos_outer.grid()
            us = "+" if pos["upnl"] >= 0 else ""
            uc = "#3fb950" if pos["upnl"] >= 0 else "#f85149"
            self._lbl_pos_main.configure(
                text=(f"LONG  ·  Entry: ${pos['entry_price']:.5f}"
                      f"   ·   Take Profit: ${pos['tp_price']:.5f}")
            )
            self._lbl_pos_upnl.configure(
                text=(f"Mark Price: ${pos['mark_price']:.5f}"
                      f"   ·   Unrealised P&L: {us}${pos['upnl']:.4f}"
                      f"  ({us}{pos['upnl_pct']:.2f}%)"),
                text_color=uc,
            )
        else:
            self._pos_outer.grid_remove()

        sig = d.get("signal")
        if sig:
            stype   = sig.get("type", "")
            stime   = sig.get("time", "")
            placed  = sig.get("placed", False)
            filled  = sig.get("filled", False)
            sprice  = sig.get("price")
            sband   = sig.get("band", 0)

            type_color = "#3fb950" if stype == "ENTRY" else "#d29922"
            self._lbl_sig_type.configure(
                text=f"{stype}  ·  {stime}", text_color=type_color,
            )
            if sband:
                self._lbl_sig_band.configure(text=f"Band {sband}")
            else:
                self._lbl_sig_band.configure(text="")
            if placed:
                self._lbl_sig_placed.configure(
                    text="Order placed  ✓", text_color="#3fb950",
                )
                if filled and sprice is not None:
                    self._lbl_sig_filled.configure(
                        text=f"Filled  ✓  ${sprice:.5f}", text_color="#3fb950",
                    )
                else:
                    self._lbl_sig_filled.configure(
                        text="Filled  ✗", text_color="#f85149",
                    )
            else:
                self._lbl_sig_placed.configure(
                    text="Order not placed  ✗", text_color="#f85149",
                )
                self._lbl_sig_filled.configure(text="")

        # ── Re-opt countdown ──────────────────────────────────────────────────
        reopt_sec = d.get("next_reopt_sec", 0.0)
        if reopt_sec > 0:
            rh = int(reopt_sec / 3600)
            rm = int((reopt_sec % 3600) / 60)
            self._lbl_reopt_countdown.configure(
                text=f"Next re-optimisation in  {rh:02d}h {rm:02d}m"
            )

    # ── Trades table ──────────────────────────────────────────────────────────
    def _refresh_trades(self) -> None:
        """Fetch DB rows on a background thread — never block the Tkinter loop."""
        def _fetch() -> None:
            import sqlite3
            trades_rows: list = []
            equity_rows: list = []
            db_path = C.DB_PATH
            if os.path.exists(db_path):
                try:
                    conn = sqlite3.connect(db_path, timeout=2)
                    cur  = conn.cursor()
                    cur.execute("""
                        SELECT ts_utc, symbol, entry_price, exit_price, pnl_net, reason
                        FROM trades ORDER BY ts_utc DESC LIMIT 20
                    """)
                    trades_rows = cur.fetchall()
                    cur.execute("""
                        SELECT ts_utc, symbol, event, balance
                        FROM balance_snapshots ORDER BY ts_utc DESC LIMIT 40
                    """)
                    equity_rows = cur.fetchall()
                    conn.close()
                except Exception:
                    pass
            # Hand results back to the main thread — zero blocking
            self.after(0, lambda: self._apply_db_rows(trades_rows, equity_rows))

        threading.Thread(target=_fetch, daemon=True, name="gui-db-fetch").start()
        if self._running:
            self.after(10_000, self._refresh_trades)

    def _apply_db_rows(self, trades_rows: list, equity_rows: list) -> None:
        """Apply fetched DB data to widgets (called on main thread via after())."""
        self._apply_trades_rows(trades_rows)
        self._apply_equity_rows(equity_rows)

    def _apply_trades_rows(self, rows_db: list) -> None:
        for item in self._tree.get_children():
            self._tree.delete(item)

        for ts, sym, entry, exit_, pnl, reason in rows_db:
            pnl    = float(pnl or 0)
            sign   = "+" if pnl >= 0 else ""
            tag    = "win" if pnl >= 0 else "loss"
            r      = (reason or "").upper().replace("ADX_", "").replace("_EXIT", "").title()
            self._tree.insert("", "end",
                              values=(str(ts)[:16], sym or "",
                                      f"${float(entry or 0):.5f}",
                                      f"${float(exit_ or 0):.5f}",
                                      f"{sign}${pnl:.4f}",
                                      r),
                              tags=(tag,))

        self._tree.tag_configure("win",  foreground="#3fb950")
        self._tree.tag_configure("loss", foreground="#f85149")

    def _apply_equity_rows(self, rows_db: list) -> None:
        """Populate the Equity tab from pre-fetched balance_snapshots rows."""
        if not rows_db:
            return

        # Build a compact ASCII equity display (newest → oldest, top → bottom)
        rows_db = list(reversed(rows_db))   # chronological order
        balances = [float(r[3] or 0) for r in rows_db]
        start_bal = balances[0] if balances else 0.0
        peak_bal  = max(balances)
        curr_bal  = balances[-1]
        total_pnl = curr_bal - start_bal

        # Mini sparkline using block chars
        if len(balances) > 1:
            lo, hi = min(balances), max(balances)
            span   = (hi - lo) or 1.0
            blocks = " ▁▂▃▄▅▆▇█"
            spark  = "".join(blocks[max(0, min(8, int((b - lo) / span * 8)))] for b in balances[-32:])
        else:
            spark = "—"

        sign = "+" if total_pnl >= 0 else ""
        lines = [
            f"  Session P&L  {sign}${total_pnl:.4f}   Peak ${peak_bal:.4f}   Now ${curr_bal:.4f}",
            f"  {spark}",
            f"  {'─' * 60}",
            f"  {'Time (UTC)':<19}  {'Symbol':<10}  {'Event':<18}  {'Balance':>10}",
            f"  {'─' * 60}",
        ]
        for ts, sym, evt, bal in rows_db[-30:]:
            lines.append(f"  {str(ts)[:16]:<19}  {(sym or ''):<10}  {(evt or ''):<18}  ${float(bal or 0):>9.4f}")

        text = "\n".join(lines)
        self._equity_box.configure(state="normal")
        self._equity_box.delete("1.0", "end")
        self._equity_box.insert("end", text)
        self._equity_box.configure(state="disabled")


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
