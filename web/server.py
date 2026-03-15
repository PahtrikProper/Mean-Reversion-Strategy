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
import sqlite3
import threading
from pathlib import Path
from typing import Optional

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

def _get_conn() -> sqlite3.Connection:
    """Open a read-only connection to the trading database."""
    conn = sqlite3.connect(
        f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    return conn


def _db_is_ready() -> bool:
    """Return True when candle_analytics has at least one row (seed complete)."""
    try:
        conn = sqlite3.connect(
            f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False
        )
        row = conn.execute(
            "SELECT COUNT(*) FROM candle_analytics"
        ).fetchone()
        conn.close()
        return (row[0] or 0) > 0
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

    # ── REST: historical candles + bands ──────────────────────────────────────
    @app.get("/api/history")
    async def history(symbol: str = "XRPUSDT", interval: str = "5", limit: int = 10000):
        try:
            conn = _get_conn()
            rows = conn.execute(
                """
                SELECT
                    c.ts_ms                          AS time,
                    c.open, c.high, c.low, c.close,
                    c.volume,
                    a.ma,
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
            conn.close()
            # Reverse so oldest-first for Lightweight Charts
            return JSONResponse([dict(r) for r in reversed(rows)])
        except Exception as exc:
            log.error("history error: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=500)

    # ── REST: recent trades ────────────────────────────────────────────────────
    @app.get("/api/trades")
    async def trades(symbol: str = "XRPUSDT", interval: str = "5"):
        try:
            conn = _get_conn()
            rows = conn.execute(
                """
                SELECT ts_utc, action, side, fill_price, qty, pnl_net, result, mode
                FROM   trades
                WHERE  symbol = ? AND interval = ?
                ORDER  BY ts_utc DESC
                LIMIT  500
                """,
                (symbol, interval),
            ).fetchall()
            conn.close()
            return JSONResponse([dict(r) for r in rows])
        except Exception as exc:
            log.error("trades error: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=500)

    # ── REST: available (symbol, interval) pairs ───────────────────────────────
    @app.get("/api/symbols")
    async def symbols():
        try:
            conn = _get_conn()
            rows = conn.execute(
                """
                SELECT DISTINCT symbol, interval
                FROM   candles
                WHERE  price_type = 'last'
                ORDER  BY symbol, CAST(interval AS INTEGER)
                """
            ).fetchall()
            conn.close()
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
        conn = _get_conn()
        try:
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
                        a.ma,
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
                    SELECT id, ts_utc, action, fill_price, result, mode
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
            conn.close()

    return app


# ── Public API ─────────────────────────────────────────────────────────────────

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
