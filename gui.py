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
from datetime import datetime
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
import POLE_POSITION as bot
from POLE_POSITION.utils import constants as C
from POLE_POSITION.utils.constants import LIVE_TP_SCALE
from POLE_POSITION.utils.api_key_prompt import (
    CREDS_FILE,
    _load_credentials,
    _save_credentials,
    validate_api_credentials,
)
from POLE_POSITION.utils.helpers import (
    leverage_for,
    maker_fee_for,
    supported_intervals,
    taker_fee_for,
)

# ── Config helpers (mirrors main.py Config) ───────────────────────────────────
def _load_config() -> Optional[dict]:
    base = sys._MEIPASS if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "POLE_POSITION", "config", "default_config.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _apply_config(cfg: dict) -> None:
    if not cfg:
        return
    if "symbol"          in cfg: C.SYMBOLS         = [cfg["symbol"]]
    if "leverage"        in cfg: C.DEFAULT_LEVERAGE = float(cfg["leverage"])
    if "starting_wallet" in cfg: C.STARTING_WALLET  = float(cfg["starting_wallet"])
    if "entry" in cfg:
        ec = cfg["entry"]
        if "ma_len"    in ec: C.DEFAULT_MA_LEN    = int(ec["ma_len"])
        if "band_mult" in ec: C.DEFAULT_BAND_MULT = float(ec["band_mult"])
    if "exit" in cfg:
        xc = cfg["exit"]
        if "tp_pct"       in xc: C.DEFAULT_TP_PCT       = float(xc["tp_pct"])
    if "optimizer" in cfg:
        if "n_trials" in cfg["optimizer"]:
            C.INIT_TRIALS = int(cfg["optimizer"]["n_trials"])


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
                upnl   = (p.entry_price - mark) * qty
                margin = p.entry_price * qty / float(t.leverage)
                pct    = (upnl / margin * 100) if margin else 0.0
                pos_info = {
                    "symbol":      sym,
                    "entry_price": p.entry_price,
                    "tp_price":    p.entry_price * (1.0 - t.exit_params.tp_pct * LIVE_TP_SCALE),
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
            from POLE_POSITION.utils.api_key_prompt import ensure_api_credentials
            ensure_api_credentials()

            symbols   = C.SYMBOLS
            intervals = supported_intervals(C.CANDLE_INTERVALS)
            client    = bot.BybitPrivateClient()
            gate      = bot.PositionGate()

            # Sync leverage
            self._log("Verifying account settings…")
            for sym in symbols:
                lev = client.get_leverage(sym)
                C.LEVERAGE_BY_SYMBOL[sym] = lev
                C.DEFAULT_LEVERAGE        = lev

            # ── Optimisation phase ────────────────────────────────────────────
            n_pairs  = len(symbols) * len(intervals)
            pair_idx = 0
            results: Dict = {}

            for sym in symbols:
                if self._stop.is_set():
                    break
                risk_df = bot.fetch_risk_tiers(sym)

                for iv in intervals:
                    if self._stop.is_set():
                        break

                    pair_idx += 1
                    self._log(f"Downloading market data for {sym}…")
                    df_last, df_mark = bot.download_seed_history(sym, C.DAYS_BACK_SEED, iv)
                    self._log(f"Market data ready ({len(df_last)} candles)")

                    self._emit("status", "optimizing")
                    self._log("Analyzing market conditions…")

                    # closure to capture loop vars
                    def _make_cb(pidx: int, n_pairs: int) -> Any:
                        def cb(done: int, total: int) -> None:
                            base = (pidx - 1) / n_pairs
                            frac = (done / total) / n_pairs
                            self._emit("progress", base + frac,
                                       f"Analyzing market conditions… {done * 100 // total}%")
                        return cb

                    opt = bot.optimise_bayesian(
                        df_last=df_last,
                        df_mark=df_mark,
                        risk_df=risk_df,
                        trials=C.INIT_TRIALS,
                        lookback_candles=min(len(df_last), len(df_mark)),
                        event_name=f"INIT_{sym}_{iv}m",
                        leverage=leverage_for(sym),
                        fee_rate=taker_fee_for(sym),
                        maker_fee_rate=maker_fee_for(sym),
                        interval_minutes=int(iv),
                        progress_callback=_make_cb(pair_idx, n_pairs),
                        verbose=False,
                    )

                    br = opt["best_result"]
                    pf = br.pnl_pct / (1.0 + max(br.max_drawdown_pct, 0.001))
                    self._log("Market analysis complete.")

                    results[(sym, iv)] = {
                        **opt,
                        "df_last": df_last,
                        "df_mark": df_mark,
                        "risk_df": risk_df,
                        "interval": iv,
                        "score": pf,
                    }

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
                self._emit("best_params", {
                    "ma_len":     _ep.ma_len,
                    "band_mult":  _ep.band_mult,
                    "tp_pct":     _xp.tp_pct * 100.0,
                    "n_wins":     _n_wins,
                    "n_losses":   _n_losses,
                    "trades":     _br.trades,
                    "return_pct": _br.pnl_pct,
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

            # ── Blocking WebSocket loop ───────────────────────────────────────
            bot.start_live_ws(traders, stop_event=self._stop)

        except Exception as exc:
            self._emit("error", str(exc))
            self._emit("status", "error")
        finally:
            if self._poller:
                self._poller.stop()
            self._emit("done",)


# ── Paper Bot Controller ──────────────────────────────────────────────────────
class _PaperBotController:
    """Owns the paper trading background thread.  No API keys required."""

    def __init__(self, q: queue.Queue, stop_evt: threading.Event) -> None:
        self._q       = q
        self._stop    = stop_evt
        self._poller: Optional[_StatsPoller] = None

    def start(self) -> None:
        self._stop.clear()
        threading.Thread(target=self._run, daemon=True, name="paper-bot-main").start()

    def stop(self) -> None:
        self._stop.set()
        if self._poller:
            self._poller.stop()

    def _emit(self, *msg: Any) -> None:
        self._q.put(msg)

    def _log(self, msg: str) -> None:
        self._emit("log", msg)

    def _run(self) -> None:
        try:
            self._emit("status", "initializing")
            self._log("Starting paper trading session…")

            cfg = _load_config()
            if cfg:
                _apply_config(cfg)

            symbols   = C.PAPER_SYMBOLS
            intervals = supported_intervals(C.CANDLE_INTERVALS)
            gate      = bot.PositionGate()

            # Apply the user-selected leverage to all paper symbols.
            # Always overwrite — do not skip if a prior live session set a different value.
            for sym in symbols:
                C.LEVERAGE_BY_SYMBOL[sym] = C.DEFAULT_LEVERAGE

            n_pairs  = len(symbols) * len(intervals)
            pair_idx = 0
            results: Dict = {}

            for sym in symbols:
                if self._stop.is_set():
                    break
                try:
                    risk_df    = bot.fetch_risk_tiers(sym)
                    instrument = bot.get_instrument_info(sym)
                except Exception as exc:
                    self._log(f"Skipping {sym}: {exc}")
                    continue

                for iv in intervals:
                    if self._stop.is_set():
                        break
                    try:
                        pair_idx += 1
                        self._log(f"Downloading market data for {sym}…")
                        df_last, df_mark = bot.download_seed_history(sym, C.DAYS_BACK_SEED, iv)
                        self._log(f"Market data ready ({len(df_last)} candles)")

                        self._emit("status", "optimizing")
                        self._log("Analyzing market conditions…")

                        def _make_cb(pidx: int, npairs: int) -> Any:
                            def cb(done: int, total: int) -> None:
                                base = (pidx - 1) / npairs
                                frac = (done / total) / npairs
                                self._emit("progress", base + frac,
                                           f"Analyzing market conditions… {done * 100 // total}%")
                            return cb

                        opt = bot.optimise_bayesian(
                            df_last=df_last,
                            df_mark=df_mark,
                            risk_df=risk_df,
                            trials=C.INIT_TRIALS,
                            lookback_candles=min(len(df_last), len(df_mark)),
                            event_name=f"PAPER_INIT_{sym}_{iv}m",
                            leverage=leverage_for(sym),
                            fee_rate=taker_fee_for(sym),
                            maker_fee_rate=maker_fee_for(sym),
                            interval_minutes=int(iv),
                            progress_callback=_make_cb(pair_idx, n_pairs),
                            verbose=False,
                        )

                        br = opt["best_result"]
                        pf = br.pnl_pct / (1.0 + max(br.max_drawdown_pct, 0.001))
                        self._log("Market analysis complete.")

                        results[(sym, iv)] = {
                            **opt,
                            "df_last":    df_last,
                            "df_mark":    df_mark,
                            "risk_df":    risk_df,
                            "instrument": instrument,
                            "interval":   iv,
                            "score":      pf,
                        }
                    except Exception as exc:
                        self._log(f"Skipping {sym} {iv}m: {exc}")

            if self._stop.is_set():
                self._emit("status", "idle")
                self._log("Stopped by user.")
                self._emit("done",)
                return

            if not results:
                self._emit("status", "error")
                self._log("No valid symbols found. Check network connection and symbol list.")
                self._emit("done",)
                return

            ranked   = sorted(results.items(), key=lambda x: x[1]["score"], reverse=True)
            selected = []
            seen: set = set()
            for (sym, iv), d in ranked:
                if sym not in seen:
                    selected.append((sym, d))
                    seen.add(sym)
                if len(selected) == C.MAX_ACTIVE_SYMBOLS:
                    break

            if selected:
                _sym, _d = selected[0]
                _ep = _d["entry_params"]
                _xp = _d["exit_params"]
                _br = _d["best_result"]
                _n_wins   = sum(1 for t in _br.trade_records if t.pnl_net > 0)
                _n_losses = sum(1 for t in _br.trade_records if t.pnl_net < 0)
                self._emit("best_params", {
                    "ma_len":       _ep.ma_len,
                    "band_mult":    _ep.band_mult,
                    "tp_pct":       _xp.tp_pct * 100.0,
                    "n_wins":       _n_wins,
                    "n_losses":     _n_losses,
                    "trades":       _br.trades,
                    "return_pct":   _br.pnl_pct,
                })

            traders: Dict[str, Any] = {}
            for sym, d in selected:
                self._log(f"Initializing paper trader for {sym}…")
                traders[sym] = bot.PaperTrader(
                    symbol=sym,
                    df_last_seed=d["df_last"],
                    df_mark_seed=d["df_mark"],
                    risk_df=d["risk_df"],
                    entry_params=d["entry_params"],
                    exit_params=d["exit_params"],
                    gate=gate,
                    interval=d["interval"],
                    instrument=d["instrument"],
                )

            self._poller = _StatsPoller(traders, self._q)
            self._poller.start()

            self._emit("status", "trading")
            self._emit("progress", 1.0, "")
            syms_str = ", ".join(traders.keys())
            self._log(f"Paper trading active — {syms_str}  (virtual $500 USDT wallet)")

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
        "paper":        ("#58a6ff", "● PAPER"),
        "stopping":     ("#d29922", "● STOPPING"),
        "error":        ("#f85149", "● ERROR"),
    }

    def __init__(self) -> None:
        super().__init__()
        self.title("Mean Reversion Trader")
        self.geometry("980x920")
        self.minsize(820, 720)
        self.resizable(True, True)

        self._q        = queue.Queue()
        self._stop_evt = threading.Event()
        self._ctrl: Optional[_BotController] = None
        self._running   = False
        self._mode      = "LIVE"   # "LIVE" or "PAPER"
        self._api_open  = True
        self._risk_open = False  # Settings start collapsed

        # Install filtered log handler
        handler = _GUILogHandler(self._q)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)

        self._build_ui()
        self._load_saved_keys()
        self._poll()

    # ── UI Construction ───────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        # Row weights: trades table (10) and log (12) expand
        self.grid_rowconfigure(10, weight=1)
        self.grid_rowconfigure(12, weight=2)

        # ── Header ────────────────────────────────────────────────────────────
        hdr = ctk.CTkFrame(self, height=60, fg_color="#0d1117", corner_radius=0)
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
        api_outer = ctk.CTkFrame(self, fg_color="#161b22", corner_radius=8)
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
        risk_outer = ctk.CTkFrame(self, fg_color="#161b22", corner_radius=8)
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

        _days_options = ["2 days", "3 days", "5 days", "7 days",
                         "15 days", "30 days", "60 days", "90 days"]
        self._days_var = ctk.StringVar(value=f"{C.DAYS_BACK_SEED} days")
        self._days_menu = ctk.CTkOptionMenu(
            self._risk_body,
            values=_days_options,
            variable=self._days_var,
            width=90,
        )
        self._days_menu.grid(row=1, column=1, padx=(0, 8), pady=(0, 10))

        self._btn_apply_days = ctk.CTkButton(
            self._risk_body, text="Apply", width=80,
            command=self._apply_days,
        )
        self._btn_apply_days.grid(row=1, column=2, padx=(0, 14), pady=(0, 10), sticky="w")

        self._lbl_days_status = ctk.CTkLabel(
            self._risk_body, text=f"Current: {C.DAYS_BACK_SEED} day(s) of history",
            text_color="#8b949e", font=ctk.CTkFont(size=12),
        )
        self._lbl_days_status.grid(row=1, column=3, padx=14, pady=(0, 10), sticky="w")

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

        iv_frame = ctk.CTkFrame(self._risk_body, fg_color="transparent")
        iv_frame.grid(row=3, column=1, columnspan=3, sticky="w", pady=(0, 12))

        self._iv_vars: dict = {}
        self._iv_checks: list = []
        for _lbl, _val in [("1m","1"),("3m","3"),("5m","5"),("15m","15"),("30m","30"),("60m","60")]:
            _var = ctk.BooleanVar(value=(_val in C.CANDLE_INTERVALS))
            self._iv_vars[_val] = _var
            _cb = ctk.CTkCheckBox(
                iv_frame, text=_lbl, variable=_var, width=65,
                font=ctk.CTkFont(size=13),
                command=self._apply_intervals,
            )
            _cb.pack(side="left", padx=(0, 4))
            self._iv_checks.append(_cb)

        # ── Symbols row ───────────────────────────────────────────────────────
        ctk.CTkLabel(
            self._risk_body, text="Symbols:",
            font=ctk.CTkFont(size=13), text_color="#c9d1d9",
        ).grid(row=4, column=0, padx=(14, 8), pady=(0, 12), sticky="w")

        self._symbols_entry = ctk.CTkEntry(
            self._risk_body, width=200,
            placeholder_text="e.g. BTCUSDT, ETHUSDT",
            font=ctk.CTkFont(size=13),
        )
        self._symbols_entry.insert(0, ", ".join(C.SYMBOLS))
        self._symbols_entry.grid(row=4, column=1, padx=(0, 8), pady=(0, 12), sticky="w")

        self._btn_apply_symbols = ctk.CTkButton(
            self._risk_body, text="Apply", width=80,
            command=self._apply_symbols,
        )
        self._btn_apply_symbols.grid(row=4, column=2, padx=(0, 14), pady=(0, 12), sticky="w")

        self._lbl_symbols_status = ctk.CTkLabel(
            self._risk_body,
            text=f"Current: {', '.join(C.SYMBOLS)}",
            text_color="#8b949e", font=ctk.CTkFont(size=12),
        )
        self._lbl_symbols_status.grid(row=4, column=3, padx=14, pady=(0, 12), sticky="w")

        # ── Leverage row (paper trading — live syncs from Bybit) ──────────────
        ctk.CTkLabel(
            self._risk_body, text="Leverage:",
            font=ctk.CTkFont(size=13), text_color="#c9d1d9",
        ).grid(row=5, column=0, padx=(14, 8), pady=(0, 12), sticky="w")

        _lev_options = ["1x", "2x", "3x", "5x", "10x", "15x", "20x", "25x", "50x", "75x", "100x"]
        _lev_default = f"{int(C.DEFAULT_LEVERAGE)}x"
        if _lev_default not in _lev_options:
            _lev_default = "10x"
        self._leverage_var = ctk.StringVar(value=_lev_default)
        self._leverage_menu = ctk.CTkOptionMenu(
            self._risk_body,
            values=_lev_options,
            variable=self._leverage_var,
            width=90,
        )
        self._leverage_menu.grid(row=5, column=1, padx=(0, 8), pady=(0, 12))

        self._btn_apply_leverage = ctk.CTkButton(
            self._risk_body, text="Apply", width=80,
            command=self._apply_leverage,
        )
        self._btn_apply_leverage.grid(row=5, column=2, padx=(0, 14), pady=(0, 12), sticky="w")

        self._lbl_leverage_status = ctk.CTkLabel(
            self._risk_body,
            text=f"Current: {int(C.DEFAULT_LEVERAGE)}x  (paper — live syncs from Bybit)",
            text_color="#8b949e", font=ctk.CTkFont(size=12),
        )
        self._lbl_leverage_status.grid(row=5, column=3, padx=14, pady=(0, 12), sticky="w")


        # ── Controls ──────────────────────────────────────────────────────────
        ctrl = ctk.CTkFrame(self, fg_color="#161b22", corner_radius=8)
        ctrl.grid(row=3, column=0, sticky="ew", padx=10, pady=8)
        ctrl.grid_columnconfigure(3, weight=1)

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

        self._mode_seg = ctk.CTkSegmentedButton(
            ctrl,
            values=["LIVE", "PAPER"],
            command=self._on_mode_change,
            width=160,
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self._mode_seg.set("LIVE")
        self._mode_seg.grid(row=0, column=2, padx=8, pady=12)

        self._lbl_ctrl_msg = ctk.CTkLabel(
            ctrl, text="Enter your API keys and press START",
            text_color="#8b949e", font=ctk.CTkFont(size=12),
        )
        self._lbl_ctrl_msg.grid(row=0, column=3, padx=14, sticky="w")

        # ── Progress bar (hidden until bot starts) ────────────────────────────
        self._prog_outer = ctk.CTkFrame(self, fg_color="#161b22", corner_radius=8)
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
        cards = ctk.CTkFrame(self, fg_color="transparent")
        cards.grid(row=5, column=0, sticky="ew", padx=10, pady=(0, 8))
        for i in range(4):
            cards.grid_columnconfigure(i, weight=1)

        self._card_bal    = self._stat_card(cards, "Account Balance", "--",    0)
        self._card_pnl    = self._stat_card(cards, "P&L  R=Realized  A=Account", "--", 1)
        self._card_wr     = self._stat_card(cards, "Win Rate",         "--",    2)
        self._card_trades = self._stat_card(cards, "Total Trades",     "--",    3)

        # ── Best strategy panel (hidden until first optimisation completes) ──────
        self._best_outer = ctk.CTkFrame(self, fg_color="#161b22", corner_radius=8)
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
        self._lbl_best_stats.grid(row=4, column=0, sticky="w", padx=14, pady=(1, 10))

        # ── Open position panel (hidden when flat) ────────────────────────────
        self._pos_outer = ctk.CTkFrame(self, fg_color="#161b22", corner_radius=8)
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
        sig_outer = ctk.CTkFrame(self, fg_color="#161b22", corner_radius=8)
        sig_outer.grid(row=8, column=0, sticky="ew", padx=10, pady=(0, 8))
        sig_outer.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(
            sig_outer, text="LAST SIGNAL",
            font=ctk.CTkFont(size=11, weight="bold"), text_color="#8b949e",
        ).grid(row=0, column=0, columnspan=4, sticky="w", padx=14, pady=(8, 2))

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

        # ── Recent trades header ───────────────────────────────────────────────
        trades_hdr = ctk.CTkFrame(self, fg_color="#161b22", corner_radius=0, height=32)
        trades_hdr.grid(row=9, column=0, sticky="ew", padx=10, pady=(0, 0))
        ctk.CTkLabel(
            trades_hdr, text="RECENT TRADES",
            font=ctk.CTkFont(size=11, weight="bold"), text_color="#8b949e",
        ).pack(anchor="w", padx=14, pady=(8, 4))

        # ── Trades table ──────────────────────────────────────────────────────
        tree_frame = ctk.CTkFrame(self, fg_color="#161b22", corner_radius=8)
        tree_frame.grid(row=10, column=0, sticky="nsew", padx=10, pady=(0, 8))
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

        # ── Activity log header ───────────────────────────────────────────────
        log_hdr = ctk.CTkFrame(self, fg_color="#161b22", corner_radius=0, height=32)
        log_hdr.grid(row=11, column=0, sticky="ew", padx=10, pady=(0, 0))
        ctk.CTkLabel(
            log_hdr, text="ACTIVITY",
            font=ctk.CTkFont(size=11, weight="bold"), text_color="#8b949e",
        ).pack(anchor="w", padx=14, pady=(8, 4))

        # ── Activity log ──────────────────────────────────────────────────────
        self._log_box = ctk.CTkTextbox(
            self, height=140,
            font=ctk.CTkFont(family="Courier New" if sys.platform == "win32" else "Courier", size=11),
            fg_color="#0d1117", text_color="#c9d1d9",
            corner_radius=8,
        )
        self._log_box.grid(row=12, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self._log_box.configure(state="disabled")

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

    def _append_log(self, msg: str) -> None:
        ts = datetime.utcnow().strftime("%H:%M:%S")
        self._log_box.configure(state="normal")
        self._log_box.insert("end", f"[{ts}]  {msg}\n")
        self._log_box.see("end")
        self._log_box.configure(state="disabled")

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
        self._lbl_risk_status.configure(
            text=f"Current: {pct}% of funds per trade",
            text_color="#3fb950",
        )

    def _apply_days(self) -> None:
        raw = self._days_var.get().replace("days", "").replace("day", "").strip()
        try:
            days = int(raw)
        except ValueError:
            return
        days = max(1, days)
        C.DAYS_BACK_SEED = days
        self._lbl_days_status.configure(
            text=f"Current: {days} day(s) of history",
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
        self._lbl_tests_status.configure(
            text=f"Current: {n:,} tests",
            text_color="#3fb950",
        )

    def _apply_intervals(self) -> None:
        selected = sorted(
            [v for v, var in self._iv_vars.items() if var.get()],
            key=lambda x: int(x),
        )
        if selected:
            C.CANDLE_INTERVALS = selected

    def _apply_symbols(self) -> None:
        raw = self._symbols_entry.get().strip()
        if not raw:
            return
        syms = [s.strip().upper() for s in raw.split(",") if s.strip()]
        if not syms:
            return
        C.SYMBOLS       = syms
        C.PAPER_SYMBOLS = syms
        self._lbl_symbols_status.configure(
            text=f"Current: {', '.join(syms)}",
            text_color="#3fb950",
        )

    def _apply_leverage(self) -> None:
        raw = self._leverage_var.get().replace("x", "").strip()
        try:
            lev = float(raw)
        except ValueError:
            return
        if lev <= 0:
            return
        C.DEFAULT_LEVERAGE = lev
        # Apply immediately to all configured paper symbols so the
        # liquidation price calculation uses the correct leverage from the start.
        for sym in C.PAPER_SYMBOLS:
            C.LEVERAGE_BY_SYMBOL[sym] = lev
        self._lbl_leverage_status.configure(
            text=f"Current: {int(lev)}x  (paper — live syncs from Bybit)",
            text_color="#3fb950",
        )

    # ── Mode toggle ───────────────────────────────────────────────────────────
    def _on_mode_change(self, mode: str) -> None:
        self._mode = mode
        if mode == "PAPER":
            self._lbl_ctrl_msg.configure(
                text="Paper mode — no API keys required — press START"
            )
        else:
            self._lbl_ctrl_msg.configure(text="Enter your API keys and press START")

    # ── Bot controls ──────────────────────────────────────────────────────────
    def _start_bot(self) -> None:
        if self._mode == "LIVE":
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

        self._btn_start.configure(state="disabled")
        self._btn_stop.configure(state="normal")
        self._mode_seg.configure(state="disabled")
        self._lbl_ctrl_msg.configure(text="Bot is starting up…")
        self._risk_menu.configure(state="disabled")
        self._btn_apply_risk.configure(state="disabled")
        self._days_menu.configure(state="disabled")
        self._btn_apply_days.configure(state="disabled")
        self._tests_menu.configure(state="disabled")
        self._btn_apply_tests.configure(state="disabled")
        for _cb in self._iv_checks:
            _cb.configure(state="disabled")
        self._symbols_entry.configure(state="disabled")
        self._btn_apply_symbols.configure(state="disabled")
        self._leverage_menu.configure(state="disabled")
        self._btn_apply_leverage.configure(state="disabled")
        self._prog_outer.grid()
        self._prog_bar.set(0)

        self._running = True
        if self._mode == "PAPER":
            self._ctrl = _PaperBotController(self._q, self._stop_evt)
        else:
            self._ctrl = _BotController(self._q, self._stop_evt)
        self._ctrl.start()
        self._refresh_trades()

    def _stop_bot(self) -> None:
        self._btn_stop.configure(state="disabled")
        self._lbl_ctrl_msg.configure(text="Stopping…")
        self._set_status("stopping")
        if self._ctrl:
            self._ctrl.stop()

    # ── Queue polling ─────────────────────────────────────────────────────────
    def _poll(self) -> None:
        try:
            while True:
                self._handle(self._q.get_nowait())
        except queue.Empty:
            pass
        except Exception:
            pass
        finally:
            self.after(100, self._poll)

    def _handle(self, msg: tuple) -> None:
        kind = msg[0]

        if kind == "log":
            self._append_log(msg[1])

        elif kind == "status":
            self._set_status(msg[1])
            _trading_msg = (
                "Paper trading — monitoring market"
                if self._mode == "PAPER"
                else "Live trading — monitoring market"
            )
            friendly = {
                "initializing": "Starting up…",
                "optimizing":   "Analyzing market conditions…",
                "trading":      _trading_msg,
                "paper":        "Paper trading — monitoring market",
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

        elif kind == "error":
            self._append_log(f"Error: {msg[1]}")
            messagebox.showerror("Bot Error", msg[1])

        elif kind == "done":
            self._running = False
            self._btn_start.configure(state="normal")
            self._btn_stop.configure(state="disabled")
            self._mode_seg.configure(state="normal")
            self._risk_menu.configure(state="normal")
            self._btn_apply_risk.configure(state="normal")
            self._days_menu.configure(state="normal")
            self._btn_apply_days.configure(state="normal")
            self._tests_menu.configure(state="normal")
            self._btn_apply_tests.configure(state="normal")
            for _cb in self._iv_checks:
                _cb.configure(state="normal")
            self._symbols_entry.configure(state="normal")
            self._btn_apply_symbols.configure(state="normal")
            self._leverage_menu.configure(state="normal")
            self._btn_apply_leverage.configure(state="normal")
            self._set_status("idle")
            self._lbl_ctrl_msg.configure(text="Ready to start")
            self._prog_outer.grid_remove()
            self._append_log("Bot stopped.")
            self._refresh_trades()

    # ── Best strategy display ─────────────────────────────────────────────────
    def _update_best_params(self, d: dict) -> None:
        ep_str = (
            f"Entry  ·  MA Length: {d['ma_len']}  "
            f"Band Mult: {d['band_mult']:.2f}%"
        )
        xp_str = (
            f"Exit   ·  TP: {d['tp_pct']:.2f}%  ·  Trail Stop (Jason McIntosh ATR)"
        )
        sign  = "+" if d["return_pct"] >= 0 else ""
        rc    = "#3fb950" if d["return_pct"] >= 0 else "#f85149"
        st_str = (
            f"Wins: {d['n_wins']}  ·  Losses: {d['n_losses']}  ·  "
            f"Trades: {d['trades']}  ·  Return: {sign}{d['return_pct']:.2f}%"
        )
        self._lbl_best_entry.configure(text=ep_str)
        self._lbl_best_exit.configure(text=xp_str)
        self._lbl_best_exit_ind.configure(text="")
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
                text=(f"SHORT  ·  Entry: ${pos['entry_price']:.5f}"
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

            type_color = "#3fb950" if stype == "ENTRY" else "#d29922"
            self._lbl_sig_type.configure(
                text=f"{stype}  ·  {stime}", text_color=type_color,
            )
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

    # ── Trades table ──────────────────────────────────────────────────────────
    def _refresh_trades(self) -> None:
        try:
            self._load_trades()
        except Exception:
            pass
        if self._running:
            self.after(10_000, self._refresh_trades)

    def _load_trades(self) -> None:
        path = C.TRADES_CSV_PATH
        if not os.path.exists(path):
            return

        rows = []
        try:
            with open(path, "r", encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    # Only show completed (closed) trades
                    action = row.get("action", "").upper()
                    if action != "EXIT":
                        continue
                    pnl    = float(row.get("pnl_net", 0) or 0)
                    reason = row.get("reason", "").upper()
                    sign   = "+" if pnl >= 0 else ""
                    rows.append({
                        "time":   row.get("ts_utc", "")[:16],
                        "symbol": row.get("symbol", "XRPUSDT"),
                        "entry":  f"${float(row.get('entry_price', 0) or 0):.5f}",
                        "exit":   f"${float(row.get('fill_price',  0) or 0):.5f}",
                        "pnl":    f"{sign}${pnl:.4f}",
                        "result": reason.replace("ADX_", "").replace("_EXIT", "").title(),
                        "tag":    "win" if pnl >= 0 else "loss",
                    })
        except Exception:
            return

        for item in self._tree.get_children():
            self._tree.delete(item)

        for row in reversed(rows[-20:]):
            self._tree.insert("", "end",
                              values=(row["time"], row["symbol"],
                                      row["entry"], row["exit"],
                                      row["pnl"],   row["result"]),
                              tags=(row["tag"],))

        self._tree.tag_configure("win",  foreground="#3fb950")
        self._tree.tag_configure("loss", foreground="#f85149")


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
