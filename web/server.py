"""Lightweight Charts web server — Mean Reversion Trader

Serves the chart UI and real-time OHLCV + band data via WebSocket.
Reads directly from data/trading.db (read-only) — never writes.
Runs in a background daemon thread launched from main.py or gui.py.

Usage:
    from web.server import start
    port = start()          # starts on 127.0.0.1:8765
    # Browser opens automatically once /api/ready returns true (DB has data)
"""

import asyncio
import logging
import resource
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

log = logging.getLogger("chart_server")

STATIC_DIR = Path(__file__).parent / "static"
DB_PATH    = Path(__file__).parent.parent / "data" / "trading.db"
HOST       = "127.0.0.1"
PORT       = 8765

# ── Singleton state ────────────────────────────────────────────────────────────
_server_thread: Optional[threading.Thread] = None
_started        = threading.Event()
_active_port    = PORT


# ── DB helper ─────────────────────────────────────────────────────────────────

@contextmanager
def _db_connection() -> Generator[sqlite3.Connection, None, None]:
    """Context manager that opens a read-only DB connection and *always* closes
    it — even when the caller raises an exception.  This prevents the FD leak
    that previously caused ``OSError: [Errno 24] Too many open files`` after
    the frontend's 2-second /api/ready polling exhausted the macOS per-process
    256-FD limit.
    """
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(
            f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _db_is_ready() -> bool:
    """Return True as soon as the candles table has seed data.

    Checking `candles` (not `candle_analytics`) means the chart unblocks
    immediately after the seed download finishes — bands/indicators fill in
    progressively once the first optimisation completes.
    """
    try:
        with _db_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM candles WHERE price_type='last'"
            ).fetchone()
            return (row[0] or 0) > 50   # need at least a minimal seed
    except Exception:
        return False


# ── FastAPI app factory ────────────────────────────────────────────────────────

def _make_app():
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles

    app = FastAPI(title="Mean Reversion Trader — Chart", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ── Static chart page ──────────────────────────────────────────────────────
    @app.get("/")
    async def index():
        return FileResponse(
            str(STATIC_DIR / "index.html"),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    # ── REST: chart-ready flag (DB-driven — no event/import required) ──────────
    @app.get("/api/ready")
    async def ready():
        return JSONResponse({"ready": _db_is_ready()})

    # ── REST: historical candles + bands + indicators ─────────────────────────
    @app.get("/api/history")
    async def history(symbol: str = "XRPUSDT", interval: str = "5", limit: int = 10000):
        try:
            with _db_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT
                        c.ts_ms                          AS time,
                        c.open, c.high, c.low, c.close,
                        c.volume,
                        a.ma,
                        a.adx, a.rsi,
                        a.premium_1,  a.premium_2,  a.premium_3,  a.premium_4,
                        a.premium_5,  a.premium_6,  a.premium_7,  a.premium_8,
                        a.discount_1, a.discount_2, a.discount_3, a.discount_4,
                        a.discount_5, a.discount_6, a.discount_7, a.discount_8
                    FROM candles c
                    LEFT JOIN candle_analytics a
                           ON c.symbol   = a.symbol
                          AND c.interval = a.interval
                          AND c.ts_utc   = a.ts_utc
                    WHERE c.symbol = ? AND c.interval = ? AND c.price_type = 'last'
                    ORDER BY c.ts_ms DESC
                    LIMIT ?
                    """,
                    (symbol, interval, limit),
                ).fetchall()
            # Reverse so oldest-first for Lightweight Charts
            return JSONResponse([dict(r) for r in reversed(rows)])
        except Exception as exc:
            log.error("history error: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=500)

    # ── REST: recent trades ────────────────────────────────────────────────────
    @app.get("/api/trades")
    async def trades(symbol: str = "XRPUSDT", interval: str = "5"):
        try:
            with _db_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT ts_utc, action, reason, side, fill_price, qty, pnl_net, result, mode
                    FROM   trades
                    WHERE  symbol = ? AND interval = ?
                    ORDER  BY ts_utc DESC
                    LIMIT  500
                    """,
                    (symbol, interval),
                ).fetchall()
            return JSONResponse([dict(r) for r in rows])
        except Exception as exc:
            log.error("trades error: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=500)

    # ── REST: latest strategy params (for threshold reference lines) ───────────
    @app.get("/api/params")
    async def params(symbol: str = "XRPUSDT", interval: str = "5"):
        try:
            with _db_connection() as conn:
                row = conn.execute(
                    """
                    SELECT adx_threshold, rsi_neutral_lo, ma_len, band_mult,
                           exit_ma_len, exit_band_mult, leverage, tp_pct, sl_pct
                    FROM   params
                    WHERE  symbol = ? AND interval = ?
                    ORDER  BY ts_utc DESC
                    LIMIT  1
                    """,
                    (symbol, interval),
                ).fetchone()
            if row:
                return JSONResponse(dict(row))
            # Fallback defaults if no params in DB yet
            return JSONResponse({
                "adx_threshold": 25, "rsi_neutral_lo": 50,
                "ma_len": 100, "band_mult": 2.5,
                "exit_ma_len": 100, "exit_band_mult": 2.5,
                "leverage": 3, "tp_pct": 0.20, "sl_pct": 0.40,
            })
        except Exception as exc:
            log.error("params error: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=500)

    # ── REST: available (symbol, interval) pairs ───────────────────────────────
    @app.get("/api/symbols")
    async def symbols():
        try:
            with _db_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT DISTINCT symbol, interval
                    FROM   candles
                    WHERE  price_type = 'last'
                    ORDER  BY symbol, CAST(interval AS INTEGER)
                    """
                ).fetchall()
            return JSONResponse([dict(r) for r in rows])
        except Exception as exc:
            log.error("symbols error: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=500)

    # ── WebSocket: live candle + trade push ────────────────────────────────────
    @app.websocket("/ws")
    async def ws_endpoint(
        websocket: WebSocket,
        symbol:   str = "XRPUSDT",
        interval: str = "5",
    ):
        await websocket.accept()
        # WebSocket keeps its own long-lived connection — it IS closed in finally.
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(
                f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False
            )
            conn.row_factory = sqlite3.Row

            # Seed cursors from current DB state
            row = conn.execute(
                "SELECT MAX(ts_ms) FROM candles WHERE symbol=? AND interval=? AND price_type='last'",
                (symbol, interval),
            ).fetchone()
            last_ts_ms = row[0] if (row and row[0]) else 0

            row2 = conn.execute(
                "SELECT MAX(id) FROM trades WHERE symbol=? AND interval=? AND mode != 'backtest'",
                (symbol, interval),
            ).fetchone()
            last_trade_id = row2[0] if (row2 and row2[0]) else 0

            while True:
                # ── New candles ────────────────────────────────────────────────
                new_candles = conn.execute(
                    """
                    SELECT
                        c.ts_ms AS time,
                        c.open, c.high, c.low, c.close, c.volume,
                        a.ma, a.adx, a.rsi,
                        a.premium_1,  a.premium_2,  a.premium_3,  a.premium_4,
                        a.premium_5,  a.premium_6,  a.premium_7,  a.premium_8,
                        a.discount_1, a.discount_2, a.discount_3, a.discount_4,
                        a.discount_5, a.discount_6, a.discount_7, a.discount_8
                    FROM candles c
                    LEFT JOIN candle_analytics a
                           ON c.symbol=a.symbol AND c.interval=a.interval AND c.ts_utc=a.ts_utc
                    WHERE c.symbol=? AND c.interval=? AND c.price_type='last' AND c.ts_ms > ?
                    ORDER BY c.ts_ms
                    """,
                    (symbol, interval, last_ts_ms),
                ).fetchall()

                for row in new_candles:
                    await websocket.send_json({"type": "candle", "data": dict(row)})
                    last_ts_ms = row["time"]

                # ── New live trades only (not backtest) ────────────────────────
                new_trades = conn.execute(
                    """
                    SELECT id, ts_utc, action, reason, fill_price, result, mode
                    FROM   trades
                    WHERE  symbol=? AND interval=? AND id > ? AND mode != 'backtest'
                    ORDER  BY id
                    """,
                    (symbol, interval, last_trade_id),
                ).fetchall()

                for row in new_trades:
                    await websocket.send_json({"type": "trade", "data": dict(row)})
                    last_trade_id = row["id"]

                await asyncio.sleep(1)

        except WebSocketDisconnect:
            pass
        except Exception as exc:
            log.debug("ws closed: %s", exc)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    return app


# ── Public API ─────────────────────────────────────────────────────────────────

def _raise_fd_limit() -> None:
    """Raise the per-process file-descriptor limit to the OS hard maximum.

    macOS ships with a default soft limit of 256 FDs per process.  The chart
    server opens a SQLite connection per HTTP request; under sustained polling
    the soft limit is hit quickly, causing ``OSError: [Errno 24] Too many open
    files`` in asyncio's socket.accept().  Bumping to the hard cap (typically
    unlimited or 10 240 on modern macOS) gives plenty of headroom.
    """
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < hard:
            resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
            log.info("[server] FD limit raised: %d → %d", soft, hard)
    except Exception as exc:
        log.warning("[server] Could not raise FD limit: %s", exc)


def start(host: str = HOST, port: int = PORT) -> int:
    """Start the chart server in a background daemon thread.

    Idempotent — calling more than once returns the active port without
    spawning a second thread.

    Returns:
        int: The port the server is (or will be) listening on.
    """
    global _server_thread, _active_port

    if _server_thread and _server_thread.is_alive():
        return _active_port

    _raise_fd_limit()
    _active_port = port
    _started.clear()

    def _run() -> None:
        import uvicorn
        app    = _make_app()
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="error",
            loop="asyncio",
        )
        server = uvicorn.Server(config)
        _started.set()
        server.run()

    _server_thread = threading.Thread(target=_run, daemon=True, name="chart-server")
    _server_thread.start()
    _started.wait(timeout=8)
    log.info("Chart server ready at http://%s:%d", host, port)
    return port
