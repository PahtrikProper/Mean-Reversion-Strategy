"""Logging and File Output"""

import os
import csv
import logging
import threading

from .constants import (
    LOG_DIR,
    EVENT_LOG_PATH,
    TRADES_CSV_PATH,
    PARAMS_CSV_PATH,
    ORDERS_LOG_PATH,
    COLOR_LONG,
    COLOR_SHORT,
    COLOR_ENTRY,
    COLOR_EXIT,
    COLOR_ERROR,
    COLOR_RESET,
    COLOR_PENDING,
    COLOR_CONFIRMED,
    COLOR_SUBMITTED,
)

log = logging.getLogger("paper")

_order_log_lock = threading.Lock()


def log_order(
    ts_utc: str,
    symbol: str,
    side: str,
    qty: float,
    price: float,
    order_type: str,
    status: str,
    order_id: str = None,
    reason: str = None,
    error: str = None,
    signal_side: str = None,
    signal_level: int = 0,
):
    side_color = COLOR_LONG if side == "LONG" else COLOR_SHORT
    if order_type == "ENTRY":
        order_color = COLOR_SUBMITTED
    elif order_type == "EXIT":
        order_color = COLOR_EXIT
    else:
        order_color = COLOR_RESET

    status_color = COLOR_ERROR if status == "FAILED" else COLOR_RESET
    signal_tag = ""
    if signal_side:
        sig_color  = COLOR_LONG if "LONG" in signal_side else COLOR_SHORT
        signal_tag = f"{sig_color}{signal_side}{COLOR_RESET}"

    parts = [
        f"[{ts_utc}]",
        f"{side_color}{side}{COLOR_RESET}",
        f"{order_color}{order_type}{COLOR_RESET}",
        symbol,
        f"qty={qty:.6f}",
        f"price={price:.8f}",
        f"status={status_color}{status}{COLOR_RESET}",
    ]
    if signal_tag:
        parts.append(f"signal={signal_tag}")
    if order_id:
        parts.append(f"order_id={order_id}")
    if reason:
        parts.append(f"reason={reason}")
    if error:
        parts.append(f"error={error}")

    log.info(" | ".join(parts))

    plain_parts = [
        f"[{ts_utc}]", side, order_type, symbol,
        f"qty={qty:.6f}", f"price={price:.8f}", status,
    ]
    if signal_side:
        plain_parts.append(f"signal={signal_side}")
    if order_id:
        plain_parts.append(f"order_id={order_id}")
    if reason:
        plain_parts.append(f"reason={reason}")
    if error:
        plain_parts.append(f"error={error}")

    with _order_log_lock:
        with open(ORDERS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(" | ".join(plain_parts) + "\n")


def ensure_csv(path, header):
    if not (os.path.exists(path) and os.path.getsize(path) > 0):
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(header)


def csv_append(path, row):
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(row)


# ── Trade log ─────────────────────────────────────────────────────────────────
ensure_csv(TRADES_CSV_PATH, [
    "ts_utc", "symbol", "action", "reason", "signal_side", "side",
    "qty", "fill_price", "notional", "fee",
    "entry_price", "tp_price", "mark_price",
    "wallet_before", "wallet_after",
    "pnl_gross", "pnl_net",
    "pnl_1x_usdt", "pnl_pct",
    "result",
    "ma_len", "band_mult",
    "tp_pct",
])

# ── Param log ─────────────────────────────────────────────────────────────────
ensure_csv(PARAMS_CSV_PATH, [
    "ts_utc", "event",
    "ma_len", "band_mult",
    "tp_pct",
    "wallet", "sharpe_ratio",
])
