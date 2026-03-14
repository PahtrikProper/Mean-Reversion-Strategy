"""Paper Trader — simulated trading with no API keys required.

Uses public REST endpoints (klines, risk tiers, instrument info) and the
public Bybit WebSocket for live candle and mark-price data.

Simulates wallet, fills (close + slippage), TP, stop-loss,
band exit, and liquidation using exactly the same logic as LiveRealTrader.
No real orders are ever placed.
"""

import math
import threading
import time
import logging
import pandas as pd
from typing import Dict, Any, Optional

from ..utils.constants import (
    DAYS_BACK_SEED,
    KEEP_CANDLES,
    PAPER_STARTING_BALANCE,
    INIT_TRIALS,
    REOPT_INTERVAL_SEC,
    MAX_SYMBOL_FRACTION,
    MIN_WALLET_USDT,
    LIVE_TP_SCALE,
    TIME_TP_HOURS,
    TIME_TP_FALLBACK_PCT,
    TIME_TP_SCALE,
    COLOR_ENTRY,
    COLOR_EXIT,
    COLOR_ERROR,
    COLOR_RESET,
    COLOR_SUBMITTED,
    SIGNAL_DROUGHT_HOURS,
    MAX_LOSS_PCT,
    PAPER_SYMBOLS,
    CANDLE_INTERVALS,
)
from ..utils.data_structures import RealPosition, EntryParams, ExitParams, MC_MIN_TRADES, MC_SIMS
from ..utils.position_gate import PositionGate
from ..utils.logger import log_order
from ..utils import db_logger as _db
from ..utils.helpers import now_ms, interval_minutes, taker_fee_for, maker_fee_for, supported_intervals
from ..utils.trading_status import get_status_monitor
from ..core.indicators import (
    build_indicators,
    compute_entry_signals_raw,
    resolve_entry_signals,
    compute_exit_signals_raw,
)
from ..core.orders import apply_slippage
from ..trading.bybit_client import fetch_last_klines, fetch_mark_klines, fetch_risk_tiers, get_instrument_info
from ..trading.liquidation import pick_risk_tier, liquidation_price_short_isolated
from ..backtest.backtester import run_monte_carlo, mc_score
from ..utils.plotting import print_monte_carlo_report
from ..optimize.optimizer import optimise_params

log = logging.getLogger("paper_trader")


class PaperTrader:
    """Paper trader for Mean Reversion Strategy on one symbol.

    Duck-types to LiveRealTrader so it can be passed directly to
    start_live_ws().  No client / auth needed — all data is fetched
    via public endpoints and all fills are simulated locally.

    Wallet model:
      Entry : wallet -= margin + entry_fee   (margin locked)
      Exit  : wallet += margin + pnl_gross - exit_fee
      Liq   : margin is forfeited (wallet stays as post-entry value)
    """

    def __init__(
        self,
        symbol: str,
        df_last_seed: pd.DataFrame,
        df_mark_seed: pd.DataFrame,
        risk_df: pd.DataFrame,
        entry_params: EntryParams,
        exit_params: ExitParams,
        gate: PositionGate,
        interval: str,
        instrument: Dict[str, Any],
    ):
        self.symbol       = symbol
        self.gate         = gate
        self.risk_df      = risk_df
        self.interval     = interval
        self.leverage     = exit_params.leverage
        self.taker_fee    = taker_fee_for(symbol)
        self.entry_params = entry_params
        self.exit_params  = exit_params
        self.instrument   = instrument

        self.wallet         = float(PAPER_STARTING_BALANCE)
        self.initial_wallet = self.wallet

        # Position state maintained locally — no REST queries
        self.position: Optional[RealPosition]  = None
        self._paper_margin: float              = 0.0
        self._paper_tp_price: Optional[float]  = None
        self._paper_liq_price: Optional[float] = None
        self._paper_entry_fee: float           = 0.0

        # DataFrame: last-price OHLCV
        self.df = df_last_seed[["ts", "open", "high", "low", "close", "volume"]].copy().reset_index(drop=True)
        self.closed_candle_count = 0
        self.last_reopt_time     = time.time()
        self._reopt_running: bool = False

        self._entry_time: Optional[pd.Timestamp] = None
        self._entry_price: Optional[float]       = None
        self._time_tp_applied: bool              = False  # True once 12h TP tightening fires

        self.trade_count      = 0
        self.win_count        = 0
        self.realized_pnl_net = 0.0
        self.account_pnl_usdt = 0.0
        self.account_pnl_pct  = 0.0
        self.last_signal: Optional[dict] = None

        # ── Reliability & guard attributes ────────────────────────────────────
        self._last_signal_ts: float    = time.time()   # for drought detection
        self._halted: bool             = False          # True when max-loss fired
        self._halt_ts: Optional[float] = None           # timestamp when halt started
        self._last_drought_log: float  = 0.0            # cooldown for drought events
        self._shadow_positions: list   = []             # virtual "what if" trades for gate-blocked signals
        self._traders_ref: Optional[Dict] = None        # set by caller; used for symbol-switching at re-opt

        if len(df_mark_seed) == 0:
            raise RuntimeError("No mark seed data")
        self.mark_price = float(df_mark_seed["close"].iloc[-1])

        self._recompute_indicators()

    # ── Indicator rebuild ──────────────────────────────────────────────────────

    def _recompute_indicators(self):
        min_len = max(self.entry_params.ma_len, self.exit_params.exit_ma_len) + 20
        if len(self.df) < min_len:
            return
        base    = self.df[["ts", "open", "high", "low", "close", "volume"]].copy()
        self.df = build_indicators(
            base,
            ma_len=self.entry_params.ma_len,
            band_mult=self.entry_params.band_mult,
            exit_ma_len=self.exit_params.exit_ma_len,
            exit_band_mult=self.exit_params.exit_band_mult,
            band_ema_len=self.entry_params.band_ema_len,
            adx_period=self.entry_params.adx_period,
            rsi_period=self.entry_params.rsi_period,
        )

    # ── Instrument helpers (identical to LiveRealTrader) ───────────────────────

    def _format_qty(self, raw_qty: float) -> float:
        lot     = self.instrument.get("lotSizeFilter", {})
        min_qty = float(lot.get("minOrderQty", 0) or 0)
        max_qty = float(lot.get("maxOrderQty", 0) or 0)
        step    = float(lot.get("qtyStep", 0.000001) or 0.000001)
        if step <= 0:
            raise RuntimeError("Invalid qty step")
        qty = math.floor(raw_qty / step) * step
        qty = float(f"{qty:.12f}")
        if qty <= 0 or qty < min_qty:
            raise RuntimeError(f"qty {qty} below minOrderQty {min_qty}")
        if max_qty and qty > max_qty:
            raise RuntimeError(f"qty {qty} above maxOrderQty {max_qty}")
        return qty

    def _format_price(self, raw_price: float) -> float:
        pf       = self.instrument.get("priceFilter", {})
        tick_str = str(pf.get("tickSize", "0.0001") or "0.0001")
        try:
            tick = float(tick_str)
            if tick <= 0:
                raise ValueError("non-positive tick")
        except (ValueError, TypeError):
            tick = 0.0001
        price = math.floor(raw_price / tick) * tick
        if "e" in tick_str.lower():
            dp = abs(int(tick_str.lower().split("e")[1]))
        elif "." in tick_str:
            dp = len(tick_str.rstrip("0").split(".")[1]) if "." in tick_str.rstrip("0") else 0
        else:
            dp = 0
        return round(price, max(dp, 1))

    def _min_notional(self) -> float:
        lot = self.instrument.get("lotSizeFilter", {})
        for k in ("minOrderValue", "minNotionalValue", "minNotional"):
            v = lot.get(k)
            if v is not None:
                try:
                    return float(v)
                except Exception:
                    pass
        return 0.0

    # ── Mark price callback ────────────────────────────────────────────────────

    def on_mark_price_update(self, mark_price: float, ts_utc: str):
        self.mark_price = float(mark_price)
        _db.log_mark_price_tick(ts_utc=ts_utc, symbol=self.symbol, mark_price=self.mark_price)

    # ── Account PnL ────────────────────────────────────────────────────────────

    def _update_account_pnl(self):
        """Recalculate session PnL including unrealized PnL on open position."""
        if self.position is not None:
            qty_abs    = abs(self.position.qty)
            unrealized = (self.position.entry_price - self.mark_price) * qty_abs
            equity     = self.wallet + self._paper_margin + unrealized
        else:
            equity = self.wallet
        self.account_pnl_usdt = equity - float(self.initial_wallet)
        self.account_pnl_pct  = (
            (self.account_pnl_usdt / float(self.initial_wallet)) * 100.0
            if self.initial_wallet else 0.0
        )

    # ── Liquidation price refresh ──────────────────────────────────────────────

    def _refresh_liq_price(self):
        if self.position is None:
            self._paper_liq_price = None
            return
        qty_abs = abs(self.position.qty)
        pv_mark = qty_abs * self.mark_price
        tier    = pick_risk_tier(self.risk_df, pv_mark)
        self._paper_liq_price = liquidation_price_short_isolated(
            entry_price=self.position.entry_price,
            qty_short=-qty_abs,
            leverage=float(self.leverage),
            mark_price=self.mark_price,
            tier=tier,
            fee_rate=float(self.taker_fee),
        )

    # ── Entry execution ────────────────────────────────────────────────────────

    def _execute_entry(self, close_price: float, ts_utc: str, bar_ts: pd.Timestamp):
        if self.position is not None:
            _db.log_event(ts_utc=ts_utc, level="INFO", event_type="ENTRY_SKIPPED",
                symbol=self.symbol,
                message="Entry signal fired but already in position — no pyramiding",
                detail={"reason": "ALREADY_IN_POSITION", "close": close_price,
                        "entry_price": self.position.entry_price if self.position else None})
            return
        if self.wallet < MIN_WALLET_USDT:
            log.warning(f"[PAPER][{ts_utc}] Skipping entry — wallet {self.wallet:.4f} < {MIN_WALLET_USDT}")
            _db.log_event(ts_utc=ts_utc, level="WARNING", event_type="ENTRY_SKIPPED",
                symbol=self.symbol,
                message=f"Wallet {self.wallet:.4f} USDT below minimum {MIN_WALLET_USDT} USDT",
                detail={"reason": "INSUFFICIENT_WALLET", "wallet": self.wallet,
                        "min_wallet": MIN_WALLET_USDT, "close": close_price})
            return
        if not self.gate.try_acquire(self.symbol):
            log.warning(f"[PAPER][{ts_utc}] Skipping entry — gate blocked")
            _db.log_event(ts_utc=ts_utc, level="WARNING", event_type="ENTRY_SKIPPED",
                symbol=self.symbol,
                message="Position gate blocked — another symbol holds the slot",
                detail={"reason": "GATE_BLOCKED", "close": close_price,
                        "wallet": self.wallet})
            return

        wallet_before = self.wallet
        try:
            max_margin = self.wallet * float(MAX_SYMBOL_FRACTION)
            fill_price = apply_slippage(close_price, "sell")
            qty        = self._format_qty((max_margin * float(self.leverage)) / fill_price)
            notional   = qty * fill_price
            mn         = self._min_notional()
            if mn and notional < mn:
                raise RuntimeError(f"Notional {notional:.4f} < min {mn:.4f}")
            margin    = notional / float(self.leverage)
            entry_fee = notional * float(self.taker_fee)
            if self.wallet < margin + entry_fee:
                raise RuntimeError(f"Insufficient margin: wallet {self.wallet:.4f} < {margin + entry_fee:.4f}")

            # Deduct from wallet and lock margin
            self.wallet          -= margin + entry_fee
            self._paper_margin    = margin
            self._paper_entry_fee = entry_fee

            # Record position locally
            self.position   = RealPosition(qty=-qty, entry_price=fill_price, side="Sell")
            self._entry_time  = bar_ts
            self._entry_price = fill_price

            # TP price (same scaling as live)
            self._paper_tp_price = self._format_price(
                fill_price * (1.0 - float(self.exit_params.tp_pct) * LIVE_TP_SCALE)
            )

            # Initial liquidation price
            self._refresh_liq_price()

            log.info(
                f"{COLOR_SUBMITTED}[PAPER][{ts_utc}] SHORT ENTRY: "
                f"qty={qty:.6f}  fill={fill_price:.8f}  "
                f"TP={self._paper_tp_price}  margin={margin:.4f}  "
                f"wallet={self.wallet:.4f}{COLOR_RESET}"
            )
            log_order(
                ts_utc=ts_utc, symbol=f"[PAPER]{self.symbol}", side="SHORT",
                qty=qty, price=fill_price, order_type="ENTRY", status="FILLED",
                reason="BAND_ENTRY",
            )
            self._log_paper_trade(
                ts_utc=ts_utc, action="ENTRY", reason="BAND_ENTRY",
                side="SHORT", qty=qty, fill_price=fill_price,
                entry_price=fill_price,
                entry_fee=entry_fee, exit_fee=0.0,
                pnl_gross=0.0, pnl_net=-entry_fee,
                wallet_before=wallet_before, wallet_after=self.wallet,
            )

        except Exception as e:
            log.error(f"[PAPER][{ts_utc}] Entry failed: {e}")
            _db.log_event(ts_utc=ts_utc, level="ERROR", event_type="ENTRY_FAILED",
                symbol=self.symbol,
                message=f"Paper entry execution failed: {e}",
                detail={"reason": str(e), "close": close_price, "wallet": wallet_before,
                        "ma_len": self.entry_params.ma_len,
                        "band_mult": self.entry_params.band_mult,
                        "tp_pct": self.exit_params.tp_pct})
            self.gate.release(self.symbol)

    # ── Exit execution ──────────────────────────────────────────────────────────

    def _execute_exit(self, close_price: float, reason: str, ts_utc: str):
        if self.position is None:
            return

        entry_price    = self.position.entry_price
        qty_abs        = abs(self.position.qty)
        wallet_before  = self.wallet
        entry_fee_stored = self._paper_entry_fee
        try:
            fill_price = apply_slippage(close_price, "buy")
            pnl_gross  = (entry_price - fill_price) * qty_abs
            exit_fee   = qty_abs * fill_price * float(self.taker_fee)
            pnl_net    = pnl_gross - exit_fee - entry_fee_stored

            # Return margin and net PnL to wallet
            self.wallet += self._paper_margin + pnl_gross - exit_fee
            self.wallet  = max(0.0, self.wallet)
            wallet_after = self.wallet

            log_order(
                ts_utc=ts_utc, symbol=f"[PAPER]{self.symbol}", side="SHORT",
                qty=qty_abs, price=fill_price, order_type="EXIT", status="FILLED",
                reason=reason,
            )
            self._log_paper_trade(
                ts_utc=ts_utc, action="EXIT", reason=reason,
                side="COVER", qty=qty_abs, fill_price=fill_price,
                entry_price=entry_price,
                entry_fee=entry_fee_stored, exit_fee=exit_fee,
                pnl_gross=pnl_gross, pnl_net=pnl_net,
                wallet_before=wallet_before, wallet_after=wallet_after,
            )

        except Exception as e:
            log.error(f"[PAPER][{ts_utc}] Exit failed: {e}")
            _db.log_event(ts_utc=ts_utc, level="ERROR", event_type="EXIT_FAILED",
                symbol=self.symbol,
                message=f"Paper exit execution failed: {e}",
                detail={"reason": str(e), "exit_reason": reason,
                        "close": close_price,
                        "entry_price": entry_price,
                        "qty": qty_abs,
                        "wallet_before": wallet_before})
        finally:
            # Always clear position state and release gate
            self.position              = None
            self._paper_margin         = 0.0
            self._paper_tp_price       = None
            self._paper_liq_price = None
            self._paper_entry_fee = 0.0
            self._entry_time      = None
            self._entry_price     = None
            self._time_tp_applied = False
            self.gate.release(self.symbol)
            self._update_account_pnl()

    # ── Trade log ──────────────────────────────────────────────────────────────

    def _log_paper_trade(
        self,
        ts_utc: str,
        action: str,
        reason: str,
        side: str,
        qty: float,
        fill_price: float,
        entry_price: float,
        entry_fee: float,
        exit_fee: float,
        pnl_gross: float,
        pnl_net: float,
        wallet_before: float,
        wallet_after: float,
    ):
        tp_price = entry_price * (1.0 - float(self.exit_params.tp_pct) * LIVE_TP_SCALE)

        result = ""
        if action == "EXIT":
            self.trade_count      += 1
            win = pnl_net > 0
            self.win_count        += win
            self.realized_pnl_net += pnl_net
            result = "WIN" if win else "LOSS"

        fee_logged = exit_fee if side == "COVER" else entry_fee
        pnl_1x = float(pnl_net / float(self.leverage)) if self.leverage else 0.0
        pnl_pct_val = float(pnl_net / float(self.initial_wallet) * 100.0) if self.initial_wallet > 0 else 0.0

        _db.log_trade(
            ts_utc=ts_utc, mode="paper", symbol=self.symbol, interval=self.interval,
            action=action, reason=reason, side=side,
            qty=float(qty), fill_price=float(fill_price),
            notional=float(qty * fill_price), fee=float(fee_logged),
            entry_price=float(entry_price), tp_price=float(tp_price),
            mark_price=float(self.mark_price),
            wallet_before=float(wallet_before), wallet_after=float(wallet_after),
            pnl_gross=float(pnl_gross), pnl_net=float(pnl_net),
            pnl_1x_usdt=pnl_1x, pnl_pct=pnl_pct_val,
            result=result,
            ma_len=self.entry_params.ma_len,
            band_mult=float(self.entry_params.band_mult),
            tp_pct=float(self.exit_params.tp_pct),
        )
        _db.log_balance_snapshot(
            ts_utc=ts_utc, symbol=self.symbol, event=f"PAPER_{action}",
            wallet_usdt=float(wallet_after),
            session_pnl_usdt=float(wallet_after - float(self.initial_wallet)),
            session_pnl_pct=float((wallet_after - float(self.initial_wallet)) / float(self.initial_wallet) * 100.0) if self.initial_wallet > 0 else 0.0,
        )

        if action == "EXIT":
            pnl_sign     = "+" if pnl_net >= 0 else ""
            result_color = COLOR_ENTRY if result == "WIN" else COLOR_ERROR
            log.info(
                f"{result_color}[PAPER] {result}  {reason}  "
                f"pnl=${pnl_sign}{pnl_net:.4f}  fill={fill_price:.8f}  "
                f"qty={qty:.6f}  wallet=${wallet_after:.2f}{COLOR_RESET}"
            )

    # ── Candle-close display ───────────────────────────────────────────────────

    def _display_candle_close(
        self,
        ts_utc: str,
        o: float, h: float, l: float, c: float,
        entry_sig: bool,
        exit_sig: bool,
        adx: float,
        rsi: float,
    ):
        xp = self.exit_params
        wr = (self.win_count / self.trade_count * 100) if self.trade_count > 0 else 0.0

        if entry_sig:
            sig_str = "** SHORT ENTRY **"
            sig_col = COLOR_ENTRY
        elif exit_sig:
            sig_str = "** EXIT SIGNAL **"
            sig_col = COLOR_EXIT
        else:
            sig_str = "none"
            sig_col = COLOR_RESET

        pnl_sign  = "+" if self.account_pnl_usdt >= 0 else ""
        pnl_color = COLOR_ENTRY if self.account_pnl_usdt >= 0 else COLOR_ERROR
        log.info(
            f"[PAPER][{ts_utc}]  {self.symbol} #{self.closed_candle_count} {self.interval}m  "
            f"C={c:.5f}  |  "
            f"ADX={adx:.1f}(<{self.entry_params.adx_threshold:.0f})  "
            f"RSI={rsi:.1f}(>={self.entry_params.rsi_neutral_lo:.0f})  "
            f"TP={xp.tp_pct*100:.3f}%  |  "
            f"{sig_col}{sig_str}{COLOR_RESET}  |  "
            f"W={self.win_count} L={self.trade_count - self.win_count} WR={wr:.0f}%  "
            f"wallet=${self.wallet:.2f}  "
            f"{pnl_color}session={pnl_sign}${self.account_pnl_usdt:.2f} "
            f"({pnl_sign}{self.account_pnl_pct:.2f}%){COLOR_RESET}"
        )

        if self.position is not None:
            pos      = self.position
            qty_abs  = abs(pos.qty)
            upnl     = (pos.entry_price - self.mark_price) * qty_abs
            margin   = pos.entry_price * qty_abs / float(self.leverage)
            upnl_pct = (upnl / margin * 100) if margin > 0 else 0.0
            tp_disp  = self._paper_tp_price or pos.entry_price * (1.0 - float(xp.tp_pct) * LIVE_TP_SCALE)
            sign     = "+" if upnl >= 0 else ""
            days_held = ""
            if self._entry_time is not None:
                try:
                    dh = (pd.Timestamp.now(tz="UTC").tz_convert(None) - self._entry_time.tz_convert(None)).total_seconds() / 86400.0
                    days_held = f"  held={dh:.2f}d"
                except Exception:
                    pass
            liq_str = f"  liq=${self._paper_liq_price:.5f}" if self._paper_liq_price else ""
            log.info(
                f"  [PAPER] SHORT  entry=${pos.entry_price:.5f}  tp=${tp_disp:.5f}  "
                f"mark=${self.mark_price:.5f}  qty={qty_abs:.4f}  "
                f"uPnL=${sign}{upnl:.4f} ({sign}{upnl_pct:.2f}%){days_held}{liq_str}"
            )

    # ── Re-optimise ────────────────────────────────────────────────────────────

    def _maybe_reoptimise(self):
        """Every REOPT_INTERVAL_SEC (8h), spawn a background re-optimisation thread when flat.
        Returns immediately so the WebSocket callback thread is never blocked."""
        if time.time() - self.last_reopt_time < REOPT_INTERVAL_SEC:
            return
        if self.position is not None:
            return
        if self._reopt_running:
            return
        self._reopt_running = True
        threading.Thread(
            target=self._run_reoptimise,
            daemon=True,
            name=f"paper-reopt-{self.symbol}",
        ).start()

    def _run_reoptimise(self):
        """Background thread: re-run param search across ALL PAPER_SYMBOLS × CANDLE_INTERVALS.

        Selects the globally best (symbol, interval, params) by score = PnL% / (1 + DD%).
        Switches this trader to the winner if a different pair scores better and MC score > 0.
        """
        log.info("[PAPER][REOPT] starting multi-pair optimisation ...")
        try:
            best_score   = float("-inf")
            best_sym     = self.symbol
            best_iv      = self.interval
            best_entry   = None
            best_exit_p  = None
            best_br      = None
            best_df_last = None
            best_risk_df = None
            best_inst    = None

            for sym in PAPER_SYMBOLS:
                try:
                    risk_df = fetch_risk_tiers(sym)
                    inst    = get_instrument_info(sym)
                except Exception as e:
                    log.warning(f"[PAPER][REOPT] {sym}: fetch_risk_tiers failed: {e}")
                    continue

                for iv in supported_intervals(CANDLE_INTERVALS):
                    try:
                        df_last, df_mark = _download_seed(sym, DAYS_BACK_SEED, iv)

                        saved_best = None
                        if sym == self.symbol and iv == self.interval:
                            saved_best = {
                                "ma_len":          self.entry_params.ma_len,
                                "band_mult":       self.entry_params.band_mult,
                                "adx_threshold":   self.entry_params.adx_threshold,
                                "rsi_neutral_lo":  self.entry_params.rsi_neutral_lo,
                                "band_ema_len":    self.entry_params.band_ema_len,
                                "adx_period":      self.entry_params.adx_period,
                                "rsi_period":      self.entry_params.rsi_period,
                                "tp_pct":          self.exit_params.tp_pct,
                                "sl_pct":          self.exit_params.sl_pct,
                                "exit_ma_len":     self.exit_params.exit_ma_len,
                                "exit_band_mult":  self.exit_params.exit_band_mult,
                                "leverage":        self.exit_params.leverage,
                            }

                        opt = optimise_params(
                            df_last=df_last, df_mark=df_mark,
                            risk_df=risk_df,
                            trials=INIT_TRIALS,
                            lookback_candles=min(len(df_last), len(df_mark)),
                            event_name=f"PAPER_REOPT_{sym}_{iv}m",
                            fee_rate=taker_fee_for(sym),
                            maker_fee_rate=maker_fee_for(sym),
                            interval_minutes=interval_minutes(iv),
                            saved_best=saved_best,
                            db_symbol=sym, db_interval=iv, db_trigger="REOPT",
                        )

                        br = opt["best_result"]
                        pf = br.pnl_pct / (1.0 + max(br.max_drawdown_pct, 0.001))
                        log.info(
                            f"[PAPER][REOPT] {sym} {iv}m  score={pf:.4f}  "
                            f"PnL={br.pnl_pct:.2f}%  DD={br.max_drawdown_pct:.1f}%"
                        )

                        if pf > best_score:
                            best_score   = pf
                            best_sym     = sym
                            best_iv      = iv
                            best_entry   = opt["entry_params"]
                            best_exit_p  = opt["exit_params"]
                            best_br      = br
                            best_df_last = df_last
                            best_risk_df = risk_df
                            best_inst    = inst

                    except Exception as e:
                        log.warning(f"[PAPER][REOPT] {sym} {iv}m: skipped: {e}")

            if best_entry is None:
                log.warning("[PAPER][REOPT] No valid pairs found — keeping old params")
                return

            # ── Monte Carlo on the globally best result ────────────────────────
            records      = getattr(best_br, "trade_records", []) or []
            new_mc_score = float("-inf")
            mc_res       = None
            if len(records) >= MC_MIN_TRADES:
                mc_res = run_monte_carlo(records, float(PAPER_STARTING_BALANCE), n_sims=MC_SIMS)
                if mc_res:
                    new_mc_score = mc_score(mc_res)
                    print_monte_carlo_report(
                        mc_res, float(PAPER_STARTING_BALANCE),
                        len(records), best_sym, best_iv,
                    )

            log.info(
                f"[PAPER][REOPT] winner={best_sym} {best_iv}m  MC={new_mc_score:.4f}  "
                f"(current={self.symbol} {self.interval}m)"
            )

            _ts_reopt = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M:%S")

            # DB: MC run
            if mc_res:
                _db.log_monte_carlo(
                    ts_utc=_ts_reopt, symbol=best_sym, interval=best_iv,
                    mc_results=mc_res,
                    score=new_mc_score if new_mc_score != float("-inf") else None,
                )

            # DB: params event
            _event = "REOPT_ACCEPTED" if new_mc_score > 0 else "REOPT_REJECTED"
            _db.log_params(
                ts_utc=_ts_reopt, symbol=best_sym, interval=best_iv,
                event=_event,
                ma_len=best_entry.ma_len, band_mult=best_entry.band_mult,
                adx_threshold=best_entry.adx_threshold,
                rsi_neutral_lo=best_entry.rsi_neutral_lo,
                band_ema_len=best_entry.band_ema_len,
                adx_period=best_entry.adx_period,
                rsi_period=best_entry.rsi_period,
                tp_pct=best_exit_p.tp_pct,
                sl_pct=best_exit_p.sl_pct,
                exit_ma_len=best_exit_p.exit_ma_len,
                exit_band_mult=best_exit_p.exit_band_mult,
                leverage=best_exit_p.leverage,
                mc_score=new_mc_score if new_mc_score != float("-inf") else None,
                sharpe=best_br.sharpe_ratio, pnl_pct=best_br.pnl_pct,
                max_drawdown_pct=best_br.max_drawdown_pct,
                trade_count=best_br.trades, winrate=best_br.winrate,
                wallet=self.wallet,
            )

            if new_mc_score > 0:
                _switched = (best_sym != self.symbol or best_iv != self.interval)

                if _switched:
                    log.info(
                        f"[PAPER][REOPT] ★ switching {self.symbol} {self.interval}m "
                        f"→ {best_sym} {best_iv}m  (score {best_score:.4f})"
                    )
                    if self._traders_ref is not None:
                        self._traders_ref.pop(self.symbol, None)
                        self._traders_ref[best_sym] = self

                    self.symbol     = best_sym
                    self.interval   = best_iv
                    self.risk_df    = best_risk_df
                    self.instrument = best_inst
                    self.taker_fee  = taker_fee_for(best_sym)
                    self.df = best_df_last[
                        ["ts", "open", "high", "low", "close", "volume"]
                    ].copy().reset_index(drop=True)
                    self.closed_candle_count = 0
                    self._last_signal_ts     = time.time()
                    self._shadow_positions   = []

                self.entry_params = best_entry
                self.exit_params  = best_exit_p
                self.leverage     = best_exit_p.leverage
                self._recompute_indicators()
                log.info(
                    f"[PAPER][REOPT] {'switched + ' if _switched else ''}params updated "
                    f"(MC={new_mc_score:.4f})"
                )
            else:
                log.info(
                    f"[PAPER][REOPT] MC score {new_mc_score:.4f} <= 0 — keeping old params"
                )

        except Exception as e:
            log.error(f"[PAPER][REOPT] failed: {e}", exc_info=True)
        finally:
            self.last_reopt_time = time.time()
            self._reopt_running  = False

    # ── Main candle callback ───────────────────────────────────────────────────

    def on_closed_candle(self, candle: Dict[str, Any]):
        """Called once per confirmed closed candle from the WebSocket.

        Exit priority (identical to backtester and LiveRealTrader):
          1. Liquidation   — mark_price >= liq_price
          2. Take-Profit   — candle low <= tp_price
          3. Stop-Loss     — high >= entry * (1 + sl_pct)
          4. Band Exit     — low drops below discount band
        """
        try:
            self._on_closed_candle_inner(candle)
        except Exception as _occ_err:
            _ts_err = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M:%S")
            log.error(f"[PAPER][{self.symbol}] on_closed_candle unhandled exception: {_occ_err}",
                      exc_info=True)
            _db.log_event(
                ts_utc=_ts_err, level="ERROR", event_type="CANDLE_CALLBACK_ERROR",
                symbol=self.symbol,
                message=f"paper on_closed_candle crashed: {_occ_err}",
                detail={"error": str(_occ_err)},
            )

    def _on_closed_candle_inner(self, candle: Dict[str, Any]):
        """Inner implementation — wrapped by on_closed_candle for safety."""
        ts     = pd.to_datetime(int(candle["start"]), unit="ms", utc=True)
        ts_utc = ts.strftime("%Y-%m-%d %H:%M:%S")
        get_status_monitor().on_candle_received(self.symbol)

        o   = float(candle["open"])
        h   = float(candle["high"])
        l   = float(candle["low"])
        c   = float(candle["close"])
        vol = float(candle.get("volume", 0))

        ts_ms = int(candle["start"])
        new_row = pd.DataFrame([{"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": vol}])
        self.df = pd.concat([self.df, new_row], ignore_index=True)
        if len(self.df) > KEEP_CANDLES:
            self.df = self.df.iloc[-KEEP_CANDLES:].reset_index(drop=True)

        self._recompute_indicators()
        self.closed_candle_count += 1
        self._maybe_reoptimise()

        # ── DB: raw candle ──
        _db.log_candle(
            ts_utc=ts_utc, ts_ms=ts_ms, symbol=self.symbol,
            interval=self.interval, price_type="last",
            o=o, h=h, l=l, c=c, vol=vol,
        )

        # Refresh liquidation price with latest mark price
        if self.position is not None:
            self._refresh_liq_price()

        self._update_account_pnl()
        acted = False

        # ── Warm-up guard ──
        min_len = max(self.entry_params.ma_len, self.exit_params.exit_ma_len) + 20
        if len(self.df) < min_len or "adx" not in self.df.columns:
            log.info(
                f"[PAPER][{ts_utc}] {self.symbol} Candle #{self.closed_candle_count} "
                f"(warm-up {len(self.df)}/{min_len})  "
                f"O={o:.4f} H={h:.4f} L={l:.4f} C={c:.4f}"
            )
            return

        row  = self.df.iloc[-1]
        prev = self.df.iloc[-2]

        adx_val = float(row["adx"])
        rsi_val = float(row["rsi"]) if not pd.isna(row["rsi"]) else 100.0

        # ── Max-loss halt: auto-expire after 4 h ─────────────────────────────
        if self._halted:
            if time.time() - (self._halt_ts or 0) >= 4 * 3600:
                self._halted  = False
                self._halt_ts = None
                log.info(f"[PAPER][{self.symbol}] Max-loss halt expired — resuming entries")
                _db.log_event(ts_utc=ts_utc, level="INFO", event_type="MAX_LOSS_HALT_EXPIRED",
                    symbol=self.symbol, message="4-hour max-loss halt expired — entries re-enabled",
                    detail={})

        _max_loss_pct = MAX_LOSS_PCT
        if _max_loss_pct is not None and not self._halted:
            if self.account_pnl_pct <= -abs(_max_loss_pct):
                self._halted  = True
                self._halt_ts = time.time()
                log.warning(
                    f"[PAPER][{self.symbol}] MAX-LOSS HALT: "
                    f"session PnL={self.account_pnl_pct:.2f}% <= -{abs(_max_loss_pct):.1f}% "
                    f"— halting entries for 4 hours"
                )
                _db.log_event(ts_utc=ts_utc, level="WARNING", event_type="MAX_LOSS_HALT",
                    symbol=self.symbol,
                    message=(f"Max-loss halt: PnL={self.account_pnl_pct:.2f}% <= "
                             f"-{abs(_max_loss_pct):.1f}%"),
                    detail={"session_pnl_pct": self.account_pnl_pct,
                            "max_loss_pct": _max_loss_pct})
                if self.position is not None:
                    self._execute_exit(c, "MAX_LOSS", ts_utc)

        _raw_short = compute_entry_signals_raw(
            current_row=row, prev_row=prev,
            current_high=h,
        )

        # ── Signal drought tracking ───────────────────────────────────────────
        if _raw_short > 0:
            self._last_signal_ts = time.time()
        elif SIGNAL_DROUGHT_HOURS and SIGNAL_DROUGHT_HOURS > 0:
            _drought_sec = time.time() - self._last_signal_ts
            if (_drought_sec >= SIGNAL_DROUGHT_HOURS * 3600
                    and time.time() - self._last_drought_log >= SIGNAL_DROUGHT_HOURS * 3600):
                self._last_drought_log = time.time()
                _db.log_event(
                    ts_utc=ts_utc, level="WARNING", event_type="SIGNAL_DROUGHT",
                    symbol=self.symbol,
                    message=(f"No raw entry signal for {_drought_sec/3600:.1f}h "
                             f"(threshold {SIGNAL_DROUGHT_HOURS:.0f}h)"),
                    detail={"drought_hours": round(_drought_sec / 3600, 2),
                            "threshold_hours": SIGNAL_DROUGHT_HOURS},
                )

        _final_short = resolve_entry_signals(
            _raw_short, adx_val, rsi_val,
            adx_threshold=self.entry_params.adx_threshold,
            rsi_neutral_lo=self.entry_params.rsi_neutral_lo,
        )
        entry_sig = _final_short > 0
        _raw_exit = compute_exit_signals_raw(
            current_row=row, prev_row=prev,
            current_low=l,
        )
        exit_sig = _raw_exit > 0

        # ── Shadow position tracking — check virtual "what if" trades ─────────
        if self._shadow_positions:
            _sh_to_remove = []
            for _sh in self._shadow_positions:
                _sh["candles"] += 1
                _sh_outcome = None
                _sh_out_px  = None
                if l <= _sh["tp_price"]:
                    _sh_outcome = "TP_HIT"; _sh_out_px = _sh["tp_price"]
                elif h >= _sh["sl_price"]:
                    _sh_outcome = "SL_HIT"; _sh_out_px = _sh["sl_price"]
                elif _sh["candles"] >= 100:
                    _sh_outcome = "EXPIRED"
                if _sh_outcome:
                    if _sh_outcome != "EXPIRED":
                        _sh_pnl = ((_sh["entry_price"] - _sh_out_px)
                                   / _sh["entry_price"] * float(self.leverage) * 100.0)
                        _db.log_missed_trade(
                            entry_ts=_sh["entry_ts"], resolved_ts=ts_utc,
                            symbol=self.symbol, interval=self.interval,
                            blocked_by=_sh["blocked_by"],
                            entry_price=_sh["entry_price"], tp_price=_sh["tp_price"],
                            sl_price_at_resolution=_sh["sl_price"],
                            band=_sh["band"],
                            adx_at_entry=_sh.get("adx_at_entry"),
                            rsi_at_entry=_sh.get("rsi_at_entry"),
                            outcome=_sh_outcome,
                            outcome_pnl_pct=round(_sh_pnl, 4),
                            candles_elapsed=_sh["candles"],
                        )
                        if _sh_outcome == "TP_HIT":
                            log.info(
                                f"[PAPER][{self.symbol}] 🔍 Skipped trade would have been profitable  "
                                f"blocked_by={_sh['blocked_by']}  pnl≈{_sh_pnl:+.2f}%  "
                                f"band={_sh['band']}  ({_sh['candles']} candles after signal)"
                            )
                    _sh_to_remove.append(_sh)
            for _sh in _sh_to_remove:
                self._shadow_positions.remove(_sh)

        # ── DB: candle analytics ──
        _db.log_candle_analytics(
            ts_utc=ts_utc, symbol=self.symbol, interval=self.interval,
            df=self.df, mark_price=self.mark_price,
            ma_len=self.entry_params.ma_len, band_mult=self.entry_params.band_mult,
            exit_ma_len=self.exit_params.exit_ma_len,
            exit_band_mult=float(self.exit_params.exit_band_mult),
            sl_pct=float(self.exit_params.sl_pct),
        )

        # ── DB: signal ──
        _sl_price_lvl = (
            self._entry_price * (1.0 + float(self.exit_params.sl_pct))
            if self.position is not None and self._entry_price is not None else None
        )

        if entry_sig:
            _sig_type  = "ENTRY"
            _blocked   = None
        elif _raw_exit > 0:
            _sig_type  = "EXIT_BAND"
            _blocked   = None
        elif _sl_price_lvl is not None and h >= _sl_price_lvl:
            _sig_type  = "EXIT_SL"
            _blocked   = None
        else:
            _sig_type  = "NONE"
            _blocked_entry = None
            if _raw_short > 0 and _final_short == 0:
                _blocked_entry = "ADX" if adx_val >= 25 else "RSI"
                # ── Shadow for indicator-blocked signal ───────────────────────
                if self.position is None and len(self._shadow_positions) < 5:
                    _sh_ep  = c
                    _sh_tp  = _sh_ep * (1.0 - float(self.exit_params.tp_pct) * LIVE_TP_SCALE)
                    _sh_sl  = _sh_ep * (1.0 + float(self.exit_params.sl_pct))
                    self._shadow_positions.append({
                        "entry_ts": ts_utc, "entry_price": _sh_ep,
                        "tp_price": _sh_tp, "sl_price": _sh_sl,
                        "band": _raw_short, "blocked_by": _blocked_entry,
                        "candles": 0, "adx_at_entry": adx_val,
                        "rsi_at_entry": rsi_val,
                    })
            _blocked = _blocked_entry

        _db.log_signal(
            ts_utc=ts_utc, symbol=self.symbol, interval=self.interval,
            signal_type=_sig_type,
            raw_band_level=_raw_short, final_band_level=_final_short,
            adx=adx_val, rsi=rsi_val,
            sl_price_level=_sl_price_lvl,
            blocked_by=_blocked,
            o=o, h=h, l=l, c=c,
            ma_len=self.entry_params.ma_len,
            band_mult=self.entry_params.band_mult,
            tp_pct=self.exit_params.tp_pct,
        )

        # ── DB: position snapshot ──
        if self.position is not None:
            _qty_abs  = abs(self.position.qty)
            _upnl     = (self.position.entry_price - self.mark_price) * _qty_abs
            _tp_snap  = self._paper_tp_price
            _ts_entry = self._entry_time.strftime("%Y-%m-%d %H:%M:%S") if self._entry_time else None
            _db.log_position(
                ts_utc=ts_utc, symbol=self.symbol,
                qty=-_qty_abs,
                entry_price=self.position.entry_price,
                entry_time=_ts_entry,
                mark_price=self.mark_price,
                liquidation_price=self._paper_liq_price,
                unrealized_pnl=_upnl,
                min_low_since_entry=None,
                sl_price=_sl_price_lvl,
                tp_price=_tp_snap,
                wallet=self.wallet,
            )
        else:
            _db.log_position(
                ts_utc=ts_utc, symbol=self.symbol,
                qty=None, entry_price=None, entry_time=None,
                mark_price=self.mark_price,
                liquidation_price=None, unrealized_pnl=None,
                min_low_since_entry=None, sl_price=None,
                tp_price=None, wallet=self.wallet,
            )

        # ── Time-based TP tightening (20h after entry → data-driven tighter TP) ──
        if (self.position is not None
                and self._entry_time is not None
                and not self._time_tp_applied):
            try:
                elapsed_sec = (
                    pd.Timestamp.now(tz="UTC").tz_convert(None)
                    - self._entry_time.tz_convert(None)
                ).total_seconds()
                if elapsed_sec >= TIME_TP_HOURS * 3600:
                    _dyn_tp_pct = _db.compute_time_tp_pct(
                        symbol=self.symbol,
                        min_hold_hours=TIME_TP_HOURS,
                        fallback_pct=TIME_TP_FALLBACK_PCT,
                        scale=TIME_TP_SCALE,
                    )
                    new_tp = self._format_price(
                        self.position.entry_price * (1.0 - _dyn_tp_pct)
                    )
                    self._paper_tp_price  = new_tp
                    self._time_tp_applied = True
                    log.info(
                        f"{COLOR_EXIT}[PAPER][{ts_utc}] {TIME_TP_HOURS:.0f}h elapsed — "
                        f"TP tightened to {new_tp:.8f} ({_dyn_tp_pct*100:.3f}% from entry){COLOR_RESET}"
                    )
                    _db.log_event(
                        ts_utc=ts_utc, level="INFO", event_type="TIME_TP_APPLIED",
                        symbol=self.symbol,
                        message=(
                            f"{TIME_TP_HOURS:.0f}h time TP fired — target "
                            f"{new_tp:.8f} ({_dyn_tp_pct*100:.3f}%)"
                        ),
                        detail={"entry_price": self.position.entry_price, "new_tp": new_tp,
                                "elapsed_hours": round(elapsed_sec / 3600, 2),
                                "time_tp_pct": _dyn_tp_pct,
                                "is_fallback": _dyn_tp_pct == TIME_TP_FALLBACK_PCT},
                    )
            except Exception:
                pass

        # ── Priority 1: Liquidation ──
        if self.position is not None and self._paper_liq_price is not None:
            if self.mark_price >= self._paper_liq_price:
                qty_abs          = abs(self.position.qty)
                liq_price_snap   = self._paper_liq_price
                entry_price_snap = self.position.entry_price
                margin_snap      = self._paper_margin
                entry_fee_snap   = self._paper_entry_fee
                tp_snap          = self._paper_tp_price or 0.0
                # wallet already has margin deducted — forfeiture means it stays as-is
                wallet_liq       = self.wallet
                pnl_net_liq      = -(margin_snap + entry_fee_snap)

                log.info(
                    f"{COLOR_ERROR}[PAPER][{ts_utc}] LIQUIDATED  "
                    f"mark={self.mark_price:.5f} >= liq={liq_price_snap:.5f}{COLOR_RESET}"
                )
                self.trade_count += 1  # liquidation counts as a loss

                # Log liquidation as a trade row
                _db.log_trade(
                    ts_utc=ts_utc, mode="paper", symbol=self.symbol, interval=self.interval,
                    action="EXIT", reason="LIQUIDATED",
                    side="COVER", qty=qty_abs, fill_price=liq_price_snap,
                    notional=qty_abs * liq_price_snap, fee=0.0,
                    entry_price=entry_price_snap, tp_price=tp_snap,
                    mark_price=self.mark_price,
                    wallet_before=wallet_liq + margin_snap + entry_fee_snap,
                    wallet_after=wallet_liq,
                    pnl_gross=-margin_snap, pnl_net=pnl_net_liq,
                    pnl_1x_usdt=float(pnl_net_liq / float(self.leverage)) if self.leverage else 0.0,
                    pnl_pct=float(pnl_net_liq / float(self.initial_wallet) * 100.0) if self.initial_wallet > 0 else 0.0,
                    result="LOSS",
                    ma_len=self.entry_params.ma_len,
                    band_mult=float(self.entry_params.band_mult),
                    tp_pct=float(self.exit_params.tp_pct),
                )
                _db.log_balance_snapshot(
                    ts_utc=ts_utc, symbol=self.symbol, event="PAPER_LIQUIDATED",
                    wallet_usdt=wallet_liq,
                    session_pnl_usdt=wallet_liq - float(self.initial_wallet),
                    session_pnl_pct=float((wallet_liq - float(self.initial_wallet)) / float(self.initial_wallet) * 100.0) if self.initial_wallet > 0 else 0.0,
                )
                _db.log_event(
                    ts_utc=ts_utc, level="ERROR", event_type="LIQUIDATION",
                    symbol=self.symbol,
                    message=f"Position liquidated: mark={self.mark_price:.5f} >= liq={liq_price_snap:.5f}",
                    detail={"entry_price": entry_price_snap, "mark_price": self.mark_price,
                            "liq_price": liq_price_snap, "qty": qty_abs,
                            "margin_lost": margin_snap + entry_fee_snap,
                            "wallet_after": wallet_liq},
                )

                # Entire margin is forfeited — wallet stays as post-entry value
                self.position         = None
                self._paper_margin    = 0.0
                self._paper_tp_price  = None
                self._paper_liq_price = None
                self._paper_entry_fee = 0.0
                self._entry_time      = None
                self._entry_price     = None
                self._time_tp_applied = False
                self.gate.release(self.symbol)
                self._update_account_pnl()
                acted = True

        # ── Priority 2: Take-Profit (in-candle low check) ──
        if not acted and self.position is not None and self._paper_tp_price is not None:
            if l <= self._paper_tp_price:
                self._execute_exit(self._paper_tp_price, "TP", ts_utc)
                acted = True

        # ── Hard stop-loss signal ──
        sl_exit = False
        if self.position is not None and self._entry_price is not None:
            sl_price = self._entry_price * (1.0 + float(self.exit_params.sl_pct))
            if h >= sl_price:
                sl_exit = True

        if entry_sig:
            self.last_signal = {"type": "ENTRY", "time": ts_utc, "placed": False, "filled": False, "price": None, "band": _raw_short}
        elif exit_sig or sl_exit:
            self.last_signal = {"type": "EXIT",  "time": ts_utc, "placed": False, "filled": False, "price": None, "band": _raw_short}

        # ── Priority 3: Stop-loss exit ──
        if not acted and sl_exit and self.position is not None:
            self._execute_exit(c, "STOP_LOSS", ts_utc)
            acted = True

        # ── Priority 4: Band exit ──
        if not acted and exit_sig and self.position is not None:
            self._execute_exit(c, "BAND_EXIT", ts_utc)
            acted = True

        # ── Entry ──
        if not acted and entry_sig and self.position is None and not self._halted:
            if self.wallet >= MIN_WALLET_USDT:
                self._execute_entry(c, ts_utc, ts)
            else:
                log.warning(f"[PAPER][{ts_utc}] Entry signal — wallet {self.wallet:.4f} < {MIN_WALLET_USDT}")
                # ── Shadow for wallet-blocked signal ─────────────────────
                if len(self._shadow_positions) < 5:
                    _sh_ep  = c
                    _sh_tp  = _sh_ep * (1.0 - float(self.exit_params.tp_pct) * LIVE_TP_SCALE)
                    _sh_sl  = _sh_ep * (1.0 + float(self.exit_params.sl_pct))
                    self._shadow_positions.append({
                        "entry_ts": ts_utc, "entry_price": _sh_ep,
                        "tp_price": _sh_tp, "sl_price": _sh_sl,
                        "band": _raw_short, "blocked_by": "WALLET",
                        "candles": 0, "adx_at_entry": adx_val,
                        "rsi_at_entry": rsi_val,
                    })
        elif entry_sig and self.position is not None:
            log.info(f"[PAPER][{ts_utc}] Entry signal — already SHORT (no pyramiding)")
            # ── Shadow for position-blocked signal ───────────────────────
            if len(self._shadow_positions) < 5:
                _sh_ep  = c
                _sh_tp  = _sh_ep * (1.0 - float(self.exit_params.tp_pct) * LIVE_TP_SCALE)
                _sh_sl  = _sh_ep * (1.0 + float(self.exit_params.sl_pct))
                self._shadow_positions.append({
                    "entry_ts": ts_utc, "entry_price": _sh_ep,
                    "tp_price": _sh_tp, "sl_price": _sh_sl,
                    "band": _raw_short, "blocked_by": "POSITION",
                    "candles": 0, "adx_at_entry": adx_val,
                    "rsi_at_entry": rsi_val,
                })

        # ── Display ──
        self._display_candle_close(
            ts_utc=ts_utc, o=o, h=h, l=l, c=c,
            entry_sig=entry_sig, exit_sig=exit_sig,
            adx=adx_val, rsi=rsi_val,
        )


# ─── Module-level seed download (uses public REST only) ────────────────────────

def _download_seed(symbol: str, days_back: int, interval: str):
    """Download last-price and mark-price klines for a symbol via public REST."""
    end_ts   = now_ms()
    start_ts = end_ts - int(days_back * 24 * 60 * 60 * 1000)
    log.info(f"[PAPER] Downloading LAST-price {interval}m for {symbol} ({days_back} days)...")
    df_last = fetch_last_klines(symbol, interval, start_ts, end_ts)
    log.info(f"[PAPER] Downloading MARK-price {interval}m for {symbol} ({days_back} days)...")
    df_mark = fetch_mark_klines(symbol, interval, start_ts, end_ts)
    return df_last, df_mark
