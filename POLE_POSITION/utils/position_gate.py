"""Position Gate — thread-safe mutex allowing up to MAX_SLOTS concurrent positions."""

import threading
from typing import Set

class PositionGate:
    """Thread-safe gate allowing up to MAX_SLOTS symbols to hold open positions simultaneously.

    When trading multiple symbols concurrently (e.g. the top-2 ranked symbols), this
    prevents more than MAX_SLOTS positions from being open at once.  Each symbol
    gets its own independent slot — two different symbols can trade in parallel,
    but a third symbol is blocked until one of them closes.

    The live trader calls try_acquire() before placing an entry and release() after
    closing a position.  force_acquire() is used at startup to sync gate state when
    a position is already open on Bybit."""

    MAX_SLOTS: int = 1  # allow top-1 symbol to trade at a time

    def __init__(self):
        """Initialize the gate with no active symbols and a threading lock."""
        self._lock = threading.Lock()
        self._active_symbols: Set[str] = set()

    def try_acquire(self, symbol: str) -> bool:
        """Attempt to claim a position slot for this symbol.

        Returns True if the symbol already holds a slot, or if a free slot exists
        (slot is now claimed for this symbol).
        Returns False if all MAX_SLOTS slots are taken by other symbols."""
        with self._lock:
            if symbol in self._active_symbols:
                return True  # already holds a slot
            if len(self._active_symbols) < self.MAX_SLOTS:
                self._active_symbols.add(symbol)
                return True  # claimed a free slot
            return False  # all slots occupied by other symbols

    def release(self, symbol: str) -> None:
        """Release the position slot after closing a position.
        Safe to call even if the symbol doesn't hold a slot (no-op)."""
        with self._lock:
            self._active_symbols.discard(symbol)

    def force_acquire(self, symbol: str) -> None:
        """Unconditionally claim a slot.  Used at startup when a position is already
        open (detected via REST) to sync the gate state with Bybit reality."""
        with self._lock:
            self._active_symbols.add(symbol)

    @property
    def active_count(self) -> int:
        """Return the number of currently active (position-holding) symbols."""
        with self._lock:
            return len(self._active_symbols)

    @property
    def active_symbols(self) -> Set[str]:
        """Return a copy of the set of currently active symbols."""
        with self._lock:
            return set(self._active_symbols)
