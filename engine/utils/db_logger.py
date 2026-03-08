"""SQLite database logger — Mean Reversion Trader

Thread-safe WAL-mode database logging for all strategy events.
Call init_db(path) once at startup; all other functions are safe
to call from any thread thereafter.

Tables
------
candles              — raw OHLCV for every closed candle (last + mark price)
candle_analytics     — computed candle anatomy, indicators, band geometry,
                       volatility, volume, and market-context metrics
signals              — every entry/exit signal (fired or blocked)
trades               — every ENTRY and EXIT fill
orders               — every order placement attempt (live mode)
positions            — position snapshot on every closed candle
params               — parameter changes from re-optimisation
optimization_runs    — summary of each optimizer run
optimization_trials  — every valid trial from each optimizer run
monte_carlo_runs     — aggregated MC simulation statistics
balance_snapshots    — wallet balance at key events
events               — general INFO / WARNING / ERROR events
"""

import sqlite3
import threading
import json
import math
import logging
import datetime
import numpy as np
from typing import Optional, List, Any

log = logging.getLogger("db_logger")

# ── Singleton state ────────────────────────────────────────────────────────────
_lock: threading.Lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def init_db(path: str) -> None:
    """Open (or create) the SQLite database and create all tables.

    Safe to call multiple times — subsequent calls are no-ops if the
    database is already open at the same path.
    """
    global _conn
    with _lock:
        if _conn is not None:
            return
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute("PRAGMA cache_size=10000")
        _conn.execute("PRAGMA temp_store=MEMORY")
        _create_tables()
        _conn.commit()
    log.info(f"[DB] Opened database: {path}")


def _execute(sql: str, params: tuple = ()) -> None:
    """Execute a single statement inside the global lock.

    Never raises — log failures are printed but the trading loop
    is never interrupted.
    """
    if _conn is None:
        return
    try:
        with _lock:
            _conn.execute(sql, params)
            _conn.commit()
    except Exception as exc:
        log.warning(f"[DB] Write failed: {exc}")


def _executemany(sql: str, rows: List[tuple]) -> None:
    """Bulk insert with a single commit — used for optimization trials."""
    if _conn is None or not rows:
        return
    try:
        with _lock:
            _conn.executemany(sql, rows)
            _conn.commit()
    except Exception as exc:
        log.warning(f"[DB] Bulk write failed: {exc}")


def _create_tables() -> None:
    assert _conn is not None
    _conn.executescript("""
    -- ── Raw candles ──────────────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS candles (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_utc     TEXT    NOT NULL,
        ts_ms      INTEGER,
        symbol     TEXT    NOT NULL,
        interval   TEXT    NOT NULL,
        price_type TEXT    NOT NULL,   -- 'last' or 'mark'
        open       REAL,
        high       REAL,
        low        REAL,
        close      REAL,
        volume     REAL
    );

    -- ── Candle analytics ─────────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS candle_analytics (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_utc               TEXT    NOT NULL,
        symbol               TEXT    NOT NULL,
        interval             TEXT    NOT NULL,
        -- Candle anatomy
        body_ratio           REAL,   -- (close-open)/(high-low), signed
        upper_wick_ratio     REAL,   -- (high-max(o,c)) / range
        lower_wick_ratio     REAL,   -- (min(o,c)-low)  / range
        candle_direction     TEXT,   -- 'bullish', 'bearish', 'doji'
        -- Core indicators
        ma                   REAL,   -- RMA(close, ma_len) centre line
        atr                  REAL,
        atr_pct              REAL,   -- atr / close * 100
        adx                  REAL,
        rsi                  REAL,
        -- Premium bands 1-8
        premium_1 REAL, premium_2 REAL, premium_3 REAL, premium_4 REAL,
        premium_5 REAL, premium_6 REAL, premium_7 REAL, premium_8 REAL,
        -- Discount bands 1-8
        discount_1 REAL, discount_2 REAL, discount_3 REAL, discount_4 REAL,
        discount_5 REAL, discount_6 REAL, discount_7 REAL, discount_8 REAL,
        band_width_pct       REAL,   -- (premium_8 - discount_8) / close * 100
        -- Distance from close to each premium band (positive = band above price)
        dist_to_premium_1 REAL, dist_to_premium_2 REAL,
        dist_to_premium_3 REAL, dist_to_premium_4 REAL,
        dist_to_premium_5 REAL, dist_to_premium_6 REAL,
        dist_to_premium_7 REAL, dist_to_premium_8 REAL,
        -- Distance from close to each discount band (positive = price above band)
        dist_to_discount_1 REAL, dist_to_discount_2 REAL,
        dist_to_discount_3 REAL, dist_to_discount_4 REAL,
        dist_to_discount_5 REAL, dist_to_discount_6 REAL,
        dist_to_discount_7 REAL, dist_to_discount_8 REAL,
        -- Volatility
        hv_20                REAL,  -- 20-bar historical vol (std of log returns)
        -- Volume
        volume               REAL,
        volume_ratio         REAL,  -- volume / 20-bar avg volume
        -- Market context
        mark_price           REAL,
        basis_pct            REAL,  -- (close - mark) / mark * 100
        -- Strategy params in effect
        ma_len               INTEGER,
        band_mult            REAL
    );

    -- ── Signals ──────────────────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS signals (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_utc           TEXT    NOT NULL,
        symbol           TEXT    NOT NULL,
        interval         TEXT    NOT NULL,
        signal_type      TEXT,   -- 'ENTRY','EXIT_BAND','EXIT_TRAIL','EXIT_TP',
                                 -- 'EXIT_LIQ','NONE'
        raw_band_level   INTEGER,  -- 0-8; raw crossover before gates
        final_band_level INTEGER,  -- 0-8; after gate filtering
        adx              REAL,
        rsi              REAL,
        atr              REAL,
        trail_stop_level REAL,   -- computed trail stop price (NULL if flat)
        blocked_by       TEXT,   -- 'ADX','RSI','GATE','POSITION', NULL if fired
        open             REAL,
        high             REAL,
        low              REAL,
        close            REAL,
        ma_len           INTEGER,
        band_mult        REAL,
        tp_pct           REAL
    );

    -- ── Trades ───────────────────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS trades (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_utc        TEXT    NOT NULL,
        mode          TEXT,   -- 'live' or 'paper'
        symbol        TEXT    NOT NULL,
        interval      TEXT,
        action        TEXT,   -- 'ENTRY' or 'EXIT'
        reason        TEXT,   -- 'BAND_ENTRY','TP','TRAIL_STOP','BAND_EXIT',
                              -- 'LIQUIDATED','EXTERNAL_CLOSE'
        side          TEXT,
        qty           REAL,
        fill_price    REAL,
        notional      REAL,
        fee           REAL,
        entry_price   REAL,
        tp_price      REAL,
        mark_price    REAL,
        wallet_before REAL,
        wallet_after  REAL,
        pnl_gross     REAL,
        pnl_net       REAL,
        pnl_1x_usdt   REAL,
        pnl_pct       REAL,
        result        TEXT,   -- 'WIN','LOSS',''
        ma_len        INTEGER,
        band_mult     REAL,
        tp_pct        REAL
    );

    -- ── Orders ───────────────────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS orders (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_utc       TEXT    NOT NULL,
        mode         TEXT,
        symbol       TEXT    NOT NULL,
        side         TEXT,
        qty          REAL,
        price        REAL,
        order_type   TEXT,
        status       TEXT,
        order_id     TEXT,
        reason       TEXT,
        error        TEXT,
        signal_side  TEXT,
        signal_level INTEGER
    );

    -- ── Position snapshots ───────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS positions (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_utc              TEXT    NOT NULL,
        symbol              TEXT    NOT NULL,
        qty                 REAL,
        entry_price         REAL,
        entry_time          TEXT,
        mark_price          REAL,
        liquidation_price   REAL,
        unrealized_pnl      REAL,
        min_low_since_entry REAL,
        trail_stop_price    REAL,
        tp_price            REAL,
        wallet              REAL
    );

    -- ── Parameter changes ────────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS params (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_utc           TEXT    NOT NULL,
        symbol           TEXT    NOT NULL,
        interval         TEXT,
        event            TEXT,   -- 'STARTUP','REOPT_ACCEPTED','REOPT_REJECTED'
        ma_len           INTEGER,
        band_mult        REAL,
        tp_pct           REAL,
        mc_score         REAL,
        sharpe           REAL,
        pnl_pct          REAL,
        max_drawdown_pct REAL,
        trade_count      INTEGER,
        winrate          REAL,
        wallet           REAL
    );

    -- ── Optimization run summary ─────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS optimization_runs (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id         TEXT    NOT NULL,
        ts_utc         TEXT    NOT NULL,
        symbol         TEXT    NOT NULL,
        interval       TEXT,
        trigger        TEXT,   -- 'STARTUP' or 'REOPT'
        total_trials   INTEGER,
        valid_trials   INTEGER,
        duration_sec   REAL,
        best_ma_len    INTEGER,
        best_band_mult REAL,
        best_tp_pct    REAL,
        best_pnl_pct   REAL,
        best_n_losses  INTEGER,
        accepted       INTEGER  -- 1 = params accepted, 0 = rejected
    );

    -- ── Per-trial results ────────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS optimization_trials (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id           TEXT    NOT NULL,
        trial_num        INTEGER,
        ma_len           INTEGER,
        band_mult        REAL,
        tp_pct           REAL,
        trades           INTEGER,
        n_wins           INTEGER,
        n_losses         INTEGER,
        win_rate         REAL,
        profit_factor    REAL,
        return_pct       REAL,
        pnl_usdt         REAL,
        avg_win          REAL,
        avg_loss         REAL,
        max_drawdown_pct REAL,
        sharpe           REAL
    );

    -- ── Monte Carlo aggregated stats ─────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS monte_carlo_runs (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_utc       TEXT    NOT NULL,
        symbol       TEXT    NOT NULL,
        interval     TEXT,
        simulations  INTEGER,
        p5_pnl       REAL,
        p25_pnl      REAL,
        p50_pnl      REAL,
        p75_pnl      REAL,
        p95_pnl      REAL,
        prob_profit  REAL,
        prob_ruin    REAL,
        p5_drawdown  REAL,
        p50_drawdown REAL,
        p95_drawdown REAL,
        median_max_losing_streak REAL,
        mc_score     REAL
    );

    -- ── Balance snapshots ────────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS balance_snapshots (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_utc           TEXT    NOT NULL,
        symbol           TEXT    NOT NULL,
        event            TEXT,
        wallet_usdt      REAL,
        session_pnl_usdt REAL,
        session_pnl_pct  REAL
    );

    -- ── General events ───────────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS events (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_utc     TEXT    NOT NULL,
        level      TEXT,   -- 'INFO','WARNING','ERROR'
        event_type TEXT,
        symbol     TEXT,
        message    TEXT,
        detail     TEXT    -- JSON blob for extra context
    );

    -- ── Mark price ticks ─────────────────────────────────────────────────────
    CREATE TABLE IF NOT EXISTS mark_price_ticks (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_utc     TEXT    NOT NULL,
        symbol     TEXT    NOT NULL,
        mark_price REAL    NOT NULL
    );

    -- ── Missed trades (blocked signals that would have been profitable) ────────
    CREATE TABLE IF NOT EXISTS missed_trades (
        id                       INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_ts                 TEXT    NOT NULL,
        resolved_ts              TEXT,
        symbol                   TEXT    NOT NULL,
        interval                 TEXT,
        blocked_by               TEXT,   -- 'ADX','RSI','POSITION','WALLET'
        entry_price              REAL,
        tp_price                 REAL,
        trail_stop_at_resolution REAL,
        band                     INTEGER,
        adx_at_entry             REAL,
        rsi_at_entry             REAL,
        outcome                  TEXT,   -- 'TP_HIT','TRAIL_STOPPED'
        outcome_pnl_pct          REAL,   -- leveraged PnL% that would have been achieved
        candles_elapsed          INTEGER
    );

    -- ── Indexes ──────────────────────────────────────────────────────────────
    CREATE INDEX IF NOT EXISTS idx_candles_sym      ON candles           (symbol, interval, ts_utc);
    CREATE INDEX IF NOT EXISTS idx_analytics_sym    ON candle_analytics  (symbol, interval, ts_utc);
    CREATE INDEX IF NOT EXISTS idx_signals_sym      ON signals           (symbol, ts_utc);
    CREATE INDEX IF NOT EXISTS idx_trades_sym       ON trades            (symbol, ts_utc);
    CREATE INDEX IF NOT EXISTS idx_orders_sym       ON orders            (symbol, ts_utc);
    CREATE INDEX IF NOT EXISTS idx_positions_sym    ON positions         (symbol, ts_utc);
    CREATE INDEX IF NOT EXISTS idx_opt_runs_id      ON optimization_runs (run_id);
    CREATE INDEX IF NOT EXISTS idx_opt_trials_id    ON optimization_trials (run_id);
    CREATE INDEX IF NOT EXISTS idx_mc_sym           ON monte_carlo_runs  (symbol, ts_utc);
    CREATE INDEX IF NOT EXISTS idx_balance_sym      ON balance_snapshots (symbol, ts_utc);
    CREATE INDEX IF NOT EXISTS idx_events_sym       ON events            (symbol, ts_utc);
    CREATE INDEX IF NOT EXISTS idx_mp_ticks_sym     ON mark_price_ticks  (symbol, ts_utc);
    CREATE INDEX IF NOT EXISTS idx_missed_sym       ON missed_trades     (symbol, entry_ts);
    """)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _safe(v: Any) -> Any:
    """Return None for NaN/inf floats so SQLite stores NULL."""
    if v is None:
        return None
    try:
        if math.isnan(v) or math.isinf(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


# ── Public log functions ───────────────────────────────────────────────────────

def log_candle(
    ts_utc: str,
    ts_ms: int,
    symbol: str,
    interval: str,
    price_type: str,
    o: float,
    h: float,
    l: float,
    c: float,
    vol: float,
) -> None:
    _execute(
        "INSERT INTO candles "
        "(ts_utc,ts_ms,symbol,interval,price_type,open,high,low,close,volume) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ts_utc, ts_ms, symbol, interval, price_type, o, h, l, c, vol),
    )


def log_candle_analytics(
    ts_utc: str,
    symbol: str,
    interval: str,
    df: Any,          # pd.DataFrame with indicator columns
    mark_price: float,
    ma_len: int,
    band_mult: float,
) -> None:
    """Compute and log all candle analytics from the indicator DataFrame."""
    if df is None or len(df) < 2:
        return

    row = df.iloc[-1]

    o   = _safe(float(row["open"]))
    h   = _safe(float(row["high"]))
    l   = _safe(float(row["low"]))
    c   = _safe(float(row["close"]))
    vol = _safe(float(row.get("volume", 0)))

    # ── Candle anatomy ──────────────────────────────────────────────────────
    rng = (h - l) if (h is not None and l is not None) else 0.0
    if rng and rng > 0 and o is not None and c is not None:
        body             = c - o
        body_ratio       = _safe(body / rng)
        upper_wick_ratio = _safe((h - max(o, c)) / rng)
        lower_wick_ratio = _safe((min(o, c) - l) / rng)
        if abs(body_ratio or 0) < 0.1:
            direction = "doji"
        elif body > 0:
            direction = "bullish"
        else:
            direction = "bearish"
    else:
        body_ratio = upper_wick_ratio = lower_wick_ratio = None
        direction = "doji"

    # ── Core indicators ─────────────────────────────────────────────────────
    def _col(key: str) -> Optional[float]:
        v = row.get(key)
        if v is None:
            return None
        try:
            fv = float(v)
            return None if (math.isnan(fv) or math.isinf(fv)) else fv
        except (TypeError, ValueError):
            return None

    ma_val  = _col("main")
    atr_val = _col("atr")
    adx_val = _col("adx")
    rsi_val = _col("rsi")
    atr_pct = _safe((atr_val / c * 100) if (atr_val and c and c > 0) else None)

    # ── Bands ───────────────────────────────────────────────────────────────
    premiums  = [_col(f"premium_{k}")  for k in range(1, 9)]
    discounts = [_col(f"discount_{k}") for k in range(1, 9)]

    p8, d8 = premiums[7], discounts[7]
    band_width_pct = _safe(
        ((p8 - d8) / c * 100) if (p8 is not None and d8 is not None and c and c > 0) else None
    )

    dist_premiums = [
        _safe(((p - c) / c * 100) if (p is not None and c and c > 0) else None)
        for p in premiums
    ]
    dist_discounts = [
        _safe(((c - d) / c * 100) if (d is not None and c and c > 0) else None)
        for d in discounts
    ]

    # ── Historical volatility (20-bar) ──────────────────────────────────────
    hv_20 = None
    if "close" in df.columns and len(df) >= 21:
        try:
            closes = df["close"].iloc[-21:].to_numpy(dtype=float)
            log_rets = np.log(closes[1:] / closes[:-1])
            hv_20 = _safe(float(np.std(log_rets, ddof=1)) if len(log_rets) >= 2 else None)
        except Exception:
            pass

    # ── Volume ratio ────────────────────────────────────────────────────────
    vol_ratio = None
    if "volume" in df.columns and len(df) >= 20 and vol is not None:
        try:
            recent = df["volume"].iloc[-20:].to_numpy(dtype=float)
            avg_vol = float(np.mean(recent[:-1])) if len(recent) > 1 else 0.0
            vol_ratio = _safe((vol / avg_vol) if avg_vol > 0 else None)
        except Exception:
            pass

    # ── Basis ────────────────────────────────────────────────────────────────
    mp = _safe(mark_price)
    basis_pct = _safe(
        ((c - mark_price) / mark_price * 100)
        if (c and mark_price and mark_price > 0) else None
    )

    _execute(
        """INSERT INTO candle_analytics (
            ts_utc, symbol, interval,
            body_ratio, upper_wick_ratio, lower_wick_ratio, candle_direction,
            ma, atr, atr_pct, adx, rsi,
            premium_1, premium_2, premium_3, premium_4,
            premium_5, premium_6, premium_7, premium_8,
            discount_1, discount_2, discount_3, discount_4,
            discount_5, discount_6, discount_7, discount_8,
            band_width_pct,
            dist_to_premium_1, dist_to_premium_2, dist_to_premium_3, dist_to_premium_4,
            dist_to_premium_5, dist_to_premium_6, dist_to_premium_7, dist_to_premium_8,
            dist_to_discount_1, dist_to_discount_2, dist_to_discount_3, dist_to_discount_4,
            dist_to_discount_5, dist_to_discount_6, dist_to_discount_7, dist_to_discount_8,
            hv_20, volume, volume_ratio,
            mark_price, basis_pct,
            ma_len, band_mult
        ) VALUES (
            ?,?,?,?,?,?,?,?,?,?,?,?,
            ?,?,?,?,?,?,?,?,
            ?,?,?,?,?,?,?,?,
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
            ?,?,?,?,?,?,?
        )""",
        (
            ts_utc, symbol, interval,
            body_ratio, upper_wick_ratio, lower_wick_ratio, direction,
            ma_val, atr_val, atr_pct, adx_val, rsi_val,
            *premiums,
            *discounts,
            band_width_pct,
            *dist_premiums,
            *dist_discounts,
            hv_20, vol, vol_ratio,
            mp, basis_pct,
            ma_len, band_mult,
        ),
    )


def log_signal(
    ts_utc: str,
    symbol: str,
    interval: str,
    signal_type: str,
    raw_band_level: int,
    final_band_level: int,
    adx: Optional[float],
    rsi: Optional[float],
    atr: Optional[float],
    trail_stop_level: Optional[float],
    blocked_by: Optional[str],
    o: float, h: float, l: float, c: float,
    ma_len: int,
    band_mult: float,
    tp_pct: float,
) -> None:
    _execute(
        """INSERT INTO signals (
            ts_utc, symbol, interval, signal_type,
            raw_band_level, final_band_level,
            adx, rsi, atr, trail_stop_level, blocked_by,
            open, high, low, close,
            ma_len, band_mult, tp_pct
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            ts_utc, symbol, interval, signal_type,
            raw_band_level, final_band_level,
            _safe(adx), _safe(rsi), _safe(atr), _safe(trail_stop_level), blocked_by,
            _safe(o), _safe(h), _safe(l), _safe(c),
            ma_len, band_mult, tp_pct,
        ),
    )


def log_trade(
    ts_utc: str,
    mode: str,
    symbol: str,
    interval: str,
    action: str,
    reason: str,
    side: str,
    qty: float,
    fill_price: float,
    notional: float,
    fee: float,
    entry_price: float,
    tp_price: float,
    mark_price: float,
    wallet_before: float,
    wallet_after: float,
    pnl_gross: float,
    pnl_net: float,
    pnl_1x_usdt: float,
    pnl_pct: float,
    result: str,
    ma_len: int,
    band_mult: float,
    tp_pct: float,
) -> None:
    _execute(
        """INSERT INTO trades (
            ts_utc, mode, symbol, interval, action, reason, side,
            qty, fill_price, notional, fee,
            entry_price, tp_price, mark_price,
            wallet_before, wallet_after,
            pnl_gross, pnl_net, pnl_1x_usdt, pnl_pct, result,
            ma_len, band_mult, tp_pct
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            ts_utc, mode, symbol, interval, action, reason, side,
            _safe(qty), _safe(fill_price), _safe(notional), _safe(fee),
            _safe(entry_price), _safe(tp_price), _safe(mark_price),
            _safe(wallet_before), _safe(wallet_after),
            _safe(pnl_gross), _safe(pnl_net), _safe(pnl_1x_usdt), _safe(pnl_pct), result,
            ma_len, _safe(band_mult), _safe(tp_pct),
        ),
    )


def log_order(
    ts_utc: str,
    mode: str,
    symbol: str,
    side: str,
    qty: float,
    price: float,
    order_type: str,
    status: str,
    order_id: Optional[str] = None,
    reason: Optional[str] = None,
    error: Optional[str] = None,
    signal_side: Optional[str] = None,
    signal_level: int = 0,
) -> None:
    _execute(
        """INSERT INTO orders (
            ts_utc, mode, symbol, side, qty, price,
            order_type, status, order_id, reason, error,
            signal_side, signal_level
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            ts_utc, mode, symbol, side,
            _safe(qty), _safe(price),
            order_type, status, order_id, reason, error,
            signal_side, signal_level,
        ),
    )


def log_position(
    ts_utc: str,
    symbol: str,
    qty: Optional[float],
    entry_price: Optional[float],
    entry_time: Optional[str],
    mark_price: Optional[float],
    liquidation_price: Optional[float],
    unrealized_pnl: Optional[float],
    min_low_since_entry: Optional[float],
    trail_stop_price: Optional[float],
    tp_price: Optional[float],
    wallet: float,
) -> None:
    _execute(
        """INSERT INTO positions (
            ts_utc, symbol,
            qty, entry_price, entry_time, mark_price, liquidation_price,
            unrealized_pnl, min_low_since_entry, trail_stop_price, tp_price,
            wallet
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            ts_utc, symbol,
            _safe(qty), _safe(entry_price), entry_time,
            _safe(mark_price), _safe(liquidation_price),
            _safe(unrealized_pnl), _safe(min_low_since_entry),
            _safe(trail_stop_price), _safe(tp_price),
            _safe(wallet),
        ),
    )


def log_params(
    ts_utc: str,
    symbol: str,
    interval: str,
    event: str,
    ma_len: int,
    band_mult: float,
    tp_pct: float,
    mc_score: Optional[float] = None,
    sharpe: Optional[float] = None,
    pnl_pct: Optional[float] = None,
    max_drawdown_pct: Optional[float] = None,
    trade_count: Optional[int] = None,
    winrate: Optional[float] = None,
    wallet: Optional[float] = None,
) -> None:
    _execute(
        """INSERT INTO params (
            ts_utc, symbol, interval, event,
            ma_len, band_mult, tp_pct,
            mc_score, sharpe, pnl_pct, max_drawdown_pct,
            trade_count, winrate, wallet
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            ts_utc, symbol, interval, event,
            ma_len, _safe(band_mult), _safe(tp_pct),
            _safe(mc_score), _safe(sharpe), _safe(pnl_pct), _safe(max_drawdown_pct),
            trade_count, _safe(winrate), _safe(wallet),
        ),
    )


def log_optimization_run(
    run_id: str,
    ts_utc: str,
    symbol: str,
    interval: str,
    trigger: str,
    total_trials: int,
    valid_trials: int,
    duration_sec: float,
    best_ma_len: int,
    best_band_mult: float,
    best_tp_pct: float,
    best_pnl_pct: float,
    best_n_losses: int,
    accepted: bool = True,
) -> None:
    _execute(
        """INSERT INTO optimization_runs (
            run_id, ts_utc, symbol, interval, trigger,
            total_trials, valid_trials, duration_sec,
            best_ma_len, best_band_mult, best_tp_pct, best_pnl_pct, best_n_losses,
            accepted
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            run_id, ts_utc, symbol, interval, trigger,
            total_trials, valid_trials, _safe(duration_sec),
            best_ma_len, _safe(best_band_mult), _safe(best_tp_pct),
            _safe(best_pnl_pct), best_n_losses,
            1 if accepted else 0,
        ),
    )


def log_optimization_trials(run_id: str, results: List[dict]) -> None:
    """Bulk-insert all valid optimizer trial results."""
    rows = []
    for i, r in enumerate(results, 1):
        pf = r.get("profit_factor", 0.0)
        if pf == float("inf") or (isinstance(pf, float) and math.isinf(pf)):
            pf = None
        rows.append((
            run_id, i,
            r.get("ma_len"), _safe(r.get("band_mult")), _safe(r.get("tp_pct")),
            r.get("trades"), r.get("n_wins"), r.get("n_losses"),
            _safe(r.get("win_rate")), _safe(pf),
            _safe(r.get("return_pct")), _safe(r.get("pnl_usdt")),
            _safe(r.get("avg_win")), _safe(r.get("avg_loss")),
            _safe(r.get("max_drawdown_pct")), _safe(r.get("sharpe")),
        ))
    _executemany(
        """INSERT INTO optimization_trials (
            run_id, trial_num,
            ma_len, band_mult, tp_pct,
            trades, n_wins, n_losses,
            win_rate, profit_factor,
            return_pct, pnl_usdt,
            avg_win, avg_loss,
            max_drawdown_pct, sharpe
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )


def log_monte_carlo(
    ts_utc: str,
    symbol: str,
    interval: str,
    mc_results: List[Any],  # list of MCSimResult
    score: Optional[float] = None,
) -> None:
    """Aggregate and log Monte Carlo simulation results."""
    if not mc_results:
        return
    try:
        pnl_pcts  = np.array([r.pnl_pct          for r in mc_results], dtype=float)
        drawdowns = np.array([r.max_drawdown_pct  for r in mc_results], dtype=float)
        streaks   = np.array([r.max_losing_streak for r in mc_results], dtype=float)
        prob_profit = float(np.mean(pnl_pcts > 0))
        prob_ruin   = float(np.mean([r.ruined for r in mc_results]))
        _execute(
            """INSERT INTO monte_carlo_runs (
                ts_utc, symbol, interval, simulations,
                p5_pnl, p25_pnl, p50_pnl, p75_pnl, p95_pnl,
                prob_profit, prob_ruin,
                p5_drawdown, p50_drawdown, p95_drawdown,
                median_max_losing_streak, mc_score
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                ts_utc, symbol, interval, len(mc_results),
                _safe(float(np.percentile(pnl_pcts, 5))),
                _safe(float(np.percentile(pnl_pcts, 25))),
                _safe(float(np.percentile(pnl_pcts, 50))),
                _safe(float(np.percentile(pnl_pcts, 75))),
                _safe(float(np.percentile(pnl_pcts, 95))),
                _safe(prob_profit), _safe(prob_ruin),
                _safe(float(np.percentile(drawdowns, 5))),
                _safe(float(np.percentile(drawdowns, 50))),
                _safe(float(np.percentile(drawdowns, 95))),
                _safe(float(np.median(streaks))),
                _safe(score),
            ),
        )
    except Exception as exc:
        log.warning(f"[DB] log_monte_carlo failed: {exc}")


def log_balance_snapshot(
    ts_utc: str,
    symbol: str,
    event: str,
    wallet_usdt: float,
    session_pnl_usdt: float,
    session_pnl_pct: float,
) -> None:
    _execute(
        """INSERT INTO balance_snapshots (
            ts_utc, symbol, event, wallet_usdt, session_pnl_usdt, session_pnl_pct
        ) VALUES (?,?,?,?,?,?)""",
        (
            ts_utc, symbol, event,
            _safe(wallet_usdt), _safe(session_pnl_usdt), _safe(session_pnl_pct),
        ),
    )


def log_event(
    ts_utc: str,
    level: str,
    event_type: str,
    symbol: Optional[str] = None,
    message: str = "",
    detail: Optional[dict] = None,
) -> None:
    detail_str = json.dumps(detail) if detail else None
    _execute(
        """INSERT INTO events (ts_utc, level, event_type, symbol, message, detail)
           VALUES (?,?,?,?,?,?)""",
        (ts_utc, level, event_type, symbol, message, detail_str),
    )


def log_mark_price_tick(
    ts_utc: str,
    symbol: str,
    mark_price: float,
) -> None:
    _execute(
        "INSERT INTO mark_price_ticks (ts_utc, symbol, mark_price) VALUES (?,?,?)",
        (ts_utc, symbol, _safe(mark_price)),
    )


def log_missed_trade(
    entry_ts: str,
    resolved_ts: str,
    symbol: str,
    interval: str,
    blocked_by: str,
    entry_price: float,
    tp_price: float,
    trail_stop_at_resolution: Optional[float],
    band: int,
    adx_at_entry: Optional[float],
    rsi_at_entry: Optional[float],
    outcome: str,
    outcome_pnl_pct: float,
    candles_elapsed: int,
) -> None:
    """Log a signal that was blocked by a gate but would have resolved profitably or as a loss.

    outcome is 'TP_HIT' (profitable) or 'TRAIL_STOPPED' (stopped out).
    outcome_pnl_pct is the leveraged percentage P&L that would have been achieved.
    """
    _execute(
        """INSERT INTO missed_trades (
            entry_ts, resolved_ts, symbol, interval, blocked_by,
            entry_price, tp_price, trail_stop_at_resolution,
            band, adx_at_entry, rsi_at_entry,
            outcome, outcome_pnl_pct, candles_elapsed
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            entry_ts, resolved_ts, symbol, interval, blocked_by,
            _safe(entry_price), _safe(tp_price), _safe(trail_stop_at_resolution),
            band, _safe(adx_at_entry), _safe(rsi_at_entry),
            outcome, _safe(outcome_pnl_pct), candles_elapsed,
        ),
    )


def compute_time_tp_pct(
    symbol: str,
    min_hold_hours: float = 20.0,
    fallback_pct: float = 0.005,
    scale: float = 0.75,
) -> float:
    """Compute a data-driven time-based TP percentage from historical trade data.

    Queries the trades table for the top 3 most profitable EXIT rows (excluding
    LIQUIDATED) for *symbol* where the position was held for at least
    min_hold_hours.  Averages the achieved TP% of those trades, applies scale,
    and returns the result.

    The achieved TP% per trade is:
        (entry_price - fill_price) / entry_price

    Always logs a TIME_TP_COMPUTED event to the DB so every computation is
    fully auditable regardless of whether it used live data or the fallback.

    Falls back to fallback_pct when:
      - The DB is not open yet
      - Fewer than 3 qualifying trades exist
      - The scaled average is not a positive number

    Args:
        symbol:         Trading pair (e.g. "XRPUSDT")
        min_hold_hours: Minimum position hold time in hours to qualify
        fallback_pct:   TP fraction to use when DB data is insufficient
        scale:          Scale factor applied to the data-driven average

    Returns:
        float — TP fraction (e.g. 0.0042 = 0.42% below entry price)
    """
    if _conn is None:
        return fallback_pct

    _ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    try:
        sql = """
            WITH exits AS (
                SELECT
                    e.pnl_net,
                    CAST((e.entry_price - e.fill_price) AS REAL) / e.entry_price
                        AS tp_pct_achieved,
                    (
                        SELECT i.ts_utc
                        FROM   trades i
                        WHERE  i.symbol = e.symbol
                          AND  i.action = 'ENTRY'
                          AND  i.ts_utc < e.ts_utc
                        ORDER  BY i.ts_utc DESC
                        LIMIT  1
                    ) AS entry_ts,
                    e.ts_utc AS exit_ts
                FROM trades e
                WHERE e.symbol = ?
                  AND e.action = 'EXIT'
                  AND e.reason NOT IN ('LIQUIDATED')
                  AND e.pnl_net > 0
                  AND e.entry_price > 0
                  AND e.fill_price  > 0
            )
            SELECT tp_pct_achieved, pnl_net
            FROM   exits
            WHERE  entry_ts IS NOT NULL
              AND  (julianday(exit_ts) - julianday(entry_ts)) * 24.0 >= ?
            ORDER  BY pnl_net DESC
            LIMIT  3
        """
        with _lock:
            cur  = _conn.execute(sql, (symbol, float(min_hold_hours)))
            rows = cur.fetchall()

        top3_tp   = [round(float(r[0]), 8) for r in rows]
        top3_pnl  = [round(float(r[1]), 6) for r in rows]
        n_found   = len(rows)

        if n_found < 3:
            log.debug(
                f"[DB] compute_time_tp_pct({symbol}): {n_found} qualifying "
                f"trade(s) found (need 3) — fallback {fallback_pct:.4f}"
            )
            _execute(
                "INSERT INTO events (ts_utc,level,event_type,symbol,message,detail) "
                "VALUES (?,?,?,?,?,?)",
                (
                    _ts, "INFO", "TIME_TP_COMPUTED", symbol,
                    f"time_tp_pct: {n_found}/3 qualifying trades — fallback {fallback_pct*100:.3f}%",
                    json.dumps({
                        "qualifying_trades_found": n_found,
                        "min_hold_hours": min_hold_hours,
                        "top3_tp_pcts":  top3_tp,
                        "top3_pnl_net":  top3_pnl,
                        "avg_tp_pct":    None,
                        "scale":         scale,
                        "result":        round(fallback_pct, 8),
                        "is_fallback":   True,
                    }),
                ),
            )
            return fallback_pct

        avg_tp = sum(float(r[0]) for r in rows) / 3.0
        scaled = avg_tp * float(scale)

        if scaled <= 0:
            log.debug(
                f"[DB] compute_time_tp_pct({symbol}): scaled {scaled:.6f} <= 0 "
                f"— fallback {fallback_pct:.4f}"
            )
            _execute(
                "INSERT INTO events (ts_utc,level,event_type,symbol,message,detail) "
                "VALUES (?,?,?,?,?,?)",
                (
                    _ts, "INFO", "TIME_TP_COMPUTED", symbol,
                    f"time_tp_pct: scaled result {scaled*100:.4f}% <= 0 — fallback {fallback_pct*100:.3f}%",
                    json.dumps({
                        "qualifying_trades_found": n_found,
                        "min_hold_hours": min_hold_hours,
                        "top3_tp_pcts":  top3_tp,
                        "top3_pnl_net":  top3_pnl,
                        "avg_tp_pct":    round(avg_tp, 8),
                        "scale":         scale,
                        "scaled_before_check": round(scaled, 8),
                        "result":        round(fallback_pct, 8),
                        "is_fallback":   True,
                    }),
                ),
            )
            return fallback_pct

        log.debug(
            f"[DB] compute_time_tp_pct({symbol}): "
            f"top3={top3_tp} avg={avg_tp:.6f} × {scale} = {scaled:.6f}"
        )
        _execute(
            "INSERT INTO events (ts_utc,level,event_type,symbol,message,detail) "
            "VALUES (?,?,?,?,?,?)",
            (
                _ts, "INFO", "TIME_TP_COMPUTED", symbol,
                f"time_tp_pct: {scaled*100:.4f}% "
                f"(avg {avg_tp*100:.4f}% × {scale} from {n_found} trades)",
                json.dumps({
                    "qualifying_trades_found": n_found,
                    "min_hold_hours": min_hold_hours,
                    "top3_tp_pcts":  top3_tp,
                    "top3_pnl_net":  top3_pnl,
                    "avg_tp_pct":    round(avg_tp, 8),
                    "scale":         scale,
                    "result":        round(scaled, 8),
                    "is_fallback":   False,
                }),
            ),
        )
        return float(scaled)

    except Exception as exc:
        log.warning(f"[DB] compute_time_tp_pct failed: {exc}")
        return fallback_pct


# ── Database maintenance ───────────────────────────────────────────────────────

# How many days to retain rows per table.
# High-volume real-time tables are pruned aggressively; trade history is kept
# for a full year so compute_time_tp_pct always has sufficient data.
_RETENTION_DAYS: dict = {
    "mark_price_ticks":    3,    # tick-by-tick mark price — very high volume
    "candles":             30,
    "candle_analytics":    30,
    "signals":             30,
    "positions":           30,
    "events":              60,
    "orders":              90,
    "balance_snapshots":   90,
    "optimization_trials": 14,   # 4 000 rows/run can accumulate fast
    "trades":              365,   # needed by compute_time_tp_pct — keep 1 year
    "params":              365,
    "optimization_runs":   365,
    "monte_carlo_runs":    365,
    "missed_trades":        90,   # what-if shadow position outcomes
}

# Tables that have a ts_utc column used for time-based pruning
_PRUNABLE_TABLES = list(_RETENTION_DAYS.keys())


def run_maintenance(vacuum: bool = False) -> None:
    """Prune stale rows, checkpoint the WAL file, and update query statistics.

    Should be called periodically (e.g. every 24 hours) from a background
    daemon thread.  Never raises — all errors are logged as warnings so the
    trading loop is never affected.

    Args:
        vacuum: If True, also run VACUUM after pruning.  This rebuilds the
                database file and can reclaim significant disk space, but
                takes exclusive write access for several seconds.  Leave
                False during active trading; set True for a nightly deep clean.
    """
    if _conn is None:
        return

    _ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    deleted: dict = {}

    try:
        # ── 1. Prune old rows from each table ─────────────────────────────────
        for table, days in _RETENTION_DAYS.items():
            try:
                cutoff = f"datetime('now', '-{days} days')"
                with _lock:
                    cur = _conn.execute(
                        f"DELETE FROM {table} WHERE ts_utc < {cutoff}"
                    )
                    n = cur.rowcount
                    _conn.commit()
                deleted[table] = n
                if n:
                    log.debug(f"[DB][MAINT] Pruned {n} rows from {table} (>{days}d old)")
            except Exception as tbl_err:
                log.warning(f"[DB][MAINT] Prune failed for {table}: {tbl_err}")
                deleted[table] = -1

        total_deleted = sum(v for v in deleted.values() if v > 0)

        # ── 2. WAL checkpoint — flush pages from the WAL back to the main db ──
        try:
            with _lock:
                _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                _conn.commit()
        except Exception as wal_err:
            log.warning(f"[DB][MAINT] WAL checkpoint failed: {wal_err}")

        # ── 3. Update query planner statistics ────────────────────────────────
        try:
            with _lock:
                _conn.execute("PRAGMA analysis_limit=1000")
                _conn.execute("ANALYZE")
                _conn.commit()
        except Exception as ana_err:
            log.warning(f"[DB][MAINT] ANALYZE failed: {ana_err}")

        # ── 4. Optional full VACUUM ───────────────────────────────────────────
        if vacuum:
            try:
                with _lock:
                    _conn.execute("VACUUM")
            except Exception as vac_err:
                log.warning(f"[DB][MAINT] VACUUM failed: {vac_err}")

        # ── 5. Log the maintenance event ──────────────────────────────────────
        log.info(
            f"[DB][MAINT] Complete — pruned {total_deleted} total rows, "
            f"WAL checkpoint done, ANALYZE done"
            + (" + VACUUM" if vacuum else "")
        )
        _execute(
            "INSERT INTO events (ts_utc,level,event_type,symbol,message,detail) "
            "VALUES (?,?,?,?,?,?)",
            (
                _ts, "INFO", "DB_MAINTENANCE", None,
                f"Pruned {total_deleted} rows across {len(deleted)} tables",
                json.dumps({
                    "rows_deleted_per_table": deleted,
                    "total_deleted":          total_deleted,
                    "vacuum_run":             vacuum,
                    "retention_policy":       _RETENTION_DAYS,
                }),
            ),
        )

    except Exception as exc:
        log.warning(f"[DB][MAINT] Maintenance failed: {exc}")
