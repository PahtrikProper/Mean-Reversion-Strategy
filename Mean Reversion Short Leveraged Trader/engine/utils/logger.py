"""Logging — Mean Reversion Strategy

All persistent logging goes to SQLite via db_logger.
This module provides:
  - log_order(): coloured console output + SQLite write
  - csv_append() / ensure_csv(): no-ops kept for import compatibility
"""

import logging
import threading

from .constants import (
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
from . import db_logger as _db

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

    # Write to SQLite
    mode = "paper" if symbol.startswith("[PAPER]") else "live"
    clean_symbol = symbol.replace("[PAPER]", "")
    _db.log_order(
        ts_utc=ts_utc, mode=mode, symbol=clean_symbol,
        side=side, qty=qty, price=price,
        order_type=order_type, status=status,
        order_id=order_id, reason=reason, error=error,
        signal_side=signal_side, signal_level=signal_level,
    )


def ensure_csv(path, header):
    """No-op — CSV files are no longer written; everything goes to SQLite."""
    pass


def csv_append(path, row):
    """No-op — CSV files are no longer written; everything goes to SQLite."""
    pass
