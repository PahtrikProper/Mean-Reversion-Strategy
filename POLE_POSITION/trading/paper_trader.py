"""Paper Trader — simulated trading with no API keys required.

Uses public REST endpoints (klines, risk tiers, instrument info) and the
public Bybit WebSocket for live candle and mark-price data.

Simulates wallet, fills (close + slippage), TP, trail stop,
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
    COLOR_ENTRY,
    COLOR_EXIT,
    COLOR_ERROR,
    COLOR_RESET,
    COLOR_SUBMITTED,
    TRADES_CSV_PATH,
)
from ..utils.data_structures import RealPosition, EntryParams, ExitParams, MC_MIN_TRADES, MC_SIMS
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
from ..core.orders import apply_slippage
from ..trading.bybit_client import fetch_last_klines, fetch_mark_klines
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
        self.leverage     = leverage_for(symbol)
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

        self._entry_time: Optional[pd.Timestamp]  = None
        self._entry_price: Optional[float]        = None
        self._min_low_since_entry: Optional[float] = None

        self.trade_count      = 0
        self.win_count        = 0
        self.realized_pnl_net = 0.0
        self.account_pnl_usdt = 0.0
        self.account_pnl_pct  = 0.0
        self.last_signal: Optional[dict] = None

        if len(df_mark_seed) == 0:
            raise RuntimeError("No mark seed data")
        self.mark_price = float(df_mark_seed["close"].iloc[-1])

        self._recompute_indicators()

    # ── Indicator rebuild ──────────────────────────────────────────────────────

    def _recompute_indicators(self):
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
            return
        if self.wallet < MIN_WALLET_USDT:
            log.warning(f"[PAPER][{ts_utc}] Skipping entry — wallet {self.wallet:.4f} < {MIN_WALLET_USDT}")
            return
        if not self.gate.try_acquire(self.symbol):
            log.warning(f"[PAPER][{ts_utc}] Skipping entry — gate blocked")
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
        finally:
            # Always clear position state and release gate
            self.position              = None
            self._paper_margin         = 0.0
            self._paper_tp_price       = None
            self._paper_liq_price      = None
            self._paper_entry_fee      = 0.0
            self._entry_time           = None
            self._entry_price          = None
            self._min_low_since_entry  = None
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
        csv_append(TRADES_CSV_PATH, [
            ts_utc, f"[PAPER]{self.symbol}", action, reason, f"{side}_SIGNAL",
            side, float(qty), float(fill_price),
            float(qty * fill_price), float(fee_logged),
            float(entry_price), float(tp_price),
            float(self.mark_price),
            float(wallet_before), float(wallet_after),
            float(pnl_gross), float(pnl_net),
            float(pnl_net / float(self.leverage)) if self.leverage else 0.0,
            float(pnl_net / float(self.initial_wallet) * 100.0) if self.initial_wallet > 0 else 0.0,
            result,
            self.entry_params.ma_len,
            float(self.entry_params.band_mult),
            float(self.exit_params.tp_pct),
        ])

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
            f"ADX={adx:.1f}(<{ADX_THRESHOLD:.0f})  RSI={rsi:.1f}(>={RSI_NEUTRAL_LO:.0f})  "
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
        """Background thread: re-run param search using public API data."""
        log.info(f"[PAPER][REOPT] {self.symbol}: re-running optimisation...")
        try:
            df_last, df_mark = _download_seed(self.symbol, DAYS_BACK_SEED, self.interval)
            lev       = leverage_for(self.symbol)
            fee       = taker_fee_for(self.symbol)
            maker_fee = maker_fee_for(self.symbol)

            saved_best = {
                "ma_len":    self.entry_params.ma_len,
                "band_mult": self.entry_params.band_mult,
                "tp_pct":    self.exit_params.tp_pct,
            }

            opt = optimise_params(
                df_last=df_last, df_mark=df_mark,
                risk_df=self.risk_df,
                trials=INIT_TRIALS,
                lookback_candles=min(len(df_last), len(df_mark)),
                event_name=f"PAPER_REOPT_{self.symbol}_{self.interval}m",
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
                mc_res = run_monte_carlo(records, float(PAPER_STARTING_BALANCE), n_sims=MC_SIMS)
                if mc_res:
                    new_mc_score = mc_score(mc_res)
                    print_monte_carlo_report(
                        mc_res, float(PAPER_STARTING_BALANCE),
                        len(records), self.symbol, self.interval
                    )

            log.info(
                f"[PAPER][REOPT] {self.symbol}: MC score={new_mc_score:.4f} | "
                f"MA-len={new_entry.ma_len}  BandMult={new_entry.band_mult:.2f}%  "
                f"TP={new_exit.tp_pct*100:.2f}%"
            )

            if new_mc_score > 0:
                self.entry_params = new_entry
                self.exit_params  = new_exit
                log.info(f"[PAPER][REOPT] {self.symbol}: params updated (MC score={new_mc_score:.4f})")
            else:
                log.info(f"[PAPER][REOPT] {self.symbol}: MC score {new_mc_score:.4f} <= 0 — keeping old params")

        except Exception as e:
            log.error(f"[PAPER][REOPT] {self.symbol}: failed: {e}")
        finally:
            self.last_reopt_time = time.time()
            self._reopt_running = False

    # ── Main candle callback ───────────────────────────────────────────────────

    def on_closed_candle(self, candle: Dict[str, Any]):
        """Called once per confirmed closed candle from the WebSocket.

        Exit priority (identical to backtester and LiveRealTrader):
          1. Liquidation   — mark_price >= liq_price
          2. Take-Profit   — candle low <= tp_price
          3. Trail Stop    — high >= min_low_since_entry + mult×ATR
          4. Band Exit     — low drops below discount band
        """
        ts     = pd.to_datetime(int(candle["start"]), unit="ms", utc=True)
        ts_utc = ts.strftime("%Y-%m-%d %H:%M:%S")
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

        # Refresh liquidation price with latest mark price
        if self.position is not None:
            self._refresh_liq_price()

        self._update_account_pnl()
        acted = False

        # ── Warm-up guard ──
        min_len = self.entry_params.ma_len + 20
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

        _raw_short = compute_entry_signals_raw(
            current_row=row, prev_row=prev,
            current_high=h, current_low=l,
        )
        entry_sig = resolve_entry_signals(_raw_short, adx_val, rsi_val) > 0
        exit_sig  = compute_exit_signals_raw(
            current_row=row, prev_row=prev,
            current_low=l, current_high=h,
        ) > 0

        # ── Update trail stop tracking ──
        if self.position is not None and self._min_low_since_entry is not None:
            self._min_low_since_entry = min(self._min_low_since_entry, l)

        # ── Priority 1: Liquidation ──
        if self.position is not None and self._paper_liq_price is not None:
            if self.mark_price >= self._paper_liq_price:
                qty_abs = abs(self.position.qty)
                log.info(
                    f"{COLOR_ERROR}[PAPER][{ts_utc}] LIQUIDATED  "
                    f"mark={self.mark_price:.5f} >= liq={self._paper_liq_price:.5f}{COLOR_RESET}"
                )
                self.trade_count += 1  # liquidation counts as a loss
                # Entire margin is forfeited — wallet stays as post-entry value
                self.position             = None
                self._paper_margin        = 0.0
                self._paper_tp_price      = None
                self._paper_liq_price     = None
                self._paper_entry_fee     = 0.0
                self._entry_time          = None
                self._entry_price         = None
                self._min_low_since_entry = None
                self.gate.release(self.symbol)
                self._update_account_pnl()
                acted = True

        # ── Priority 2: Take-Profit (in-candle low check) ──
        if not acted and self.position is not None and self._paper_tp_price is not None:
            if l <= self._paper_tp_price:
                self._execute_exit(self._paper_tp_price, "TP", ts_utc)
                acted = True

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
        elif exit_sig or trail_stop_exit:
            self.last_signal = {"type": "EXIT",  "time": ts_utc, "placed": False, "filled": False, "price": None}

        # ── Priority 3: Trail stop exit ──
        if not acted and trail_stop_exit and self.position is not None:
            self._execute_exit(c, "TRAIL_STOP", ts_utc)
            acted = True

        # ── Priority 4: Band exit ──
        if not acted and exit_sig and self.position is not None:
            self._execute_exit(c, "BAND_EXIT", ts_utc)
            acted = True

        # ── Entry ──
        if not acted and entry_sig and self.position is None:
            if self.wallet >= MIN_WALLET_USDT:
                self._execute_entry(c, ts_utc, ts)
                if self.position is not None and self._min_low_since_entry is None:
                    self._min_low_since_entry = l
            else:
                log.warning(f"[PAPER][{ts_utc}] Entry signal — wallet {self.wallet:.4f} < {MIN_WALLET_USDT}")
        elif entry_sig and self.position is not None:
            log.info(f"[PAPER][{ts_utc}] Entry signal — already SHORT (no pyramiding)")

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
