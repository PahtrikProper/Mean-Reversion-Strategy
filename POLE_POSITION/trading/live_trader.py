"""Live Trader — Mean Reversion Strategy (SHORT only)

Entry:  high drops back below premium_k band (crossover)
        AND ADX < 25  (range-bound regime)
        AND RSI >= 40 (not deeply oversold)
Exit:   TP hit (Bybit server-side TP)
        OR trail stop: high >= min_low_since_entry + mult×ATR  (Jason McIntosh)
        OR time exit (days_held >= holding_days)
        OR band exit: low drops below discount_k band (mirrors entry logic)
        OR liquidation (detected via REST poll)

No hard stop-loss — TP, trail stop, time, and band exits only.
Trail stop trails DOWN as price falls for the SHORT, locking in profit.
"""

import pandas as pd
import threading
import time
import logging
import json
import math
import sys
from websocket import WebSocketApp
from typing import Dict, Any, Optional

from ..utils.constants import (
    DAYS_BACK_SEED,
    MIN_WALLET_USDT,
    MAX_SYMBOL_FRACTION,
    KEEP_CANDLES,
    STARTING_WALLET,
    INIT_TRIALS,
    REOPT_INTERVAL_SEC,
    COLOR_CONFIRMED,
    COLOR_RESET,
    COLOR_SUBMITTED,
    COLOR_ENTRY,
    COLOR_EXIT,
    COLOR_ERROR,
    TRADES_CSV_PATH,
    LIVE_TP_SCALE,
)
from ..utils import constants as _C
from ..utils.data_structures import RealPosition, PendingSignal, EntryParams, ExitParams, MC_MIN_TRADES, MC_SIMS
from ..utils.position_gate import PositionGate
from ..utils.logger import log_order, csv_append
from ..utils.helpers import now_ms, interval_minutes, leverage_for, taker_fee_for, maker_fee_for
from ..utils.trading_status import get_status_monitor
from ..core.indicators import (
    build_indicators,
    compute_entry_signals_raw,
    resolve_entry_signals,
    compute_exit_signals_raw,
    ADX_THRESHOLD,
    RSI_NEUTRAL_LO,
)
from ..trading.bybit_client import BybitPrivateClient, fetch_last_klines, fetch_mark_klines
from ..trading.liquidation import pick_risk_tier
from ..backtest.backtester import run_monte_carlo, mc_score
from ..utils.plotting import print_monte_carlo_report
from ..optimize.optimizer import optimise_params

log = logging.getLogger("live_trader")


class LiveRealTrader:
    """Live trader for Mean Reversion Strategy on one symbol."""

    def __init__(
        self,
        symbol: str,
        df_last_seed: pd.DataFrame,
        df_mark_seed: pd.DataFrame,
        risk_df: pd.DataFrame,
        entry_params: EntryParams,
        exit_params: ExitParams,
        client: BybitPrivateClient,
        gate: PositionGate,
        interval: str,
    ):
        self.symbol       = symbol
        self.gate         = gate
        self.client       = client
        self.risk_df      = risk_df
        self.interval     = interval
        self.leverage     = leverage_for(symbol)
        self.taker_fee    = taker_fee_for(symbol)
        self.maker_fee    = maker_fee_for(symbol)
        self.entry_params = entry_params
        self.exit_params  = exit_params
        self.instrument   = self.client.get_instrument_info(self.symbol)

        self.wallet          = float(self.client.get_unified_usdt())
        self.initial_wallet  = self.wallet
        self.position: Optional[RealPosition] = self.client.get_position(self.symbol)
        if self.position is not None:
            self.gate.force_acquire(self.symbol)

        # DataFrame: last-price OHLCV
        self.df = df_last_seed[["ts", "open", "high", "low", "close", "volume"]].copy().reset_index(drop=True)
        self.closed_candle_count = 0
        self.last_reopt_time     = time.time()
        self._entry_time: Optional[pd.Timestamp] = None  # track when we entered for time exit

        # Track entry price and wallet-at-entry for external-close detection and logging.
        # Set from an existing position at startup so re-attached bot sessions also detect
        # manual closes correctly.
        self._entry_price: Optional[float] = (
            float(self.position.entry_price) if self.position is not None else None
        )
        self._wallet_at_entry: Optional[float] = None

        if len(df_mark_seed) == 0:
            raise RuntimeError("No mark seed data")
        self.mark_price = float(df_mark_seed["close"].iloc[-1])

        self.trade_count           = 0
        self.win_count             = 0
        self.realized_pnl_net      = 0.0
        self.account_pnl_usdt      = 0.0
        self.account_pnl_pct       = 0.0
        self.last_signal: Optional[dict] = None
        self._last_entry_fee: float = 0.0  # entry fee stored for accurate exit PnL
        self._min_low_since_entry: Optional[float] = None  # Jason McIntosh trail stop
        self._reopt_running: bool = False  # guard against concurrent reopt threads

        self._recompute_indicators()

    # ── Indicator rebuild ──────────────────────────────────────────────────────

    def _recompute_indicators(self):
        """Rebuild bands, ADX, and RSI columns from raw OHLCV data."""
        min_len = self.entry_params.ma_len + 20
        if len(self.df) < min_len:
            return
        base    = self.df[["ts", "open", "high", "low", "close", "volume"]].copy()
        self.df = build_indicators(
            base,
            ma_len=self.entry_params.ma_len,
            band_mult=self.entry_params.band_mult,
            trail_atr_period=self.exit_params.trail_atr_period,
        )

    # ── Order helpers ─────────────────────────────────────────────────────────

    def _format_qty(self, raw_qty: float) -> float:
        lot = self.instrument.get("lotSizeFilter", {})
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
        """Round raw_price down to the instrument's tick size (priceFilter.tickSize).

        Uses floor (not round) so a SHORT TP is always set at or below the
        intended level rather than above it."""
        pf       = self.instrument.get("priceFilter", {})
        tick_str = str(pf.get("tickSize", "0.0001") or "0.0001")
        try:
            tick = float(tick_str)
            if tick <= 0:
                raise ValueError("non-positive tick")
        except (ValueError, TypeError):
            tick = 0.0001
        price = math.floor(raw_price / tick) * tick
        # Determine decimal places from the tick string so we don't accumulate
        # floating-point noise (e.g. "0.0001" → 4 dp, "1" → 0 dp).
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

    def _ensure_entry_risk_checks(self, qty: float, price: float, wallet_before: float):
        notional = qty * price
        mn = self._min_notional()
        if mn and notional < mn:
            raise RuntimeError(f"Notional {notional:.4f} < min {mn:.4f}")
        margin_req = notional / float(self.leverage)
        est_fee    = notional * float(self.maker_fee)
        if wallet_before < margin_req + est_fee:
            raise RuntimeError(
                f"Insufficient margin: wallet {wallet_before:.4f} < required {margin_req + est_fee:.4f}"
            )

    def _refresh_state(self):
        self.wallet         = float(self.client.get_unified_usdt())
        self.position       = self.client.get_position(self.symbol)
        self.account_pnl_usdt = self.wallet - float(self.initial_wallet)
        self.account_pnl_pct  = (
            (self.account_pnl_usdt / float(self.initial_wallet)) * 100.0
            if self.initial_wallet else 0.0
        )

    # ── Mark price callback ────────────────────────────────────────────────────

    def on_mark_price_update(self, mark_price: float, ts_utc: str):
        self.mark_price = float(mark_price)

    # ── Entry execution ────────────────────────────────────────────────────────

    def _execute_entry(self, close_price: float, ts_utc: str, bar_ts: pd.Timestamp):
        """Place a market SHORT entry order."""
        if self.position is not None:
            log.warning(f"[{ts_utc}] Cannot enter — already in position")
            return
        if self.wallet < MIN_WALLET_USDT:
            log.warning(f"[{ts_utc}] Skipping entry — wallet {self.wallet:.4f} < {MIN_WALLET_USDT}")
            return
        if not self.gate.try_acquire(self.symbol):
            log.warning(f"[{ts_utc}] Skipping entry — gate blocked")
            return

        c = close_price
        wallet_before = self.wallet
        qty = 0.0
        try:
            max_margin = self.wallet * float(MAX_SYMBOL_FRACTION)
            qty        = self._format_qty((max_margin * float(self.leverage)) / c)
            self._ensure_entry_risk_checks(qty, c, wallet_before)

            log_order(ts_utc=ts_utc, symbol=self.symbol, side="SHORT",
                      qty=qty, price=c, order_type="ENTRY", status="PLACED",
                      reason="BAND_ENTRY")

            order_id = self.client.place_market_order(self.symbol, "Sell", qty, reduce_only=False)
            if self.last_signal:
                self.last_signal["placed"] = True
            self._refresh_state()
            summary = self.client.get_execution_summary(self.symbol, order_id)

            if summary is None:
                log_order(ts_utc=ts_utc, symbol=self.symbol, side="SHORT",
                          qty=qty, price=c, order_type="ENTRY", status="FAILED",
                          order_id=order_id, error="No execution summary")
                self.gate.release(self.symbol)
                return

            fill_price  = summary["avg_price"]
            filled_qty  = summary["qty"]
            # Store entry fee so exit PnL log can subtract both fees
            self._last_entry_fee = (filled_qty * fill_price) * float(self.taker_fee)
            if self.last_signal:
                self.last_signal["filled"] = True
                self.last_signal["price"]  = fill_price

            # Compute TP using tick-size rounding (not arbitrary 8 dp).
            # LIVE_TP_SCALE shrinks the distance so the live order sits closer
            # to the fill price and is more reliably triggered than in backtests.
            tp_price = self._format_price(fill_price * (1.0 - float(self.exit_params.tp_pct) * LIVE_TP_SCALE))

            # Record entry state and write all logs BEFORE the TP call.
            # If set_trading_stop raises (wrong precision, API error, Unified
            # account mode mismatch, etc.) the entry is still fully recorded
            # and indicator / time exits will close the position as fallback.
            self._entry_time      = bar_ts
            self._entry_price     = fill_price
            self._wallet_at_entry = wallet_before

            log.info(
                f"{COLOR_SUBMITTED}[{ts_utc}] SHORT ENTRY FILLED: "
                f"qty={filled_qty:.6f} fill={fill_price:.8f} "
                f"TP={tp_price} ({self.exit_params.tp_pct*100:.2f}%){COLOR_RESET}"
            )
            log_order(ts_utc=ts_utc, symbol=self.symbol, side="SHORT",
                      qty=filled_qty, price=fill_price,
                      order_type="ENTRY", status="FILLED",
                      order_id=order_id, reason="BAND_ENTRY")

            self._log_real_trade(
                ts_utc=ts_utc, action="ENTRY", reason="BAND_ENTRY",
                side="SHORT", qty=filled_qty, fill_price=fill_price,
                entry_price=fill_price, wallet_before=wallet_before,
                wallet_after=self.wallet,
            )

            # Set server-side TP in its own isolated try/except so a Bybit
            # rejection does NOT release the gate or abort the entry sequence.
            try:
                self.client.set_trading_stop(self.symbol, tp_price)
            except Exception as tp_err:
                log.warning(
                    f"[{ts_utc}] Failed to set server-side TP: {tp_err} — "
                    f"indicator/time exits will still close the position"
                )

        except Exception as e:
            log_order(ts_utc=ts_utc, symbol=self.symbol, side="SHORT",
                      qty=qty, price=c, order_type="ENTRY", status="FAILED", error=str(e))
            self.gate.release(self.symbol)
            log.error(f"[{ts_utc}] Entry failed: {e}")
            # Return here so we never fall through to the post-try position check.
            # The gate was already released above on the failure path.
            return

    # ── Exit execution ─────────────────────────────────────────────────────────

    def _execute_exit(self, close_price: float, reason: str, ts_utc: str):
        """Place a market Buy to close the SHORT position."""
        if self.position is None:
            return
        pos          = self.position
        entry_price  = pos.entry_price
        qty_abs      = abs(pos.qty)
        wallet_before = self.wallet
        qty_to_close  = 0.0
        try:
            qty_to_close = self._format_qty(qty_abs)
            log_order(ts_utc=ts_utc, symbol=self.symbol, side="SHORT",
                      qty=qty_to_close, price=close_price,
                      order_type="EXIT", status="PLACED", reason=reason)

            order_id = self.client.place_market_order(self.symbol, "Buy", qty_to_close, reduce_only=True)
            if self.last_signal:
                self.last_signal["placed"] = True
            self._refresh_state()
            summary = self.client.get_execution_summary(self.symbol, order_id)
            if summary is None:
                log_order(ts_utc=ts_utc, symbol=self.symbol, side="SHORT",
                          qty=qty_to_close, price=close_price,
                          order_type="EXIT", status="FAILED",
                          order_id=order_id, error="No execution summary")
                log.error(f"[{ts_utc}] Exit order {order_id} has no summary")
                if self.position is None:
                    self.gate.release(self.symbol)
                return

            fill_price = summary["avg_price"]
            filled_qty = summary["qty"]
            if self.last_signal:
                self.last_signal["filled"] = True
                self.last_signal["price"]  = fill_price
            log_order(ts_utc=ts_utc, symbol=self.symbol, side="SHORT",
                      qty=filled_qty, price=fill_price,
                      order_type="EXIT", status="FILLED",
                      order_id=order_id, reason=reason)
            self._log_real_trade(
                ts_utc=ts_utc, action="EXIT", reason=reason,
                side="COVER", qty=filled_qty, fill_price=fill_price,
                entry_price=entry_price, wallet_before=wallet_before,
                wallet_after=self.wallet,
            )
            self._entry_time           = None   # clear entry state on exit
            self._entry_price          = None
            self._wallet_at_entry      = None
            self._last_entry_fee       = 0.0
            self._min_low_since_entry  = None   # reset trail stop tracking
        except Exception as e:
            log_order(ts_utc=ts_utc, symbol=self.symbol, side="SHORT",
                      qty=qty_to_close if qty_to_close else qty_abs,
                      price=close_price, order_type="EXIT", status="FAILED", error=str(e))
            log.error(f"[{ts_utc}] Exit order failed: {e}")
            try:
                self._refresh_state()
            except Exception:
                pass

        if self.position is None:
            self.gate.release(self.symbol)

    # ── External close handler ─────────────────────────────────────────────────

    def _handle_external_close(self, ts_utc: str, current_close: float):
        """Called when the bot detects that a position was closed outside its own
        _execute_exit path — e.g. a server-side TP hit, a liquidation, or a manual
        close via the Bybit app or web UI.

        Fetches the actual exit price / qty from Bybit's closed-pnl endpoint,
        logs the trade to CSV, updates win/loss counters, releases the gate,
        and resets all position-tracking state."""
        entry_price = self._entry_price if self._entry_price else current_close
        exit_price  = current_close   # fallback if API call fails
        qty_abs     = 0.0

        try:
            record = self.client.get_last_closed_pnl(self.symbol)
            if record:
                ap = float(record.get("avgExitPrice") or 0)
                if ap > 0:
                    exit_price = ap
                sz = float(record.get("closedSize") or 0)
                if sz > 0:
                    qty_abs = sz
                ae = float(record.get("avgEntryPrice") or 0)
                if ae > 0:
                    entry_price = ae
        except Exception as exc:
            log.warning(f"[{ts_utc}] get_last_closed_pnl failed: {exc}")

        wallet_before = self._wallet_at_entry if self._wallet_at_entry else self.wallet

        if qty_abs > 0:
            try:
                self._log_real_trade(
                    ts_utc=ts_utc, action="EXIT", reason="EXTERNAL_CLOSE",
                    side="COVER", qty=qty_abs, fill_price=exit_price,
                    entry_price=entry_price, wallet_before=wallet_before,
                    wallet_after=self.wallet,
                )
            except Exception as exc:
                log.error(f"[{ts_utc}] Failed to log external close trade: {exc}")
        else:
            log.warning(
                f"[{ts_utc}] External close detected for {self.symbol} "
                f"but qty unknown — trade not logged"
            )

        log.info(
            f"{COLOR_EXIT}[{ts_utc}] EXTERNAL CLOSE — {self.symbol}  "
            f"qty={qty_abs:.4f}  exit=${exit_price:.5f}  "
            f"entry=${entry_price:.5f}{COLOR_RESET}"
        )

        # Reset all position-tracking state and release the slot.
        self.gate.release(self.symbol)
        self._entry_time          = None
        self._entry_price         = None
        self._wallet_at_entry     = None
        self._last_entry_fee      = 0.0
        self._min_low_since_entry = None   # reset trail stop tracking

    # ── Trade log ──────────────────────────────────────────────────────────────

    def _log_real_trade(
        self,
        ts_utc: str,
        action: str,
        reason: str,
        side: str,
        qty: float,
        fill_price: float,
        entry_price: float,
        wallet_before: float,
        wallet_after: float,
    ):
        if side == "COVER":
            pnl_gross = (entry_price - fill_price) * qty
        else:
            pnl_gross = 0.0
        fee_rate   = self.taker_fee
        fee        = (qty * fill_price) * float(fee_rate)
        # For exits subtract both the exit fee AND the stored entry fee.
        # For entries pnl_gross is 0 so pnl_net is just the negative entry fee.
        if side == "COVER":
            pnl_net = pnl_gross - fee - self._last_entry_fee
        else:
            pnl_net = pnl_gross - fee
        tp_price   = entry_price * (1.0 - float(self.exit_params.tp_pct) * LIVE_TP_SCALE)

        result = ""
        if action == "EXIT":
            self.trade_count      += 1
            win = pnl_net > 0
            self.win_count        += win
            self.realized_pnl_net += pnl_net
            result = "WIN" if win else "LOSS"

        csv_append(TRADES_CSV_PATH, [
            ts_utc, self.symbol, action, reason, f"{side}_SIGNAL",
            side, float(qty), float(fill_price),
            float(qty * fill_price), float(fee),
            float(entry_price), float(tp_price),
            float(self.mark_price),
            float(wallet_before), float(wallet_after),
            float(pnl_gross), float(pnl_net),
            float(pnl_net / float(self.leverage)) if self.leverage else 0.0,
            float(pnl_net / wallet_before * 100.0) if wallet_before > 0 else 0.0,
            result,
            self.entry_params.ma_len,
            float(self.entry_params.band_mult),
            self.exit_params.holding_days,
            float(self.exit_params.tp_pct),
        ])

        if action == "EXIT":
            pnl_sign = "+" if pnl_net >= 0 else ""
            result_color = COLOR_ENTRY if result == "WIN" else COLOR_ERROR
            log.info(
                f"{result_color}[{ts_utc}] {result}  {reason}  "
                f"pnl=${pnl_sign}{pnl_net:.4f}  ({pnl_sign}{pnl_net / wallet_before * 100.0:.2f}%)  "
                f"fill={fill_price:.8f}  qty={qty:.6f}  wallet=${wallet_after:.2f}{COLOR_RESET}"
            )
        else:
            log.info(
                f"{COLOR_SUBMITTED}[{ts_utc}] {action} {reason} {side} "
                f"fill={fill_price:.8f}  qty={qty:.6f}  wallet=${wallet_after:.2f}{COLOR_RESET}"
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
        ep  = self.entry_params
        xp  = self.exit_params
        wr  = (self.win_count / self.trade_count * 100) if self.trade_count > 0 else 0.0

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
            f"[{ts_utc}]  {self.symbol} #{self.closed_candle_count} {self.interval}m  "
            f"C={c:.5f}  |  "
            f"ADX={adx:.1f}(<{ADX_THRESHOLD:.0f})  RSI={rsi:.1f}(>={RSI_NEUTRAL_LO:.0f})  "
            f"Hold={xp.holding_days}d  |  "
            f"{sig_col}{sig_str}{COLOR_RESET}  |  "
            f"W={self.win_count} L={self.trade_count - self.win_count} WR={wr:.0f}%  "
            f"wallet=${self.wallet:.2f}  "
            f"{pnl_color}session={pnl_sign}${self.account_pnl_usdt:.2f} "
            f"({pnl_sign}{self.account_pnl_pct:.2f}%){COLOR_RESET}"
        )

        if self.position is not None:
            pos     = self.position
            qty_abs = abs(pos.qty)
            upnl    = (pos.entry_price - self.mark_price) * qty_abs
            margin  = pos.entry_price * qty_abs / float(self.leverage)
            upnl_pct = (upnl / margin * 100) if margin > 0 else 0.0
            tp_disp = pos.entry_price * (1.0 - float(xp.tp_pct) * LIVE_TP_SCALE)
            sign    = "+" if upnl >= 0 else ""
            days_held = ""
            if self._entry_time is not None:
                try:
                    dh = (pd.Timestamp.now(tz="UTC").tz_convert(None) - self._entry_time.tz_convert(None)).total_seconds() / 86400.0
                    days_held = f"  held={dh:.2f}d"
                except Exception:
                    pass
            log.info(
                f"  SHORT  entry=${pos.entry_price:.5f}  tp=${tp_disp:.5f}  "
                f"mark=${self.mark_price:.5f}  qty={qty_abs:.4f}  "
                f"uPnL=${sign}{upnl:.4f} ({sign}{upnl_pct:.2f}%){days_held}"
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
            name=f"reopt-{self.symbol}",
        ).start()

    def _run_reoptimise(self):
        """Background thread: re-run param search and update params if improved."""
        log.info(f"[REOPT] {self.symbol}: re-running band crossover optimisation...")
        try:
            df_last, df_mark = download_seed_history(self.symbol, DAYS_BACK_SEED, self.interval)
            lev       = leverage_for(self.symbol)
            fee       = taker_fee_for(self.symbol)
            maker_fee = maker_fee_for(self.symbol)

            saved_best = {
                "ma_len":       self.entry_params.ma_len,
                "band_mult":    self.entry_params.band_mult,
                "holding_days": self.exit_params.holding_days,
                "tp_pct":       self.exit_params.tp_pct,
            }

            opt = optimise_params(
                df_last=df_last, df_mark=df_mark,
                risk_df=self.risk_df,
                trials=INIT_TRIALS,
                lookback_candles=min(len(df_last), len(df_mark)),
                event_name=f"REOPT_{self.symbol}_{self.interval}m",
                leverage=lev, fee_rate=fee, maker_fee_rate=maker_fee,
                interval_minutes=interval_minutes(self.interval),
                saved_best=saved_best,
            )

            new_entry  = opt["entry_params"]
            new_exit   = opt["exit_params"]
            new_result = opt["best_result"]

            records      = getattr(new_result, "trade_records", []) or []
            new_mc_score = float("-inf")
            if len(records) >= MC_MIN_TRADES:
                mc_res = run_monte_carlo(records, float(STARTING_WALLET), n_sims=MC_SIMS)
                if mc_res:
                    new_mc_score = mc_score(mc_res)
                    print_monte_carlo_report(
                        mc_res, float(STARTING_WALLET),
                        len(records), self.symbol, self.interval
                    )

            log.info(
                f"[REOPT] {self.symbol}: MC score={new_mc_score:.4f} | "
                f"MA-len={new_entry.ma_len} "
                f"BandMult={new_entry.band_mult:.2f}% "
                f"Hold={new_exit.holding_days}d "
                f"TP={new_exit.tp_pct*100:.2f}%"
            )

            if new_mc_score > 0:
                # Update params atomically; _recompute_indicators() runs on next candle close.
                self.entry_params = new_entry
                self.exit_params  = new_exit
                log.info(f"[REOPT] {self.symbol}: params updated (MC score={new_mc_score:.4f})")
            else:
                log.info(f"[REOPT] {self.symbol}: MC score {new_mc_score:.4f} <= 0 — keeping old params")

        except Exception as e:
            log.error(f"[REOPT] {self.symbol}: failed: {e}")
        finally:
            self.last_reopt_time = time.time()
            self._reopt_running = False

    # ── Main candle callback ───────────────────────────────────────────────────

    def on_closed_candle(self, candle: Dict[str, Any]):
        """Called once per confirmed closed candle from the WebSocket.

        Processing order:
          1. Append candle to DataFrame, rebuild indicators
          2. Refresh wallet/position state from Bybit REST
          3. Skip if not enough candles for warm-up
          4. Detect externally-closed position (server TP or liquidation)
          5. Update min_low_since_entry for trail stop tracking
          6. Check Jason McIntosh trail stop (high >= min_low + mult×ATR)  [priority 3]
          7. Check time exit (if in position and holding_days exceeded)     [priority 4]
          8. Check band exit (low drops below discount_k band — mirrors entry logic) [priority 5]
          9. Check entry signal (band crossover AND ADX gate AND RSI gate)
         10. Log candle-close summary
        """
        ts      = pd.to_datetime(int(candle["start"]), unit="ms", utc=True)
        ts_utc  = ts.strftime("%Y-%m-%d %H:%M:%S")
        get_status_monitor().on_candle_received(self.symbol)

        o   = float(candle["open"])
        h   = float(candle["high"])
        l   = float(candle["low"])
        c   = float(candle["close"])
        vol = float(candle.get("volume", 0))

        new_row = pd.DataFrame([{"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": vol}])
        self.df = pd.concat([self.df, new_row], ignore_index=True)
        if len(self.df) > KEEP_CANDLES:
            self.df = self.df.iloc[-KEEP_CANDLES:].reset_index(drop=True)

        self._recompute_indicators()
        self.closed_candle_count += 1
        self._maybe_reoptimise()
        self._refresh_state()

        # acted: set True after any exit or external-close this candle so we never
        # place an entry on the same candle we just closed a position.
        acted = False

        # ── Detect externally-closed position (server TP, liquidation, or manual close) ──
        # _entry_price is set whenever we are tracking an open position (whether entered
        # this session or detected at startup).  If position is now gone but we were
        # tracking one, the close happened outside _execute_exit — handle it properly.
        if self.position is None and self._entry_price is not None:
            self._handle_external_close(ts_utc, c)
            acted = True

        # ── Warm-up guard ──
        min_len = self.entry_params.ma_len + 20
        if len(self.df) < min_len or "adx" not in self.df.columns:
            log.info(
                f"[{ts_utc}] {self.symbol} Candle #{self.closed_candle_count} "
                f"(warm-up {len(self.df)}/{min_len})  "
                f"O={o:.4f} H={h:.4f} L={l:.4f} C={c:.4f}"
            )
            return

        row  = self.df.iloc[-1]
        prev = self.df.iloc[-2]

        adx_val = float(row["adx"])
        rsi_val = float(row["rsi"]) if not pd.isna(row["rsi"]) else 100.0

        _raw_short = compute_entry_signals_raw(
            current_row=row, prev_row=prev,
            current_high=h, current_low=l,
        )
        entry_sig = resolve_entry_signals(_raw_short, adx_val, rsi_val) > 0
        exit_sig  = compute_exit_signals_raw(
            current_row=row, prev_row=prev,
            current_low=l, current_high=h,
        ) > 0

        # ── Update Jason McIntosh trail stop tracking ──
        if self.position is not None and self._min_low_since_entry is not None:
            self._min_low_since_entry = min(self._min_low_since_entry, l)

        # ── Time exit check ──
        time_exit = False
        if self.position is not None and self._entry_time is not None:
            try:
                bar_ts_naive  = ts.tz_convert(None) if ts.tzinfo else ts
                entry_ts_naive = self._entry_time.tz_convert(None) if self._entry_time.tzinfo else self._entry_time
                days_held = (bar_ts_naive - entry_ts_naive).total_seconds() / 86400.0
                if days_held >= float(self.exit_params.holding_days):
                    time_exit = True
            except Exception:
                pass

        # ── Jason McIntosh trail stop signal ──
        trail_stop_exit = False
        if self.position is not None and self._min_low_since_entry is not None:
            trail_atr_val = float(row["atr"]) if "atr" in self.df.columns and not pd.isna(row["atr"]) else 0.0
            if trail_atr_val > 0:
                trail_stop_lvl = self._min_low_since_entry + float(self.exit_params.trail_atr_mult) * trail_atr_val
                if h >= trail_stop_lvl:
                    trail_stop_exit = True

        if entry_sig:
            self.last_signal = {"type": "ENTRY", "time": ts_utc, "placed": False, "filled": False, "price": None}
        elif exit_sig or time_exit or trail_stop_exit:
            self.last_signal = {"type": "EXIT",  "time": ts_utc, "placed": False, "filled": False, "price": None}

        # ── Jason McIntosh trail stop exit ──  (priority 3, matches backtester)
        if not acted and trail_stop_exit and self.position is not None:
            self._execute_exit(c, "TRAIL_STOP", ts_utc)
            acted = True

        # ── Time exit ──  (priority 4, matches backtester)
        if not acted and time_exit and self.position is not None:
            self._execute_exit(c, "TIME_EXIT", ts_utc)
            acted = True

        # ── Band exit ──  (priority 5, only if no other action this candle)
        if not acted and exit_sig and self.position is not None:
            self._execute_exit(c, "BAND_EXIT", ts_utc)
            acted = True

        # ── Entry logic ──  (never on the same candle as an exit or external close)
        if not acted and entry_sig and self.position is None:
            if self.wallet >= MIN_WALLET_USDT:
                self._execute_entry(c, ts_utc, ts)
                # Initialise trail stop tracking with this bar's low if entry filled
                if self.position is not None and self._min_low_since_entry is None:
                    self._min_low_since_entry = l
            else:
                log.warning(f"[{ts_utc}] Entry signal — wallet {self.wallet:.4f} < {MIN_WALLET_USDT}")
        elif entry_sig and self.position is not None:
            log.info(f"[{ts_utc}] Entry signal — already SHORT (no pyramiding)")

        # ── Display ──
        self._display_candle_close(
            ts_utc=ts_utc, o=o, h=h, l=l, c=c,
            entry_sig=entry_sig, exit_sig=exit_sig or time_exit,
            adx=adx_val, rsi=rsi_val,
        )


# ─── WebSocket loop ────────────────────────────────────────────────────────────

def start_live_ws(traders: Dict[str, Any], stop_event: threading.Event = None):
    """Start the Bybit public WebSocket for kline (candle) and ticker (mark price) data.
    Reconnects automatically on disconnect.
    Press Ctrl+C to stop, or set stop_event (threading.Event) to stop programmatically."""
    ws_url   = "wss://stream.bybit.com/v5/public/linear"
    topic_k  = sorted({f"kline.{t.interval}.{s}" for s, t in traders.items()})
    topic_t  = [f"tickers.{s}" for s in traders]
    topic_k_set = set(topic_k)
    topic_t_set = set(topic_t)

    status_monitor = get_status_monitor()
    status_monitor.start(traders, update_interval=180.0)

    last_msg_time      = {"t": time.time()}
    last_ping_time     = {"t": 0.0}
    _ws_ref            = {"ws": None}
    _ping_stop         = {"stop": False}
    PING_SILENCE_SEC   = 20.0
    PING_MIN_INTERVAL  = 10.0

    def _ping_thread():
        while not _ping_stop["stop"]:
            time.sleep(5)
            ws = _ws_ref["ws"]
            if ws is None:
                continue
            now     = time.time()
            silence = now - last_msg_time["t"]
            since   = now - last_ping_time["t"]
            if silence >= PING_SILENCE_SEC and since >= PING_MIN_INTERVAL:
                try:
                    ws.send(json.dumps({"op": "ping"}))
                    last_ping_time["t"] = now
                except Exception as exc:
                    log.warning(f"WS ping failed: {exc}")

    def on_open(ws):
        _ws_ref["ws"] = ws
        last_msg_time["t"] = time.time()
        ws.send(json.dumps({"op": "subscribe", "args": topic_k + topic_t}))
        log.info(f"WebSocket connected. Subscribed: {', '.join(topic_k + topic_t)}")
        log.info("Live trading started. Press Ctrl+C to stop.\n")

    def on_message(ws, message):
        last_msg_time["t"] = time.time()
        try:
            msg = json.loads(message)
        except Exception:
            return
        if isinstance(msg, dict) and msg.get("op") in ("subscribe", "pong"):
            return

        topic  = msg.get("topic", "")
        data   = msg.get("data")
        symbol = None
        if topic.startswith("kline."):
            parts = topic.split(".")
            if len(parts) >= 3:
                symbol = parts[2]
        elif topic.startswith("tickers."):
            symbol = topic.split(".", 1)[1]
        if symbol is None or symbol not in traders:
            return
        trader = traders[symbol]

        if topic in topic_t_set and data:
            d = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else None)
            if d:
                mp = d.get("markPrice")
                if mp is not None:
                    ts_utc = pd.Timestamp.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                    trader.on_mark_price_update(float(mp), ts_utc)
            return

        if topic in topic_k_set and data:
            for c in data:
                if c.get("confirm") is True:
                    trader.on_closed_candle(c)

    def on_error(ws, error):
        log.error(f"WebSocket error: {error}")

    def on_close(ws, code, msg):
        _ws_ref["ws"] = None
        log.warning(f"WebSocket closed: {code} {msg}")

    def run_one():
        ws = WebSocketApp(ws_url, on_open=on_open, on_message=on_message,
                          on_error=on_error, on_close=on_close)
        ws.run_forever(ping_interval=None, ping_timeout=None)

    def _stop_watcher():
        """Watches stop_event and closes the WebSocket when it is set."""
        if stop_event is None:
            return
        stop_event.wait()
        _ping_stop["stop"] = True
        ws = _ws_ref.get("ws")
        if ws:
            try:
                ws.close()
            except Exception:
                pass

    _ping_stop["stop"] = False
    threading.Thread(target=_ping_thread,  daemon=True, name="ws-ping").start()
    threading.Thread(target=_stop_watcher, daemon=True, name="ws-stop").start()

    try:
        while True:
            if stop_event and stop_event.is_set():
                break
            try:
                run_one()
            except Exception as e:
                log.error(f"WS crashed: {e}")
            if stop_event and stop_event.is_set():
                break
            log.warning("WebSocket disconnected. Reconnecting in 5s...")
            time.sleep(5)
    finally:
        _ping_stop["stop"] = True
        status_monitor.stop()


# ─── Seed history download ─────────────────────────────────────────────────────

def download_seed_history(symbol: str, days_back: int, interval: str):
    """Download last-price and mark-price kline history for seeding the live trader."""
    end_ts   = now_ms()
    start_ts = end_ts - int(days_back * 24 * 60 * 60 * 1000)

    log.info(f"Downloading LAST-price {interval}m history for {symbol} ({days_back} days)...")
    df_last = fetch_last_klines(symbol, interval, start_ts, end_ts)
    log.info(f"Last-price candles: {len(df_last)}")

    log.info(f"Downloading MARK-price {interval}m history for {symbol} ({days_back} days)...")
    df_mark = fetch_mark_klines(symbol, interval, start_ts, end_ts)
    log.info(f"Mark-price candles: {len(df_mark)}")

    return df_last, df_mark
