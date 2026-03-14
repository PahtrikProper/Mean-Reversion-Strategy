"""Real-time trading status display — Mean Reversion Strategy"""

import sys
import threading
import time
import logging
from typing import Dict, Any, Optional
from datetime import datetime
from .constants import LIVE_TP_SCALE, REOPT_INTERVAL_SEC, SIGNAL_DROUGHT_HOURS, MAX_LOSS_PCT

log = logging.getLogger("trading_status")


class TradingStatusMonitor:
    def __init__(self):
        self.traders_ref: Dict[str, Any] = {}
        self.candle_counts: Dict[str, int] = {}
        self.last_balance_update: float = 0.0
        self.balance_update_interval: float = 180.0
        self.running = False
        self.lock = threading.Lock()
        self._display_thread: Optional[threading.Thread] = None

    def start(self, traders: Dict[str, Any], update_interval: float = 180.0):
        with self.lock:
            self.traders_ref = traders
            self.balance_update_interval = update_interval
            self.last_balance_update = time.time()
            for symbol in traders:
                self.candle_counts[symbol] = 0
            self.running = True
            self._display_thread = threading.Thread(
                target=self._monitor_loop, daemon=True, name="trading-status-monitor"
            )
            self._display_thread.start()
            log.info("Trading status monitor started")

    def stop(self):
        with self.lock:
            self.running = False
        if self._display_thread:
            self._display_thread.join(timeout=5)
        log.info("Trading status monitor stopped")

    def on_candle_received(self, symbol: str):
        with self.lock:
            if symbol in self.candle_counts:
                self.candle_counts[symbol] += 1

    def _monitor_loop(self):
        while self.running:
            try:
                now = time.time()
                if now - self.last_balance_update >= self.balance_update_interval:
                    self._display_full_status()
                    self.last_balance_update = now
                else:
                    self._display_quick_status()
                self._check_alerts()
                time.sleep(10)
            except Exception as e:
                log.error(f"Status monitor error: {e}")
                time.sleep(10)

    def _check_alerts(self):
        """Print WARNING banners when signal drought or max-loss threshold detected."""
        with self.lock:
            traders = dict(self.traders_ref)
        if not traders:
            return

        # ── Signal drought alert ─────────────────────────────────────────────
        if SIGNAL_DROUGHT_HOURS and SIGNAL_DROUGHT_HOURS > 0:
            drought_threshold = SIGNAL_DROUGHT_HOURS * 3600.0
            for symbol, trader in traders.items():
                last_ts = getattr(trader, "_last_signal_ts", None)
                if last_ts is None:
                    continue
                elapsed = time.time() - last_ts
                if elapsed >= drought_threshold:
                    hrs = elapsed / 3600.0
                    # Only print every full-status interval to avoid spam
                    msg = (
                        f"\n⚠  SIGNAL DROUGHT  {symbol}  —  "
                        f"no raw entry signal for {hrs:.1f}h  "
                        f"(threshold {SIGNAL_DROUGHT_HOURS:.0f}h)\n"
                    )
                    sys.stdout.write(msg)
                    sys.stdout.flush()

        # ── Max-loss alert ────────────────────────────────────────────────────
        if MAX_LOSS_PCT is not None:
            for symbol, trader in traders.items():
                halted = getattr(trader, "_halted", False)
                if halted:
                    halt_ts = getattr(trader, "_halt_ts", None)
                    if halt_ts is not None:
                        remaining = max(0.0, 4 * 3600.0 - (time.time() - halt_ts))
                        msg = (
                            f"\n🛑  MAX-LOSS HALT  {symbol}  —  "
                            f"resumes in {remaining/3600:.1f}h\n"
                        )
                        sys.stdout.write(msg)
                        sys.stdout.flush()

    def _display_full_status(self):
        with self.lock:
            if not self.traders_ref:
                return

            traders         = self.traders_ref
            timestamp       = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            sample          = next(iter(traders.values()))
            # Aggregate wallet and PnL across all traders (not just the first one)
            total_wallet    = sum(t.wallet for t in traders.values())
            session_pnl     = sum(t.account_pnl_usdt for t in traders.values())
            start_wallet    = sum(t.wallet - t.account_pnl_usdt for t in traders.values())
            session_pnl_pct = (session_pnl / start_wallet * 100.0) if start_wallet > 0 else sample.account_pnl_pct
            total_trades    = sum(t.trade_count for t in traders.values())
            total_wins      = sum(t.win_count   for t in traders.values())
            positions_open  = sum(1 for t in traders.values() if t.position is not None)
            win_rate        = (total_wins / total_trades * 100) if total_trades > 0 else 0.0
            pnl_sign        = "+" if session_pnl >= 0 else ""
            state_str       = "SHORT" if positions_open > 0 else "FLAT"

            lines = []
            lines.append("")
            lines.append("=" * 65)
            lines.append(f"  Mean Reversion Trader  |  {state_str}  |  {timestamp}")
            lines.append("-" * 65)
            lines.append(
                f"  Balance : ${total_wallet:.2f} USDT  |  "
                f"Session P&L: ${pnl_sign}{session_pnl:.2f} ({pnl_sign}{session_pnl_pct:.2f}%)"
            )
            lines.append(
                f"  Trades  : {total_trades}  "
                f"W={total_wins}  L={total_trades - total_wins}  "
                f"WR={win_rate:.1f}%  |  "
                f"Open: {positions_open}/{len(traders)}"
            )

            for symbol, trader in traders.items():
                candles = self.candle_counts.get(symbol, 0)
                t_wins  = trader.win_count
                t_loss  = trader.trade_count - t_wins
                t_wr    = (t_wins / trader.trade_count * 100) if trader.trade_count > 0 else 0.0
                elapsed_reopt   = time.time() - trader.last_reopt_time
                remaining_reopt = max(0.0, REOPT_INTERVAL_SEC - elapsed_reopt)
                reopt_str = (f"{int(remaining_reopt // 3600):02d}h"
                             f"{int((remaining_reopt % 3600) // 60):02d}m")
                ep = trader.entry_params
                xp = trader.exit_params

                lines.append("-" * 65)
                lines.append(
                    f"  {symbol} [{trader.interval}m]  "
                    f"MA={ep.ma_len} BandMult={ep.band_mult:.2f}%  "
                    f"TP={xp.tp_pct*100:.3f}%"
                )
                lines.append(
                    f"  Candles={candles}  "
                    f"T={trader.trade_count} W={t_wins} L={t_loss} WR={t_wr:.0f}%  |  "
                    f"Next ReOpt: {reopt_str}"
                )
                if trader.position is not None:
                    pos     = trader.position
                    qty_abs = abs(pos.qty)
                    mark    = trader.mark_price
                    upnl    = (pos.entry_price - mark) * qty_abs
                    margin  = pos.entry_price * qty_abs / float(trader.leverage)
                    upnl_pct = (upnl / margin * 100) if margin > 0 else 0.0
                    sign    = "+" if upnl >= 0 else ""
                    tp_disp = pos.entry_price * (1.0 - xp.tp_pct * LIVE_TP_SCALE)
                    lines.append(
                        f"  SHORT  entry=${pos.entry_price:.5f}  tp=${tp_disp:.5f}  "
                        f"mark=${mark:.5f}  qty={qty_abs:.4f}  "
                        f"uPnL=${sign}{upnl:.4f} ({sign}{upnl_pct:.2f}%)"
                    )
                else:
                    lines.append(f"  FLAT  -- watching for band crossover entry signal")

            lines.append("=" * 65)
            lines.append("")

            # Single atomic write — prevents thread interleaving
            sys.stdout.write("\n".join(lines) + "\n")
            sys.stdout.flush()

    def _display_quick_status(self):
        pass  # candle-close display already shows all per-candle state

    def print_trade_summary(self):
        with self.lock:
            if not self.traders_ref:
                return
            total_wallet = sum(t.wallet for t in self.traders_ref.values())
            total_pnl    = sum(t.account_pnl_usdt for t in self.traders_ref.values())
            total_trades = sum(t.trade_count for t in self.traders_ref.values())
            total_wins   = sum(t.win_count   for t in self.traders_ref.values())
            wr = (total_wins / total_trades * 100) if total_trades > 0 else 0.0
            lines = [
                "",
                "=" * 50,
                "  TRADING SUMMARY",
                "-" * 50,
                f"  Wallet  : ${total_wallet:.2f} USDT",
                f"  P&L     : ${total_pnl:+.2f}",
                f"  Trades  : {total_trades}  W={total_wins}  L={total_trades - total_wins}  WR={wr:.1f}%",
                "=" * 50,
                "",
            ]
            sys.stdout.write("\n".join(lines) + "\n")
            sys.stdout.flush()


_global_status_monitor: Optional[TradingStatusMonitor] = None


def get_status_monitor() -> TradingStatusMonitor:
    global _global_status_monitor
    if _global_status_monitor is None:
        _global_status_monitor = TradingStatusMonitor()
    return _global_status_monitor


def start_status_monitor(traders: Dict[str, Any], update_interval: float = 180.0):
    get_status_monitor().start(traders, update_interval)


def stop_status_monitor():
    get_status_monitor().stop()
