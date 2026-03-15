"""Bybit API Client"""

import requests
import hmac
import hashlib
import urllib.parse
import time
import logging
import json
import uuid
import pandas as pd
import numpy as np
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass

# Import constants module (not direct values, to allow dynamic updates)
from ..utils import constants
from ..utils.constants import (
    RECV_WINDOW,
    REST_MIN_INTERVAL_SEC,
    API_POLITE_SLEEP,
    CATEGORY,
    FEE_RATE,
    MAKER_FEE_RATE,
)

# Import data structures
from ..utils.data_structures import RealPosition

# Import helpers
from ..utils.helpers import _rate_limit_rest, now_ms, leverage_for

# Bybit API endpoints
BASE_REST = "https://api.bybit.com"

# Module-level globals
_last_rest_call_time = 0.0
log = logging.getLogger("bybit_client")

def rest_get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Unauthenticated GET to Bybit public REST API.  Used for market data
    endpoints (klines, tickers, risk-limit, instruments-info).
    Automatically rate-limited.  Raises RuntimeError on non-zero retCode."""
    url = BASE_REST + path
    _rate_limit_rest()
    r = requests.get(url, params=params, timeout=30)

    # Check for 401 Unauthorized
    if r.status_code == 401:
        log.error("Received 401 Unauthorized - API credentials may be invalid")
        if constants.API_KEY.startswith("YOUR_") or constants.API_SECRET.startswith("YOUR_"):
            raise RuntimeError(
                "Bybit API returned 401 Unauthorized.\n"
                "Your credentials are using placeholder values.\n"
                "Please set valid BYBIT_API_KEY and BYBIT_API_SECRET environment variables."
            )
        raise RuntimeError(f"Bybit API returned 401 Unauthorized. Check your API credentials.")

    try:
        j = r.json()
    except ValueError as exc:
        log.error(
            "Bybit REST non-JSON response status=%s body=%s",
            r.status_code,
            r.text
        )
        raise RuntimeError("Bybit REST returned non-JSON response.") from exc
    if "retCode" not in j:
        raise RuntimeError(f"Bybit REST unexpected response: {j}")
    if j["retCode"] != 0:
        raise RuntimeError(f"Bybit REST error retCode={j['retCode']} retMsg={j.get('retMsg')} params={params}")
    return j

def _sign_request(timestamp: str, api_key: str, recv_window: str, payload: str) -> str:
    """Generate HMAC-SHA256 signature for Bybit V5 authenticated requests.
    The signing string is: timestamp + api_key + recv_window + payload
    (payload = query string for GET, JSON body for POST)."""
    raw = f"{timestamp}{api_key}{recv_window}{payload}"
    return hmac.new(constants.API_SECRET.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()

def _build_query(params: Dict[str, Any]) -> str:
    """URL-encode params with keys sorted alphabetically (required by Bybit signing)."""
    return urllib.parse.urlencode({k: params[k] for k in sorted(params)})

def rest_request(
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict[str, Any]] = None,
    auth: bool = False
) -> Dict[str, Any]:
    """General-purpose Bybit REST request handler (GET or POST).
    Supports both authenticated (auth=True) and unauthenticated calls.
    When auth=True, adds HMAC-SHA256 signature headers required by Bybit V5.
    Handles rate limiting, JSON parsing, and retCode error checking.
    Specific retCodes (110043=leverage unchanged, 10001) are logged as warnings."""
    url = BASE_REST + path
    params = params or {}
    body = body or {}

    if method.upper() == "GET":
        query = _build_query(params)
        full_url = f"{url}?{query}" if query else url
        payload = query
        headers = {}
        data = None
    else:
        full_url = url
        payload = json.dumps(body, separators=(",", ":"))
        headers = {"Content-Type": "application/json"}
        data = payload

    if auth:
        timestamp = str(now_ms())
        if not constants.API_KEY or not constants.API_SECRET:
            raise RuntimeError("Missing BYBIT_API_KEY or BYBIT_API_SECRET for authenticated requests.")

        # Check for placeholder credentials
        if constants.API_KEY.startswith("YOUR_") or constants.API_SECRET.startswith("YOUR_"):
            raise RuntimeError(
                "Bybit API credentials are using placeholder values.\n"
                "Please set BYBIT_API_KEY and BYBIT_API_SECRET environment variables "
                "or run the application to be prompted for credentials."
            )

        signature = _sign_request(timestamp, constants.API_KEY, RECV_WINDOW, payload)
        headers.update({
            "X-BAPI-API-KEY": constants.API_KEY,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": RECV_WINDOW,
        })

    if method.upper() == "GET":
        _rate_limit_rest()
        r = requests.get(full_url, headers=headers, timeout=30)
    else:
        _rate_limit_rest()
        r = requests.post(full_url, headers=headers, data=data, timeout=30)

    # Check for 401 Unauthorized
    if r.status_code == 401:
        log.error("Received 401 Unauthorized from Bybit - authentication failed")
        if auth and (constants.API_KEY.startswith("YOUR_") or constants.API_SECRET.startswith("YOUR_")):
            raise RuntimeError(
                "Bybit API returned 401 Unauthorized.\n"
                "Your credentials are using placeholder values.\n"
                "Please set valid BYBIT_API_KEY and BYBIT_API_SECRET environment variables "
                "or provide them when prompted at startup."
            )
        raise RuntimeError(f"Bybit API returned 401 Unauthorized. Check your API credentials.")

    try:
        j = r.json()
    except ValueError as exc:
        log.error(
            "Bybit REST non-JSON response status=%s body=%s",
            r.status_code,
            r.text
        )
        raise RuntimeError("Bybit REST returned non-JSON response.") from exc
    if "retCode" not in j:
        log.error("Bybit REST unexpected response: %s", j)
        raise RuntimeError(f"Bybit REST unexpected response: {j}")
    if j["retCode"] != 0:
        ret_code = j.get("retCode")
        ret_msg = j.get("retMsg")
        if ret_code in (110043, 10001):
            log.warning(
                "Bybit REST warning retCode=%s retMsg=%s params=%s body=%s",
                ret_code,
                ret_msg,
                params,
                body
            )
        else:
            log.error(
                "Bybit REST error retCode=%s retMsg=%s params=%s body=%s",
                ret_code,
                ret_msg,
                params,
                body
            )
        raise RuntimeError(f"Bybit REST error retCode={j['retCode']} retMsg={j.get('retMsg')} params={params} body={body}")
    return j

# ============================================================
# DATA DOWNLOAD (FIXED: SEPARATE PARSERS)
# ============================================================
def fetch_last_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """
    /v5/market/kline  -> returns 7 columns
    [start, open, high, low, close, volume, turnover]
    """
    path = "/v5/market/kline"
    out = []
    cur = end_ms

    while True:
        j = rest_get(path, {
            "category": CATEGORY,
            "symbol": symbol,
            "interval": interval,
            "end": cur,
            "limit": 1000
        })
        rows = j["result"]["list"]
        if not rows:
            break

        rows = sorted(rows, key=lambda x: int(x[0]))

        for r in rows:
            ts = int(r[0])
            if start_ms <= ts <= end_ms:
                out.append(r)

        earliest = int(rows[0][0])
        if earliest <= start_ms:
            break

        cur = earliest - 1
        time.sleep(API_POLITE_SLEEP)

    if not out:
        raise RuntimeError("No last-price klines returned")

    df = pd.DataFrame(out, columns=["ts","open","high","low","close","volume","turnover"])
    df["ts"] = pd.to_datetime(df["ts"].astype(np.int64), unit="ms", utc=True)
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    df[["open","high","low","close","volume","turnover"]] = df[["open","high","low","close","volume","turnover"]].astype(float)
    return df

def fetch_mark_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """
    For spot: mark price doesn't exist — return last-price klines instead.
    For linear: /v5/market/mark-price-kline -> [start, open, high, low, close]
    """
    if CATEGORY == "spot":
        return fetch_last_klines(symbol, interval, start_ms, end_ms)

    path = "/v5/market/mark-price-kline"
    out = []
    cur = end_ms

    while True:
        j = rest_get(path, {
            "category": CATEGORY,
            "symbol": symbol,
            "interval": interval,
            "end": cur,
            "limit": 1000
        })
        rows = j["result"]["list"]
        if not rows:
            break

        rows = sorted(rows, key=lambda x: int(x[0]))

        for r in rows:
            ts = int(r[0])
            if start_ms <= ts <= end_ms:
                out.append(r)

        earliest = int(rows[0][0])
        if earliest <= start_ms:
            break

        cur = earliest - 1
        time.sleep(API_POLITE_SLEEP)

    if not out:
        raise RuntimeError("No mark-price klines returned")

    df = pd.DataFrame(out, columns=["ts","open","high","low","close"])
    df["ts"] = pd.to_datetime(df["ts"].astype(np.int64), unit="ms", utc=True)
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    df[["open","high","low","close"]] = df[["open","high","low","close"]].astype(float)
    return df

def fetch_risk_tiers(symbol: str) -> pd.DataFrame:
    """
    /v5/market/risk-limit
    Returns tiered MMR and mmDeduction used in liquidation.
    For spot: liquidation does not exist — returns an empty DataFrame.
    """
    if CATEGORY == "spot":
        return pd.DataFrame(columns=["riskLimitValue", "maintenanceMarginRate", "mmDeductionValue"])

    j = rest_get("/v5/market/risk-limit", {
        "category": CATEGORY,
        "symbol": symbol
    })

    rows = j["result"]["list"]
    if not rows:
        raise RuntimeError("No risk tiers returned")

    df = pd.DataFrame(rows)
    df["riskLimitValue"] = df["riskLimitValue"].astype(float)
    # Bybit V5 uses "maintenanceMargin"; fall back to "maintainMargin" for API variants
    _mm_field = "maintenanceMargin" if "maintenanceMargin" in df.columns else "maintainMargin"
    if _mm_field not in df.columns:
        raise RuntimeError(
            f"fetch_risk_tiers: maintenance margin field not found. "
            f"Available columns: {list(df.columns)}"
        )
    df["maintenanceMarginRate"] = df[_mm_field].astype(float) / 100.0

    def _parse_mm_deduction(x):
        """Safely parse mmDeduction to float; returns 0.0 if missing or unparseable."""
        try:
            return float(x)
        except Exception:
            return 0.0

    df["mmDeductionValue"] = df["mmDeduction"].apply(_parse_mm_deduction)
    df = df.sort_values("riskLimitValue").reset_index(drop=True)
    return df

def get_instrument_info(symbol: str) -> Dict[str, Any]:
    """Fetch instrument specification (lot size, tick size, min notional, etc.) via the
    public instruments-info endpoint.  No authentication required.
    Used by PaperTrader at startup instead of BybitPrivateClient.get_instrument_info()."""
    j = rest_get("/v5/market/instruments-info", {
        "category": CATEGORY,
        "symbol": symbol,
    })
    rows = j["result"]["list"]
    if not rows:
        raise RuntimeError(f"No instrument info returned for {symbol}")
    return rows[0]


def fetch_last_price(symbol: str) -> float:
    """Fetch the current last-traded price for a symbol via the tickers endpoint.
    Used as a fallback price reference."""
    from ..utils.constants import CATEGORY
    j = rest_get("/v5/market/tickers", {
        "category": CATEGORY,
        "symbol": symbol
    })
    rows = j["result"]["list"]
    if not rows:
        raise RuntimeError("No tickers returned")
    return float(rows[0]["lastPrice"])


def _parse_balance_value(value: Any) -> Optional[float]:
    """Safely parse a Bybit balance field string to float.
    Returns None if the value is None, an empty string, or unparseable.
    Bybit may return balances as strings, nulls, or empty strings."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return float(value)
    except Exception:
        return None


class BybitPrivateClient:
    """Authenticated Bybit V5 REST client for all private operations:
    wallet balance queries, position queries, order placement, leverage/margin
    configuration, fee rate lookup, execution polling, and TP management.

    All methods use rest_request() with auth=True which handles HMAC signing.
    Designed for Unified Trading Account (UTA) on USDT linear perpetuals."""

    def get_unified_usdt(self) -> float:
        """Fetch available USDT balance from the Unified (trading) account.
        Tries fields in order: availableToWithdraw → availableBalance → walletBalance.
        This is the balance available for placing new positions."""
        j = rest_request(
            "GET",
            "/v5/account/wallet-balance",
            params={"accountType": "UNIFIED"},
            auth=True
        )
        rows = j["result"].get("list", [])
        if not rows:
            raise RuntimeError("No wallet balance returned for accountType=UNIFIED")
        for row in rows:
            for coin in row.get("coin", []):
                if coin.get("coin") != "USDT":
                    continue
                log.info("USDT wallet object: %s", coin)
                available_withdraw = coin.get("availableToWithdraw")
                available_balance = coin.get("availableBalance")
                wallet_balance = coin.get("walletBalance")

                parsed_available_withdraw = _parse_balance_value(available_withdraw)
                if parsed_available_withdraw is not None and parsed_available_withdraw > 0:
                    return parsed_available_withdraw

                parsed_available_balance = _parse_balance_value(available_balance)
                if parsed_available_balance is not None and parsed_available_balance > 0:
                    return parsed_available_balance

                parsed_wallet_balance = _parse_balance_value(wallet_balance)
                if parsed_wallet_balance is not None:
                    return parsed_wallet_balance

                raise RuntimeError("No usable USDT balance fields returned by Bybit")
        raise RuntimeError("USDT balance not found for accountType=UNIFIED")

    def get_fund_usdt(self) -> Optional[float]:
        """Fetch available USDT from the Funding (spot) account.
        Returns None if the account type doesn't support FUND wallet
        (some Unified accounts have no separate FUND wallet).
        Used by ensure_unified_balance() to auto-transfer funds if needed."""
        try:
            j = rest_request(
                "GET",
                "/v5/account/wallet-balance",
                params={"accountType": "FUND"},
                auth=True
            )
        except RuntimeError as exc:
            if "accountType only support UNIFIED" in str(exc):
                log.warning("FUND wallet not accessible on this account. Skipping transfer logic.")
                return None
            raise

        rows = j["result"].get("list", [])
        if not rows:
            raise RuntimeError("No wallet balance returned for accountType=FUND")
        for row in rows:
            for coin in row.get("coin", []):
                if coin.get("coin") != "USDT":
                    continue
                log.info("FUND USDT wallet object: %s", coin)
                available_withdraw = coin.get("availableToWithdraw")
                available_balance = coin.get("availableBalance")
                wallet_balance = coin.get("walletBalance")

                parsed_available_withdraw = _parse_balance_value(available_withdraw)
                if parsed_available_withdraw is not None and parsed_available_withdraw > 0:
                    return parsed_available_withdraw

                parsed_available_balance = _parse_balance_value(available_balance)
                if parsed_available_balance is not None and parsed_available_balance > 0:
                    return parsed_available_balance

                parsed_wallet_balance = _parse_balance_value(wallet_balance)
                if parsed_wallet_balance is not None:
                    return parsed_wallet_balance

                raise RuntimeError("No usable USDT balance fields returned by Bybit for FUND")
        raise RuntimeError("USDT balance not found for accountType=FUND")

    def transfer_fund_to_unified(self, amount: float) -> None:
        """Internal transfer of USDT from Funding wallet to Unified (trading) wallet.
        Called automatically when the Unified balance is insufficient at startup."""
        if amount <= 0:
            return
        result = rest_request(
            "POST",
            "/v5/asset/transfer/inter-transfer",
            body={
                "fromAccountType": "FUND",
                "toAccountType": "UNIFIED",
                "coin": "USDT",
                "amount": f"{amount:.8f}",
            },
            auth=True
        )
        transfer_id = result.get("result", {}).get("transferId")
        if not transfer_id:
            log.warning("transfer_fund_to_unified: no transferId in response — transfer may not have completed")

    def ensure_unified_balance(self, required_amount: float) -> float:
        """Ensure the Unified wallet has at least required_amount USDT.
        If insufficient, auto-transfers from the Funding wallet.
        Returns the final Unified balance after any transfer.
        Called once at startup before optimization begins."""
        unified = self.get_unified_usdt()
        log.info("Unified available USDT balance: %.2f", unified)
        log.info("Required margin (USDT): %.2f", required_amount)

        if unified >= required_amount:
            return unified

        fund = self.get_fund_usdt()
        if fund is None:
            raise RuntimeError("Unified balance insufficient and FUND wallet not accessible")
        log.info("Funding available USDT balance: %.2f", fund)
        if fund <= 0:
            raise RuntimeError("Not enough funds in FUND or UNIFIED")

        need = required_amount - unified
        to_transfer = min(need, fund)
        log.info("Transferring %.2f USDT from FUND to UNIFIED.", to_transfer)
        self.transfer_fund_to_unified(to_transfer)
        time.sleep(2)

        unified_after = self.get_unified_usdt()
        log.info("Unified USDT balance after transfer: %.2f", unified_after)
        if unified_after < required_amount:
            raise RuntimeError("Auto-transfer failed to fund Unified wallet")
        return unified_after

    def set_position_mode(self, symbol: str, mode: int = 0):
        """Set position mode: 0 = one-way (merged), 3 = hedge (buy/sell separate).
        This bot uses one-way mode (mode=0).  Failures are silently warned
        because Unified accounts may not allow mode changes via API."""
        try:
            rest_request(
                "POST",
                "/v5/position/switch-mode",
                body={
                    "category": CATEGORY,
                    "symbol": symbol,
                    "mode": mode
                },
                auth=True
            )
        except Exception as exc:
            log.warning("set_position_mode skipped for %s: %s", symbol, exc)

    def set_margin_mode(self, symbol: str, trade_mode: int = 1):
        """Set margin mode: 0 = cross, 1 = isolated.  This bot uses isolated margin
        (trade_mode=1) which confines liquidation risk to the position's margin.
        Also sets buy/sell leverage as part of the same request."""
        try:
            leverage = leverage_for(symbol)
            rest_request(
                "POST",
                "/v5/position/set-margin-mode",
                body={
                    "category": CATEGORY,
                    "symbol": symbol,
                    "tradeMode": trade_mode,
                    "buyLeverage": str(leverage),
                    "sellLeverage": str(leverage)
                },
                auth=True
            )
        except Exception as exc:
            log.warning("set_margin_mode skipped for %s: %s", symbol, exc)

    def set_leverage(self, symbol: str, buy_leverage: float, sell_leverage: float):
        """Set buy and sell leverage for a symbol.  Called at startup via ensure_futures_setup().
        Silently skips if leverage is already at the requested value (retCode 110043)."""
        try:
            rest_request(
                "POST",
                "/v5/position/set-leverage",
                body={
                    "category": CATEGORY,
                    "symbol": symbol,
                    "buyLeverage": str(buy_leverage),
                    "sellLeverage": str(sell_leverage)
                },
                auth=True
            )
        except RuntimeError as exc:
            msg = str(exc)
            if "110043" in msg or "not modified" in msg or "10001" in msg:
                log.info("Leverage already set or cannot be changed for %s. Skipping.", symbol)
                return
            raise

    def ensure_futures_setup(self, symbol: str, leverage: float = 1.0):
        """One-time setup for a symbol before live trading begins.
        For spot margin: sets buy/sell leverage via set_leverage.
        For linear: sets leverage (margin mode must be set in Bybit UI)."""
        lev = leverage if leverage > 1.0 else leverage_for(symbol)
        self.set_leverage(symbol, lev, lev)

    def get_wallet_balance(self) -> float:
        """Alias for get_unified_usdt(). Provided for interface consistency."""
        return self.get_unified_usdt()

    def get_leverage(self, symbol: str) -> float:
        """Read the leverage currently configured on Bybit for a symbol.
        Uses /v5/position/list which returns the leverage field even with no
        open position.  Falls back to the local constant if the query fails."""
        try:
            j = rest_request(
                "GET",
                "/v5/position/list",
                params={"category": CATEGORY, "symbol": symbol},
                auth=True
            )
            rows = j["result"].get("list", [])
            if rows:
                lev_str = rows[0].get("leverage", "")
                if lev_str:
                    return float(lev_str)
        except Exception as exc:
            log.warning("get_leverage(%s) failed: %s — using local constant", symbol, exc)
        return leverage_for(symbol)

    def get_position(self, symbol: str) -> Optional[RealPosition]:
        """Query the current open position for a symbol.
        For spot: position is tracked locally by the bot — always returns None.
        For linear: queries /v5/position/list; returns None if no position (size == 0)."""
        if CATEGORY == "spot":
            return None
        j = rest_request(
            "GET",
            "/v5/position/list",
            params={"category": CATEGORY, "symbol": symbol},
            auth=True
        )
        rows = j["result"]["list"]
        if not rows:
            return None
        row = rows[0]
        size = float(row.get("size", 0))
        if size == 0:
            return None
        side = row.get("side", "")
        entry_price = float(row.get("avgPrice", 0))
        qty = size if side == "Buy" else -size
        liq_price = None
        liq_str = row.get("liqPrice", "")
        if liq_str:
            try:
                lp = float(liq_str)
                if lp > 0:
                    liq_price = lp
            except (ValueError, TypeError):
                pass
        return RealPosition(qty=qty, entry_price=entry_price, side=side, liq_price=liq_price)

    def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        reduce_only: bool,
        order_link_id: Optional[str] = None
    ) -> str:
        """Place an IOC (immediate-or-cancel) market order on Bybit.
        For spot: positionIdx and reduceOnly are not applicable — omitted.
        For linear: reduce_only=True restricts the order to closing an existing position.
        Returns the Bybit orderId string."""
        link_id = order_link_id or f"DLT-{int(time.time() * 1000)}-{uuid.uuid4().hex[:10]}"
        body: Dict[str, Any] = {
            "category": CATEGORY,
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": str(qty),
            "timeInForce": "IOC",
            "orderLinkId": link_id,
        }
        if CATEGORY == "spot" and side.lower() == "buy":
            # isLeverage=1 activates spot margin borrowing (LONG entry only).
            # Must NOT be sent on Sell (exit) orders — would enable short-selling,
            # violating the LONG-only constraint of this strategy.
            body["isLeverage"] = "1"
        elif CATEGORY != "spot":
            body["positionIdx"] = 0
            body["reduceOnly"] = reduce_only
        j = rest_request("POST", "/v5/order/create", body=body, auth=True)
        return j["result"]["orderId"]

    def place_limit_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        reduce_only: bool,
        take_profit: Optional[float] = None,
        order_link_id: Optional[str] = None
    ) -> str:
        """Place a PostOnly limit order on Bybit (maker only).
        Used for entries — ensures the order is placed on the book and never
        crosses the spread, guaranteeing maker fee rates.  If the price would
        cross, Bybit rejects the order immediately.
        For spot: positionIdx and reduceOnly are not applicable — omitted.
        Optionally includes a server-side take-profit (LastPrice triggered).
        Returns the Bybit orderId string."""
        link_id = order_link_id or f"DLT-{int(time.time() * 1000)}-{uuid.uuid4().hex[:10]}"
        body: Dict[str, Any] = {
            "category": CATEGORY,
            "symbol": symbol,
            "side": side,
            "orderType": "Limit",
            "qty": str(qty),
            "price": f"{price:.8f}",
            "timeInForce": "PostOnly",
            "orderLinkId": link_id,
        }
        if CATEGORY != "spot":
            body["positionIdx"] = 0
            body["reduceOnly"] = reduce_only
        if take_profit is not None:
            body["takeProfit"] = f"{take_profit:.8f}"
            body["tpTriggerBy"] = "LastPrice"
        j = rest_request("POST", "/v5/order/create", body=body, auth=True)
        return j["result"]["orderId"]

    def get_order_status(self, symbol: str, order_id: str) -> Optional[Dict[str, Any]]:
        """Fetch the status of a specific order from order history.
        Returns the order dict with fields like orderStatus, cumExecQty, avgPrice.
        Used as a fallback by get_execution_summary() when execution records aren't found."""
        j = rest_request(
            "GET",
            "/v5/order/history",
            params={
                "category": CATEGORY,
                "symbol": symbol,
                "orderId": order_id,
                "limit": 1
            },
            auth=True
        )
        rows = j["result"].get("list", [])
        return rows[0] if rows else None

    def get_instrument_info(self, symbol: str) -> Dict[str, Any]:
        """Fetch instrument specification (lot size filter, tick size, min notional, etc.)
        from Bybit.  Used to validate and format order quantities in _format_qty()."""
        j = rest_get("/v5/market/instruments-info", {
            "category": CATEGORY,
            "symbol": symbol
        })
        rows = j["result"]["list"]
        if not rows:
            raise RuntimeError("No instrument info returned")
        return rows[0]

    def get_fee_rates(self, symbol: str) -> Tuple[float, float]:
        """Query the account's actual taker and maker fee rates for a symbol from Bybit.
        Returns (taker_fee_rate, maker_fee_rate).  Both are stored in their respective
        global dicts for the session and used in PnL calculations."""
        j = rest_request(
            "GET",
            "/v5/account/fee-rate",
            params={"category": CATEGORY, "symbol": symbol},
            auth=True
        )
        rows = j["result"].get("list", [])
        if not rows:
            raise RuntimeError(f"No fee rate returned for {symbol}")
        row = rows[0]

        def _parse(key: str, fallback: float) -> float:
            value = row.get(key)
            if value is None:
                return fallback
            try:
                return float(value)
            except Exception:
                return fallback

        taker = _parse("takerFeeRate", FEE_RATE)
        maker = _parse("makerFeeRate", MAKER_FEE_RATE)
        return taker, maker

    def set_trading_stop(self, symbol: str, take_profit: float) -> None:
        """Set a server-side take-profit on an open position via Bybit's trading-stop API.
        For spot: not supported — no-op (TP is managed client-side in the bot loop).
        For linear: uses LastPrice trigger; tpslMode='Full' required for Unified accounts."""
        if CATEGORY == "spot":
            return
        # Format as a clean string: strip trailing zeros but keep at least 1 decimal place
        # (take_profit is already tick-rounded by _format_price in live_trader).
        tp_str = f"{take_profit:.10f}".rstrip("0")
        if "." not in tp_str:
            tp_str += ".0"
        elif tp_str.endswith("."):
            tp_str += "0"
        rest_request(
            "POST",
            "/v5/position/trading-stop",
            body={
                "category": CATEGORY,
                "symbol": symbol,
                "takeProfit": tp_str,
                "tpTriggerBy": "LastPrice",
                "tpslMode": "Full",
                "positionIdx": 0
            },
            auth=True
        )

    def get_last_closed_pnl(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Fetch the most recent closed-position PnL record for symbol.

        Uses /v5/position/closed-pnl which returns one entry per full close.
        Useful for reconstructing exit details after a server TP, liquidation,
        or manual close triggered outside the bot (e.g. via Bybit app).

        Returned dict includes:
            avgEntryPrice, avgExitPrice, closedSize, closedPnl, side, leverage.
        Returns None if no record found or if the API call fails."""
        try:
            j = rest_request(
                "GET",
                "/v5/position/closed-pnl",
                params={
                    "category": CATEGORY,
                    "symbol": symbol,
                    "limit": 1,
                },
                auth=True,
            )
            rows = j["result"].get("list", [])
            return rows[0] if rows else None
        except Exception as exc:
            log.warning("get_last_closed_pnl(%s): %s", symbol, exc)
            return None

    def cancel_order(self, symbol: str, order_id: str) -> None:
        """Cancel an unfilled or partially filled order.
        Used to clean up PostOnly limit orders that haven't fully filled within the timeout."""
        try:
            rest_request(
                "POST",
                "/v5/order/cancel",
                body={
                    "category": CATEGORY,
                    "symbol": symbol,
                    "orderId": order_id,
                },
                auth=True
            )
        except RuntimeError as exc:
            msg = str(exc)
            if "110001" in msg or "order not exists" in msg.lower():
                log.info("Cancel skipped for %s — order already filled/cancelled.", order_id)
                return
            raise

    def _fetch_executions(self, symbol: str, order_id: str) -> List[Dict[str, Any]]:
        """Paginate through all execution (fill) records for a specific order.
        Bybit may split a single order into multiple partial fills — this collects
        all of them.  Used internally by get_execution_summary()."""
        cursor = None
        executions: List[Dict[str, Any]] = []
        while True:
            params = {
                "category": CATEGORY,
                "symbol": symbol,
                "orderId": order_id,
                "limit": 50
            }
            if cursor:
                params["cursor"] = cursor
            j = rest_request(
                "GET",
                "/v5/execution/list",
                params=params,
                auth=True
            )
            rows = j["result"]["list"]
            if rows:
                executions.extend(rows)
            cursor = j["result"].get("nextPageCursor")
            if not cursor:
                break
        return executions

    def get_execution_summary(
        self,
        symbol: str,
        order_id: str,
        timeout_sec: float = 10.0,
        poll_interval: float = 0.3
    ) -> Optional[Dict[str, float]]:
        """Poll for execution records and compute the volume-weighted average fill.
        Returns {'avg_price': float, 'qty': float} once fills are found, or None
        if no fills appear within timeout_sec.

        Timeout defaults to 10s to accommodate PostOnly orders which may sit on
        the book briefly before being filled.  Falls back to order history
        (cumExecQty/avgPrice) if execution records aren't found."""
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            executions = self._fetch_executions(symbol, order_id)
            if executions:
                total_qty = 0.0
                total_notional = 0.0
                for row in executions:
                    exec_qty = float(row.get("execQty", 0) or 0)
                    exec_price = float(row.get("execPrice", 0) or 0)
                    if exec_qty <= 0 or exec_price <= 0:
                        continue
                    total_qty += exec_qty
                    total_notional += exec_qty * exec_price
                if total_qty > 0:
                    return {
                        "avg_price": total_notional / total_qty,
                        "qty": total_qty
                    }
            time.sleep(poll_interval)
        order = self.get_order_status(symbol, order_id)
        if order:
            status = order.get("orderStatus")
            filled = float(order.get("cumExecQty", 0) or 0)
            avg = float(order.get("avgPrice", 0) or 0)
            log.warning(
                "Order %s status=%s cumExecQty=%s avgPrice=%s",
                order_id,
                status,
                filled,
                avg
            )
            if filled > 0 and avg > 0:
                return {"avg_price": avg, "qty": filled}
        return None

