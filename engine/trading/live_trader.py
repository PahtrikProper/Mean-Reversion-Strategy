"""Live Trader — Mean Reversion Strategy (LONG spot margin)

Entry:  low bounces back above discount_k band (crossover)
        AND ADX < 25  (range-bound regime)
        AND RSI <= 50 (neutral-to-oversold close confirms the dip)
Exit:   Liquidation: low  <= entry * (lev-1) / (lev * (1-MMR))           [priority 1]
        OR take-profit: high >= entry * (1 + tp_pct)                      [priority 2]
        OR stop-loss: low  <= entry * (1 - sl_pct)                        [hard guard; priority 3]
        OR band exit: high rises above premium_k band (mirrors entry logic) [priority 4]
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
    LIVE_TP_SCALE,
    TIME_TP_HOURS,
    TIME_TP_FALLBACK_PCT,
    TIME_TP_SCALE,
    SIGNAL_DROUGHT_HOURS,
    MAX_LOSS_PCT,
    CANDLE_INTERVALS,
    SYMBOLS,
)
from ..utils import constants as _C
from ..utils.data_structures import RealPosition, PendingSignal, EntryParams, ExitParams, MC_MIN_TRADES, MC_SIMS
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
from ..trading.bybit_client import BybitPrivateClient, fetch_last_klines, fetch_mark_klines, fetch_risk_tiers, get_instrument_info
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
        self.leverage     = exit_params.leverage
        self.taker_fee    = taker_fee_for(symbol)
        self.maker_fee    = maker_fee_for(symbol)
        self.entry_params = entry_params
        self.exit_params  = exit_params
        self.instrument   = self.client.get_instrument_info(self.symbol)

        # Configure spot margin leverage on Bybit before fetching wallet/position
        try:
            self.client.ensure_futures_setup(self.symbol, leverage=float(exit_params.leverage))
        except Exception as _lev_err:
            log.warning(f"Could not set leverage for {symbol}: {_lev_err}")

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
        # ── Reliability & guard attributes ────────────────────────────────────
        self._last_signal_ts: float      = time.time()   # for drought detection
        self._halted: bool               = False          # True when max-loss fired
        self._halt_ts: Optional[float]   = None           # when halt started
        self._refresh_failed: bool       = False          # True when last REST refresh failed
        self._last_entry_fee: float = 0.0  # entry fee stored for accurate exit PnL
        self._reopt_running: bool = False  # guard against concurrent reopt threads
        self._time_tp_applied: bool = False  # True once time-based TP tightening fires
        self._shadow_positions: list = []  # virtual "what if" trades for gate-blocked signals
        self._traders_ref: Optional[Dict] = None  # set by caller; used for interval-switching at re-opt

        self._recompute_indicators()

    # ── Indicator rebuild ──────────────────────────────────────────────────────

    def _recompute_indicators(self):
        """Rebuild bands, ADX, and RSI columns from raw OHLCV data."""
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

        Uses floor (not round) so a LONG TP is always set at or below the
        intended level, avoiding overfill."""
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
        """Query wallet + position from Bybit REST.  On failure, keep cached
        values and log a warning so the candle callback can still run exits."""
        try:
            self.wallet   = float(self.client.get_unified_usdt())
            self.position = self.client.get_position(self.symbol)
            self.account_pnl_usdt = self.wallet - float(self.initial_wallet)
            self.account_pnl_pct  = (
                (self.account_pnl_usdt / float(self.initial_wallet)) * 100.0
                if self.initial_wallet else 0.0
            )
            self._refresh_failed = False
        except Exception as _re:
            self._refresh_failed = True
            _ts = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M:%S")
            log.warning(f"[{self.symbol}] _refresh_state failed (cached state kept): {_re}")
            _db.log_event(
                ts_utc=_ts, level="WARNING", event_type="REFRESH_STATE_FAILED",
                symbol=self.symbol,
                message=f"REST refresh failed — using cached wallet/position: {_re}",
                detail={"error": str(_re)},
            )

    # ── Mark price callback ────────────────────────────────────────────────────

    def on_mark_price_update(self, mark_price: float, ts_utc: str):
        self.mark_price = float(mark_price)
        _db.log_mark_price_tick(ts_utc=ts_utc, symbol=self.symbol, mark_price=self.mark_price)

    # ── Entry execution ────────────────────────────────────────────────────────

    def _execute_entry(self, close_price: float, ts_utc: str, bar_ts: pd.Timestamp):
        """Place a market LONG (Buy) entry order."""
        if self.position is not None:
            log.warning(f"[{ts_utc}] Cannot enter — already in position")
            _db.log_event(ts_utc=ts_utc, level="INFO", event_type="ENTRY_SKIPPED",
                symbol=self.symbol,
                message="Entry signal fired but already in position — no pyramiding",
                detail={"reason": "ALREADY_IN_POSITION", "close": close_price,
                        "entry_price": self.position.entry_price if self.position else None})
            return
        if self.wallet < MIN_WALLET_USDT:
            log.warning(f"[{ts_utc}] Skipping entry — wallet {self.wallet:.4f} < {MIN_WALLET_USDT}")
            _db.log_event(ts_utc=ts_utc, level="WARNING", event_type="ENTRY_SKIPPED",
                symbol=self.symbol,
                message=f"Wallet {self.wallet:.4f} USDT below minimum {MIN_WALLET_USDT} USDT",
                detail={"reason": "INSUFFICIENT_WALLET", "wallet": self.wallet,
                        "min_wallet": MIN_WALLET_USDT, "close": close_price})
            return
        if not self.gate.try_acquire(self.symbol):
            log.warning(f"[{ts_utc}] Skipping entry — gate blocked")
            _db.log_event(ts_utc=ts_utc, level="WARNING", event_type="ENTRY_SKIPPED",
                symbol=self.symbol,
                message="Position gate blocked — another symbol holds the slot",
                detail={"reason": "GATE_BLOCKED", "close": close_price,
                        "wallet": self.wallet})
            return

        c = close_price
        wallet_before = self.wallet
        qty = 0.0
        try:
            max_notional = self.wallet * float(MAX_SYMBOL_FRACTION) * float(self.leverage)
            qty          = self._format_qty(max_notional / c)
            self._ensure_entry_risk_checks(qty, c, wallet_before)

            log_order(ts_utc=ts_utc, symbol=self.symbol, side="LONG",
                      qty=qty, price=c, order_type="ENTRY", status="PLACED",
                      reason="BAND_ENTRY")

            order_id = self.client.place_market_order(self.symbol, "Buy", qty, reduce_only=False)
            if self.last_signal:
                self.last_signal["placed"] = True
            self._refresh_state()
            summary = self.client.get_execution_summary(self.symbol, order_id)

            if summary is None:
                log_order(ts_utc=ts_utc, symbol=self.symbol, side="LONG",
                          qty=qty, price=c, order_type="ENTRY", status="FAILED",
                          order_id=order_id, error="No execution summary")
                _db.log_event(ts_utc=ts_utc, level="ERROR", event_type="ORDER_NO_SUMMARY",
                    symbol=self.symbol,
                    message=f"Entry order {order_id} placed but execution summary missing — gate released",
                    detail={"order_id": order_id, "side": "LONG", "qty": qty,
                            "price": c, "wallet_before": wallet_before})
                self.gate.release(self.symbol)
                return

            fill_price  = summary["avg_price"]
            filled_qty  = summary["qty"]
            # Store entry fee so exit PnL log can subtract both fees
            self._last_entry_fee = (filled_qty * fill_price) * float(self.taker_fee)
            if self.last_signal:
                self.last_signal["filled"] = True
                self.last_signal["price"]  = fill_price

            # Compute TP (LONG: price must rise). LIVE_TP_SCALE kept for compat.
            tp_price = self._format_price(fill_price * (1.0 + float(self.exit_params.tp_pct) * LIVE_TP_SCALE))

            # Record entry state and write all logs BEFORE the TP call.
            self._entry_time      = bar_ts
            self._entry_price     = fill_price
            self._wallet_at_entry = wallet_before

            log.info(
                f"{COLOR_SUBMITTED}[{ts_utc}] LONG ENTRY FILLED: "
                f"qty={filled_qty:.6f} fill={fill_price:.8f} "
                f"TP={tp_price} ({(tp_price/fill_price - 1.0)*100:.2f}%){COLOR_RESET}"
            )
            log_order(ts_utc=ts_utc, symbol=self.symbol, side="LONG",
                      qty=filled_qty, price=fill_price,
                      order_type="ENTRY", status="FILLED",
                      order_id=order_id, reason="BAND_ENTRY")

            self._log_real_trade(
                ts_utc=ts_utc, action="ENTRY", reason="BAND_ENTRY",
                side="LONG", qty=filled_qty, fill_price=fill_price,
                entry_price=fill_price, wallet_before=wallet_before,
                wallet_after=self.wallet,
            )

            # set_trading_stop is a no-op for spot (handled in bybit_client.py)
            try:
                self.client.set_trading_stop(self.symbol, tp_price)
            except Exception as tp_err:
                log.warning(f"[{ts_utc}] set_trading_stop skipped: {tp_err}")

        except Exception as e:
            log_order(ts_utc=ts_utc, symbol=self.symbol, side="LONG",
                      qty=qty, price=c, order_type="ENTRY", status="FAILED", error=str(e))
            self.gate.release(self.symbol)
            log.error(f"[{ts_utc}] Entry failed: {e}")
            _db.log_event(ts_utc=ts_utc, level="ERROR", event_type="ENTRY_FAILED",
                symbol=self.symbol,
                message=f"Live entry order failed: {e}",
                detail={"reason": str(e), "close": c, "qty_attempted": qty,
                        "wallet_before": wallet_before,
                        "ma_len": self.entry_params.ma_len,
                        "band_mult": self.entry_params.band_mult,
                        "tp_pct": self.exit_params.tp_pct})
            # Return here so we never fall through to the post-try position check.
            # The gate was already released above on the failure path.
            return

    # ── Exit execution ─────────────────────────────────────────────────────────

    def _execute_exit(self, close_price: float, reason: str, ts_utc: str):
        """Place a market Sell to close the LONG position."""
        if self.position is None:
            return
        pos          = self.position
        entry_price  = pos.entry_price
        qty_abs      = abs(pos.qty)
        wallet_before = self.wallet
        qty_to_close  = 0.0
        try:
            qty_to_close = self._format_qty(qty_abs)
            log_order(ts_utc=ts_utc, symbol=self.symbol, side="LONG",
                      qty=qty_to_close, price=close_price,
                      order_type="EXIT", status="PLACED", reason=reason)

            order_id = self.client.place_market_order(self.symbol, "Sell", qty_to_close, reduce_only=False)
            if self.last_signal:
                self.last_signal["placed"] = True
            self._refresh_state()
            summary = self.client.get_execution_summary(self.symbol, order_id)
            if summary is None:
                log_order(ts_utc=ts_utc, symbol=self.symbol, side="LONG",
                          qty=qty_to_close, price=close_price,
                          order_type="EXIT", status="FAILED",
                          order_id=order_id, error="No execution summary")
                log.error(f"[{ts_utc}] Exit order {order_id} has no summary")
                _db.log_event(ts_utc=ts_utc, level="ERROR", event_type="ORDER_NO_SUMMARY",
                    symbol=self.symbol,
                    message=f"Exit order {order_id} placed but execution summary missing",
                    detail={"order_id": order_id, "reason": reason,
                            "qty_to_close": qty_to_close, "close_price": close_price,
                            "entry_price": pos.entry_price})
                return

            fill_price = summary["avg_price"]
            filled_qty = summary["qty"]
            if self.last_signal:
                self.last_signal["filled"] = True
                self.last_signal["price"]  = fill_price
            log_order(ts_utc=ts_utc, symbol=self.symbol, side="LONG",
                      qty=filled_qty, price=fill_price,
                      order_type="EXIT", status="FILLED",
                      order_id=order_id, reason=reason)
            self._log_real_trade(
                ts_utc=ts_utc, action="EXIT", reason=reason,
                side="SELL", qty=filled_qty, fill_price=fill_price,
                entry_price=entry_price, wallet_before=wallet_before,
                wallet_after=self.wallet,
            )
            self._entry_time      = None   # clear entry state on exit
            self._entry_price     = None
            self._wallet_at_entry = None
            self._last_entry_fee  = 0.0
            self._time_tp_applied = False
        except Exception as e:
            log_order(ts_utc=ts_utc, symbol=self.symbol, side="LONG",
                      qty=qty_to_close if qty_to_close else qty_abs,
                      price=close_price, order_type="EXIT", status="FAILED", error=str(e))
            log.error(f"[{ts_utc}] Exit order failed: {e}")
            _db.log_event(ts_utc=ts_utc, level="ERROR", event_type="EXIT_FAILED",
                symbol=self.symbol,
                message=f"Live exit order failed: {e}",
                detail={"reason_str": str(e), "exit_reason": reason,
                        "close_price": close_price,
                        "entry_price": entry_price,
                        "qty_abs": qty_abs})
            try:
                self._refresh_state()
            except Exception:
                pass
        finally:
            # Release gate unconditionally so the slot is always freed after any
            # exit attempt.  _handle_external_close will resync position state on
            # the next candle if the REST query returns the position as still open.
            self.gate.release(self.symbol)

    # ── External close handler ─────────────────────────────────────────────────

    def _handle_external_close(self, ts_utc: str, current_close: float):
        """Called when the bot detects that a position was closed outside its own
        _execute_exit path — e.g. a server-side TP hit or a manual
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
                    side="SELL", qty=qty_abs, fill_price=exit_price,
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
        self._entry_time      = None
        self._entry_price     = None
        self._wallet_at_entry = None
        self._last_entry_fee  = 0.0
        self._time_tp_applied = False

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
        if side == "SELL":
            # EXIT: selling to close the long; profit = fill - entry
            pnl_gross = (fill_price - entry_price) * qty
        else:
            pnl_gross = 0.0
        fee_rate   = self.taker_fee
        fee        = (qty * fill_price) * float(fee_rate)
        # For exits subtract both the exit fee AND the stored entry fee.
        # For entries pnl_gross is 0 so pnl_net is just the negative entry fee.
        if side == "SELL":
            pnl_net = pnl_gross - fee - self._last_entry_fee
        else:
            pnl_net = pnl_gross - fee
        tp_price   = entry_price * (1.0 + float(self.exit_params.tp_pct) * LIVE_TP_SCALE)

        result = ""
        if action == "EXIT":
            self.trade_count      += 1
            win = pnl_net > 0
            self.win_count        += win
            self.realized_pnl_net += pnl_net
            result = "WIN" if win else "LOSS"

        pnl_1x   = float(pnl_net)  # no leverage for spot
        pnl_pct_val = float(pnl_net / wallet_before * 100.0) if wallet_before > 0 else 0.0
        _db.log_trade(
            ts_utc=ts_utc, mode="live", symbol=self.symbol, interval=self.interval,
            action=action, reason=reason, side=side,
            qty=float(qty), fill_price=float(fill_price),
            notional=float(qty * fill_price), fee=float(fee),
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
            ts_utc=ts_utc, symbol=self.symbol, event=f"LIVE_{action}",
            wallet_usdt=float(wallet_after),
            session_pnl_usdt=float(wallet_after - float(self.initial_wallet)),
            session_pnl_pct=float((wallet_after - float(self.initial_wallet)) / float(self.initial_wallet) * 100.0) if self.initial_wallet > 0 else 0.0,
        )

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
            sig_str = "** LONG ENTRY **"
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
            f"ADX={adx:.1f}(<{self.entry_params.adx_threshold:.0f})  "
            f"RSI={rsi:.1f}(<={self.entry_params.rsi_neutral_lo:.0f})  "
            f"TP={xp.tp_pct*100:.3f}%  |  "
            f"{sig_col}{sig_str}{COLOR_RESET}  |  "
            f"W={self.win_count} L={self.trade_count - self.win_count} WR={wr:.0f}%  "
            f"wallet=${self.wallet:.2f}  "
            f"{pnl_color}session={pnl_sign}${self.account_pnl_usdt:.2f} "
            f"({pnl_sign}{self.account_pnl_pct:.2f}%){COLOR_RESET}"
        )

        if self.position is not None:
            pos     = self.position
            qty_abs = abs(pos.qty)
            upnl    = (self.mark_price - pos.entry_price) * qty_abs
            notional = pos.entry_price * qty_abs
            upnl_pct = (upnl / notional * 100) if notional > 0 else 0.0
            tp_disp = pos.entry_price * (1.0 + float(xp.tp_pct) * LIVE_TP_SCALE)
            sign    = "+" if upnl >= 0 else ""
            days_held = ""
            if self._entry_time is not None:
                try:
                    dh = (pd.Timestamp.now(tz="UTC").tz_convert(None) - self._entry_time.tz_convert(None)).total_seconds() / 86400.0
                    days_held = f"  held={dh:.2f}d"
                except Exception:
                    pass
            log.info(
                f"  LONG entry=${pos.entry_price:.5f}  tp=${tp_disp:.5f}  "
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
        """Background thread: scan ALL SYMBOLS × CANDLE_INTERVALS and switch
        to whichever (symbol, interval) pair scores best.

        Symbol switching is allowed only when flat (no open position) so we never
        abandon a live trade mid-flight.  Interval-only switches can always happen.
        """
        log.info(f"[REOPT] starting multi-symbol optimisation scan ({SYMBOLS})...")
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

            for sym in SYMBOLS:
                # Fetch risk tiers + instrument info for every candidate symbol
                try:
                    risk_df = fetch_risk_tiers(sym)
                    inst    = get_instrument_info(sym)
                except Exception as e:
                    log.warning(f"[REOPT] {sym}: fetch_risk_tiers/instrument failed — skipping: {e}")
                    continue

                for iv in supported_intervals(CANDLE_INTERVALS):
                    try:
                        df_last, df_mark = download_seed_history(sym, DAYS_BACK_SEED, iv)

                        # Warm-start when re-testing the current live pair
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
                            }  # noqa: E501

                        opt = optimise_params(
                            df_last=df_last, df_mark=df_mark,
                            risk_df=risk_df,
                            trials=INIT_TRIALS,
                            lookback_candles=min(len(df_last), len(df_mark)),
                            event_name=f"REOPT_{sym}_{iv}m",
                            fee_rate=taker_fee_for(sym),
                            maker_fee_rate=maker_fee_for(sym),
                            interval_minutes=interval_minutes(iv),
                            saved_best=saved_best,
                            db_symbol=sym, db_interval=iv, db_trigger="REOPT",
                        )

                        br = opt["best_result"]
                        pf = br.pnl_pct / (1.0 + max(br.max_drawdown_pct, 0.001))
                        log.info(
                            f"[REOPT] {sym} {iv}m  score={pf:.4f}  "
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
                        log.warning(f"[REOPT] {sym} {iv}m: skipped: {e}")

            if best_entry is None:
                log.warning("[REOPT] no valid combos found — keeping current params")
                return

            # ── Monte Carlo on the global winner ──────────────────────────────
            records      = getattr(best_br, "trade_records", []) or []
            new_mc_score = float("-inf")
            mc_res       = None
            if len(records) >= MC_MIN_TRADES:
                mc_res = run_monte_carlo(records, float(STARTING_WALLET), n_sims=MC_SIMS)
                if mc_res:
                    new_mc_score = mc_score(mc_res)
                    print_monte_carlo_report(
                        mc_res, float(STARTING_WALLET),
                        len(records), best_sym, best_iv,
                    )

            log.info(
                f"[REOPT] winner: {best_sym} {best_iv}m  MC={new_mc_score:.4f}  "
                f"(current: {self.symbol} {self.interval}m)"
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
                _switched_sym = best_sym != self.symbol
                _switched_iv  = best_iv  != self.interval

                # Symbol switch requires being flat — can't abandon a live position
                if _switched_sym and self.position is not None:
                    log.info(
                        f"[REOPT] ★ best symbol is {best_sym} but position open on "
                        f"{self.symbol} — symbol switch deferred until flat"
                    )
                    _switched_sym = False
                    best_sym = self.symbol

                if _switched_sym or _switched_iv:
                    log.info(
                        f"[REOPT] ★ switching {self.symbol} {self.interval}m "
                        f"→ {best_sym} {best_iv}m  (score {best_score:.4f})"
                    )

                if _switched_sym:
                    self.symbol      = best_sym
                    self.risk_df     = best_risk_df
                    self.instrument  = best_inst
                    self.taker_fee   = taker_fee_for(best_sym)
                    self.maker_fee   = maker_fee_for(best_sym)
                    # Update the traders dict so WS routing follows the new symbol
                    if self._traders_ref is not None:
                        old_keys = [k for k, v in self._traders_ref.items() if v is self]
                        for k in old_keys:
                            del self._traders_ref[k]
                        self._traders_ref[best_sym] = self

                if _switched_sym or _switched_iv:
                    self.interval            = best_iv
                    self.df                  = best_df_last[
                        ["ts", "open", "high", "low", "close", "volume"]
                    ].copy().reset_index(drop=True)
                    self.closed_candle_count = 0
                    self._shadow_positions   = []

                self.entry_params = best_entry
                self.exit_params  = best_exit_p
                self.leverage     = best_exit_p.leverage
                try:
                    self.client.ensure_futures_setup(self.symbol, leverage=float(self.leverage))
                except Exception as _lev_err2:
                    log.warning(f"[REOPT] Could not update leverage for {self.symbol}: {_lev_err2}")
                self._recompute_indicators()
                # Seed analytics for full historical DataFrame so chart shows bands immediately
                try:
                    n_ana = _db.bulk_log_seed_analytics(
                        df=self.df, symbol=self.symbol, interval=self.interval,
                        ma_len=self.entry_params.ma_len, band_mult=self.entry_params.band_mult,
                        exit_ma_len=self.exit_params.exit_ma_len,
                        exit_band_mult=float(self.exit_params.exit_band_mult),
                        sl_pct=float(self.exit_params.sl_pct),
                    )
                    log.info(f"[LIVE] Seeded {n_ana} candle_analytics rows → DB")
                except Exception as _ana_err:
                    log.warning(f"[LIVE] bulk_log_seed_analytics failed: {_ana_err}")
                # Write backtest trades to DB so chart can display them as markers
                try:
                    _db.bulk_log_backtest_trades(
                        trade_records=getattr(best_br, "trade_records", []) or [],
                        symbol=self.symbol, interval=self.interval,
                        entry_params=self.entry_params, exit_params=self.exit_params,
                    )
                except Exception as _bt_err:
                    log.warning(f"[LIVE] bulk_log_backtest_trades failed: {_bt_err}")
                log.info(
                    f"[REOPT] params updated  {self.symbol} {self.interval}m  "
                    f"MC={new_mc_score:.4f}"
                )
            else:
                # Still seed analytics with current params so chart is populated
                try:
                    _db.bulk_log_seed_analytics(
                        df=self.df, symbol=self.symbol, interval=self.interval,
                        ma_len=self.entry_params.ma_len, band_mult=self.entry_params.band_mult,
                        exit_ma_len=self.exit_params.exit_ma_len,
                        exit_band_mult=float(self.exit_params.exit_band_mult),
                        sl_pct=float(self.exit_params.sl_pct),
                    )
                except Exception:
                    pass
                log.info(
                    f"[REOPT] MC score {new_mc_score:.4f} <= 0 — keeping current params"
                )

        except Exception as e:
            log.error(f"[REOPT] failed: {e}", exc_info=True)
        finally:
            self.last_reopt_time = time.time()
            self._reopt_running  = False

    # ── Main candle callback ───────────────────────────────────────────────────

    def on_closed_candle(self, candle: Dict[str, Any]):
        """Called once per confirmed closed candle from the WebSocket.

        Processing order:
          1. Append candle to DataFrame, rebuild indicators
          2. Refresh wallet/position state from Bybit REST
          3. Skip if not enough candles for warm-up
          4. Detect externally-closed position (server TP or manual close)
          5. Liquidation (high >= entry * (lev+1) / (lev * (1+MMR)))      [priority 1]
          6. Take-profit (low  <= entry * (1 - tp_pct))                   [priority 2]
          7. Stop-loss   (high >= entry * (1 + sl_pct))                   [priority 3]
          8. Band exit   (low drops below discount_k band)                [priority 4]
          9. Check entry signal (band crossover AND ADX gate AND RSI gate)
         10. Log candle-close summary
        """
        try:
            ts      = pd.to_datetime(int(candle["start"]), unit="ms", utc=True)
            ts_utc  = ts.strftime("%Y-%m-%d %H:%M:%S")
            get_status_monitor().on_candle_received(self.symbol)

            ts_ms = int(candle["start"])
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

            # ── Max-loss guard (4-hour halt) ──────────────────────────────────────
            if self._halted:
                if time.time() - (self._halt_ts or 0) >= 4 * 3600:
                    self._halted  = False
                    self._halt_ts = None
                    _ts_hl = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M:%S")
                    log.info(f"[{self.symbol}] Max-loss halt expired — resuming entries")
                    _db.log_event(ts_utc=_ts_hl, level="INFO", event_type="MAX_LOSS_HALT_EXPIRED",
                        symbol=self.symbol, message="4-hour max-loss halt expired — entries re-enabled",
                        detail={})

            _max_loss_pct = MAX_LOSS_PCT
            if _max_loss_pct is not None and not self._halted:
                _session_pnl_pct = self.account_pnl_pct   # already computed each candle
                if _session_pnl_pct <= -abs(_max_loss_pct):
                    self._halted  = True
                    self._halt_ts = time.time()
                    _ts_hl = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M:%S")
                    log.warning(
                        f"[{self.symbol}] MAX-LOSS HALT: session PnL={_session_pnl_pct:.2f}% "
                        f"<= -{abs(_max_loss_pct):.1f}%  — halting entries for 4 hours"
                    )
                    _db.log_event(ts_utc=_ts_hl, level="WARNING", event_type="MAX_LOSS_HALT",
                        symbol=self.symbol,
                        message=(
                            f"Max-loss halt triggered: session PnL={_session_pnl_pct:.2f}% "
                            f"<= -{abs(_max_loss_pct):.1f}%"
                        ),
                        detail={"session_pnl_pct": _session_pnl_pct,
                                "max_loss_pct": _max_loss_pct})
                    # Exit any open position immediately
                    if self.position is not None:
                        _ts_ex = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M:%S")
                        log.warning(f"[{self.symbol}] Max-loss: closing open position now")
                        self._execute_exit(close_price=self.position.mark_price or self.mark_price,
                                           reason="MAX_LOSS", ts_utc=_ts_ex)

            # ── DB: raw candle ──
            _db.log_candle(
                ts_utc=ts_utc, ts_ms=ts_ms, symbol=self.symbol,
                interval=self.interval, price_type="last",
                o=o, h=h, l=l, c=c, vol=vol,
            )

            # acted: set True after any exit or external-close this candle so we never
            # place an entry on the same candle we just closed a position.
            acted = False

            # ── Detect externally-closed position (server TP or manual close) ──
            # _entry_price is set whenever we are tracking an open position (whether entered
            # this session or detected at startup).  If position is now gone but we were
            # tracking one, the close happened outside _execute_exit — handle it properly.
            if self.position is None and self._entry_price is not None:
                self._handle_external_close(ts_utc, c)
                acted = True

            # ── Warm-up guard ──
            min_len = max(self.entry_params.ma_len, self.exit_params.exit_ma_len) + 20
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

            _raw_long = compute_entry_signals_raw(
                current_row=row, prev_row=prev,
                current_low=l,
            )
            # ── Signal drought tracking ──────────────────────────────────────────
            if _raw_long > 0:
                self._last_signal_ts = time.time()

            _final_long = resolve_entry_signals(
                _raw_long, adx_val, rsi_val,
                adx_threshold=self.entry_params.adx_threshold,
                rsi_neutral_lo=self.entry_params.rsi_neutral_lo,
            )
            entry_sig = _final_long > 0
            _raw_exit = compute_exit_signals_raw(
                current_row=row, prev_row=prev,
                current_high=h,
            )
            exit_sig = _raw_exit > 0

            # ── Drought event (log once per SIGNAL_DROUGHT_HOURS window) ─────────
            if SIGNAL_DROUGHT_HOURS and SIGNAL_DROUGHT_HOURS > 0:
                _drought_sec = time.time() - self._last_signal_ts
                if _drought_sec >= SIGNAL_DROUGHT_HOURS * 3600:
                    _ts_d = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M:%S")
                    _db.log_event(
                        ts_utc=_ts_d, level="WARNING", event_type="SIGNAL_DROUGHT",
                        symbol=self.symbol,
                        message=(
                            f"No raw entry signal for {_drought_sec/3600:.1f}h "
                            f"(threshold {SIGNAL_DROUGHT_HOURS:.0f}h)"
                        ),
                        detail={"drought_hours": round(_drought_sec / 3600, 2),
                                "threshold_hours": SIGNAL_DROUGHT_HOURS},
                    )

            # ── Shadow position tracking — check virtual "what if" trades ─────────
            if self._shadow_positions:
                _sh_to_remove = []
                for _sh in self._shadow_positions:
                    _sh["candles"] += 1
                    _sh_outcome = None
                    _sh_out_px  = None
                    if h >= _sh["tp_price"]:
                        _sh_outcome = "TP_HIT";   _sh_out_px = _sh["tp_price"]
                    elif l <= _sh["sl_price"]:
                        _sh_outcome = "SL_HIT";   _sh_out_px = _sh["sl_price"]
                    elif _sh["candles"] >= 100:
                        _sh_outcome = "EXPIRED"
                    if _sh_outcome:
                        if _sh_outcome != "EXPIRED":
                            _sh_pnl = ((_sh_out_px - _sh["entry_price"])
                                       / _sh["entry_price"] * 100.0)
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
                                    f"[{self.symbol}] 🔍 Skipped trade would have been profitable  "
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
                self._entry_price * (1.0 - float(self.exit_params.sl_pct))
                if self.position is not None and self._entry_price is not None else None
            )

            if entry_sig:
                _sig_type = "ENTRY"
                _blocked  = None
            elif _raw_exit > 0:
                _sig_type = "EXIT_BAND"
                _blocked  = None
            elif _sl_price_lvl is not None and l <= _sl_price_lvl:
                _sig_type = "EXIT_SL"
                _blocked  = None
            else:
                _sig_type = "NONE"
                _blocked  = None
                if _raw_long > 0 and _final_long == 0:
                    _blocked = "ADX" if adx_val >= self.entry_params.adx_threshold else "RSI"
                    # ── Shadow for indicator-blocked signal ───────────────────────
                    if self.position is None and len(self._shadow_positions) < 5:
                        _sh_ep = c
                        _sh_tp = _sh_ep * (1.0 + float(self.exit_params.tp_pct) * LIVE_TP_SCALE)
                        _sh_sl = _sh_ep * (1.0 - float(self.exit_params.sl_pct))
                        self._shadow_positions.append({
                            "entry_ts": ts_utc, "entry_price": _sh_ep,
                            "tp_price": _sh_tp, "sl_price": _sh_sl,
                            "band": _raw_long, "blocked_by": _blocked,
                            "candles": 0, "adx_at_entry": adx_val,
                            "rsi_at_entry": rsi_val,
                        })

            _db.log_signal(
                ts_utc=ts_utc, symbol=self.symbol, interval=self.interval,
                signal_type=_sig_type,
                raw_band_level=_raw_long, final_band_level=_final_long,
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
                _upnl     = (self.mark_price - self.position.entry_price) * _qty_abs
                _ts_entry = self._entry_time.strftime("%Y-%m-%d %H:%M:%S") if self._entry_time else None
                _tp_snap  = self._entry_price * (1.0 + float(self.exit_params.tp_pct) * _C.LIVE_TP_SCALE) if self._entry_price else None
                _db.log_position(
                    ts_utc=ts_utc, symbol=self.symbol,
                    qty=_qty_abs,
                    entry_price=self.position.entry_price,
                    entry_time=_ts_entry,
                    mark_price=self.mark_price,
                    liquidation_price=None,
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
                            self.position.entry_price * (1.0 + _dyn_tp_pct)
                        )
                        self.client.set_trading_stop(self.symbol, new_tp)
                        self._time_tp_applied = True
                        log.info(
                            f"{COLOR_EXIT}[{ts_utc}] {TIME_TP_HOURS:.0f}h elapsed — "
                            f"server TP tightened to {new_tp:.8f} ({_dyn_tp_pct*100:.3f}% from entry){COLOR_RESET}"
                        )
                        _db.log_event(
                            ts_utc=ts_utc, level="INFO", event_type="TIME_TP_APPLIED",
                            symbol=self.symbol,
                            message=(
                                f"{TIME_TP_HOURS:.0f}h time TP fired — server target "
                                f"{new_tp:.8f} ({_dyn_tp_pct*100:.3f}%)"
                            ),
                            detail={"entry_price": self.position.entry_price, "new_tp": new_tp,
                                    "elapsed_hours": round(elapsed_sec / 3600, 2),
                                    "time_tp_pct": _dyn_tp_pct,
                                    "is_fallback": _dyn_tp_pct == TIME_TP_FALLBACK_PCT},
                        )
                except Exception as _ttp_err:
                    log.warning(f"[{ts_utc}] Time-based TP update failed: {_ttp_err}")

            # ── Take-profit check (LONG: high >= tp_price, client-side for spot) ──
            if not acted and self.position is not None and self._entry_price is not None:
                tp_price_lvl = self._entry_price * (1.0 + float(self.exit_params.tp_pct) * LIVE_TP_SCALE)
                if h >= tp_price_lvl:
                    self._execute_exit(tp_price_lvl, "TP", ts_utc)
                    acted = True

            # ── Hard stop-loss signal (LONG: low falls below sl_price) ──
            sl_exit = False
            if self.position is not None and self._entry_price is not None:
                sl_price = self._entry_price * (1.0 - float(self.exit_params.sl_pct))
                if l <= sl_price:
                    sl_exit = True

            if entry_sig:
                self.last_signal = {"type": "ENTRY", "time": ts_utc, "placed": False, "filled": False, "price": None, "band": _raw_long}
            elif exit_sig or sl_exit:
                self.last_signal = {"type": "EXIT",  "time": ts_utc, "placed": False, "filled": False, "price": None, "band": _raw_long}

            # ── Stop-loss exit ──  (priority 3, matches backtester)
            if not acted and sl_exit and self.position is not None:
                self._execute_exit(c, "STOP_LOSS", ts_utc)
                acted = True

            # ── Band exit ──  (priority 4, only if no other action this candle)
            if not acted and exit_sig and self.position is not None:
                self._execute_exit(c, "BAND_EXIT", ts_utc)
                acted = True

            # ── Entry logic ──  (never on the same candle as an exit or external close)
            if not acted and entry_sig and self.position is None and not self._halted:
                if self.wallet >= MIN_WALLET_USDT:
                    self._execute_entry(c, ts_utc, ts)
                    # ── Same-bar exit — "Recalculate: After order is filled" (TradingView) ──
                    # Re-check TP, SL, and band exit on the entry bar.
                    if self.position is not None and self._entry_price is not None:
                        _sb_tp = self._entry_price * (1.0 + float(self.exit_params.tp_pct) * LIVE_TP_SCALE)
                        _sl_sb = self._entry_price * (1.0 - float(self.exit_params.sl_pct))
                        if h >= _sb_tp:
                            self._execute_exit(_sb_tp, "TP", ts_utc)
                        elif l <= _sl_sb:
                            self._execute_exit(c, "STOP_LOSS", ts_utc)
                        elif exit_sig:
                            self._execute_exit(c, "BAND_EXIT", ts_utc)
                else:
                    log.warning(f"[{ts_utc}] Entry signal — wallet {self.wallet:.4f} < {MIN_WALLET_USDT}")
                    # ── Shadow for wallet-blocked signal ─────────────────────────
                    if len(self._shadow_positions) < 5:
                        _sh_ep = c
                        _sh_tp = _sh_ep * (1.0 + float(self.exit_params.tp_pct) * LIVE_TP_SCALE)
                        _sh_sl = _sh_ep * (1.0 - float(self.exit_params.sl_pct))
                        self._shadow_positions.append({
                            "entry_ts": ts_utc, "entry_price": _sh_ep,
                            "tp_price": _sh_tp, "sl_price": _sh_sl,
                            "band": _raw_long, "blocked_by": "WALLET",
                            "candles": 0, "adx_at_entry": adx_val,
                            "rsi_at_entry": rsi_val,
                        })
            elif entry_sig and self.position is not None:
                log.info(f"[{ts_utc}] Entry signal — already LONG (no pyramiding)")
                # ── Shadow for position-blocked signal ───────────────────────────
                if len(self._shadow_positions) < 5:
                    _sh_ep = c
                    _sh_tp = _sh_ep * (1.0 + float(self.exit_params.tp_pct) * LIVE_TP_SCALE)
                    _sh_sl = _sh_ep * (1.0 - float(self.exit_params.sl_pct))
                    self._shadow_positions.append({
                        "entry_ts": ts_utc, "entry_price": _sh_ep,
                        "tp_price": _sh_tp, "sl_price": _sh_sl,
                        "band": _raw_long, "blocked_by": "POSITION",
                        "candles": 0, "adx_at_entry": adx_val,
                        "rsi_at_entry": rsi_val,
                    })

            # ── Display ──
            self._display_candle_close(
                ts_utc=ts_utc, o=o, h=h, l=l, c=c,
                entry_sig=entry_sig, exit_sig=exit_sig,
                adx=adx_val, rsi=rsi_val,
            )
        except Exception as _occ_err:
            _ts_err = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M:%S")
            log.error(f"[{self.symbol}] on_closed_candle unhandled exception: {_occ_err}", exc_info=True)
            _db.log_event(
                ts_utc=_ts_err, level="ERROR", event_type="CANDLE_CALLBACK_ERROR",
                symbol=self.symbol,
                message=f"on_closed_candle crashed: {_occ_err}",
                detail={"error": str(_occ_err)},
            )


# ─── WebSocket loop ────────────────────────────────────────────────────────────

def start_live_ws(
    traders: Dict[str, Any],
    stop_event: threading.Event = None,
    all_symbols: list = None,
    all_intervals: list = None,
):
    """Start the Bybit public WebSocket for kline (candle) and ticker (mark price) data.

    Subscribes to all (symbol, interval) combinations upfront so that symbol/interval
    switches during re-optimisation are picked up without reconnecting.

    Reconnects automatically on disconnect.
    Press Ctrl+C to stop, or set stop_event (threading.Event) to stop programmatically.
    """
    from ..utils.constants import CATEGORY as _CATEGORY
    ws_url  = f"wss://stream.bybit.com/v5/public/{'spot' if _CATEGORY == 'spot' else 'linear'}"

    # Subscribe to every configured (interval, symbol) pair so re-opt can switch
    # interval dynamically without needing a WS reconnect.
    _k_syms = list(all_symbols)  if all_symbols  else list(traders.keys())
    _k_ivs  = list(all_intervals) if all_intervals else list({t.interval for t in traders.values()})
    _t_syms = list(all_symbols)  if all_symbols  else list(traders.keys())

    topic_k     = sorted({f"kline.{iv}.{sym}" for sym in _k_syms for iv in _k_ivs})
    topic_t     = sorted({f"tickers.{sym}" for sym in _t_syms})
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
        _ts = pd.Timestamp.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        _db.log_event(ts_utc=_ts, level="INFO", event_type="WS_CONNECTED",
                      message=f"WebSocket connected. Topics: {', '.join(topic_k + topic_t)}")

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
                # For spot: use lastPrice; for linear: use markPrice
                mp = d.get("markPrice") or d.get("lastPrice")
                if mp is not None:
                    ts_utc = pd.Timestamp.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                    trader.on_mark_price_update(float(mp), ts_utc)
            return

        if topic.startswith("kline.") and data:
            # Route only to the trader's *current* interval — survives interval switches
            # at re-opt time without a WS reconnect.
            _iv_key = topic.split(".")[1]
            if _iv_key == trader.interval:
                for c in data:
                    if c.get("confirm") is True:
                        trader.on_closed_candle(c)

    def on_error(ws, error):
        log.error(f"WebSocket error: {error}")
        _ts = pd.Timestamp.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        _db.log_event(ts_utc=_ts, level="ERROR", event_type="WS_ERROR",
                      message=str(error))

    def on_close(ws, code, msg):
        _ws_ref["ws"] = None
        log.warning(f"WebSocket closed: {code} {msg}")
        _ts = pd.Timestamp.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        _db.log_event(ts_utc=_ts, level="WARNING", event_type="WS_DISCONNECTED",
                      message=f"WebSocket closed: code={code} msg={msg}")

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

    _ws_attempt = 0
    try:
        while True:
            if stop_event and stop_event.is_set():
                break
            try:
                run_one()  # WebSocketApp.run_forever() — blocks until close
                _ws_attempt = 0  # reset backoff on clean exit
            except Exception as e:
                log.error(f"WS crashed: {e}")
            if stop_event and stop_event.is_set():
                break
            _backoff = min(5 * (2 ** _ws_attempt), 60)
            _ws_attempt += 1
            log.warning(f"WebSocket disconnected. Reconnecting in {_backoff}s (attempt {_ws_attempt})...")
            # Use stop_event.wait() instead of time.sleep() so a Stop request
            # during the reconnect backoff takes effect immediately (returns True
            # when the event is set, False when the timeout expires normally).
            if stop_event:
                if stop_event.wait(timeout=_backoff):
                    break
            else:
                time.sleep(_backoff)
    finally:
        _ping_stop["stop"] = True
        status_monitor.stop()


# ─── Seed history download ─────────────────────────────────────────────────────

def download_seed_history(symbol: str, days_back: int, interval: str):
    """Download last-price and mark-price kline history for seeding the live trader."""
    import engine.utils.db_logger as _db
    end_ts   = now_ms()
    start_ts = end_ts - int(days_back * 24 * 60 * 60 * 1000)

    log.info(f"Downloading LAST-price {interval}m history for {symbol} ({days_back} days)...")
    df_last = fetch_last_klines(symbol, interval, start_ts, end_ts)
    log.info(f"Last-price candles: {len(df_last)}")

    log.info(f"Downloading MARK-price {interval}m history for {symbol} ({days_back} days)...")
    df_mark = fetch_mark_klines(symbol, interval, start_ts, end_ts)
    log.info(f"Mark-price candles: {len(df_mark)}")

    # Persist seed candles to DB so the chart has data from startup
    n_last = _db.bulk_log_seed_candles(df_last, symbol, interval, "last")
    n_mark = _db.bulk_log_seed_candles(df_mark, symbol, interval, "mark")
    log.info(f"Seeded {n_last} last-price + {n_mark} mark-price candles → DB")

    return df_last, df_mark
