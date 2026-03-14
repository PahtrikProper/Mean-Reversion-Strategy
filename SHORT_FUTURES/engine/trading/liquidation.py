"""Liquidation Calculations - SHORT liquidation only."""

import pandas as pd

def pick_risk_tier(risk_df: pd.DataFrame, position_value_mark: float) -> pd.Series:
    """
    Select the appropriate Bybit risk tier for a given position size.

    Bybit uses tiered risk limits: each tier has a maximum notional value,
    a maintenance margin rate (MMR), and an mmDeduction offset.  Larger positions
    require higher margin.  We pick the smallest tier whose riskLimitValue can
    accommodate the position's notional value (qty * mark_price).

    Falls back to the highest tier if the position exceeds all limits.

    Used by both the backtest liquidation check and live position monitoring.
    """
    pv = float(position_value_mark)
    mask = risk_df["riskLimitValue"] >= pv
    if mask.any():
        # iloc[0] on the filtered frame picks the first (smallest) eligible tier.
        # Using idxmax() on a boolean mask would also work with a sorted RangeIndex
        # but is semantically misleading — iloc[0] is explicit and correct.
        return risk_df[mask].iloc[0]
    return risk_df.iloc[-1]

# ----------------------------
# EXACT LIQUIDATION (ISOLATED, SHORT, LINEAR USDT)
# ----------------------------
def liquidation_price_short_isolated(
    entry_price: float,
    qty_short: float,                 # negative qty for short
    leverage: float,
    mark_price: float,
    tier: pd.Series,
    fee_rate: float,
    extra_margin_added: float = 0.0
) -> float:
    """
    Exact Bybit isolated SHORT liquidation price (USDT linear perpetual).

      LP = Entry + (IM + ExtraMargin - MM) / |Qty|

    Bybit uses ENTRY price for IM and MARK price for MM:

      IM  = |Qty| * Entry / Leverage
            (no trading fee — entry fee is paid upfront and does not enter the
            margin calculation that sets the liquidation trigger)

      MM  = max(0, |Qty| * Mark * MMR - mmDeduction + EstCloseFee)
            (mark-price based: as mark rises toward LP, MM grows and LP falls,
            consistent with Bybit's real-time LP recalculation behaviour)

      EstCloseFee = |Qty| * Mark * fee_rate
            (taker fee to close the position, charged at current mark price)

    Notes:
    - Tier must be selected on position value at MARK price (done outside).
    - Liquidation trigger compares MARK price against LP (done in backtest).
    - MM is clamped to 0: a negative MM would widen LP beyond the bankruptcy
      price, which Bybit prevents by treating it as zero.
    """
    qty_abs = abs(float(qty_short))
    if qty_abs <= 0:
        return float("inf")

    entry = float(entry_price)
    mark = float(mark_price)
    lev = float(leverage)

    pv_entry = qty_abs * entry          # position value at entry (IM basis)
    pv_mark  = qty_abs * mark           # position value at mark  (MM basis)
    est_close_fee = pv_mark * float(fee_rate)

    # Initial Margin: entry-price based, no fees
    im = pv_entry / lev

    mmr    = float(tier["maintenanceMarginRate"])
    mm_ded = float(tier["mmDeductionValue"])

    # Maintenance Margin: mark-price based, clamped to zero
    mm = max(0.0, (pv_mark * mmr) - mm_ded + est_close_fee)

    lp = entry + ((im + float(extra_margin_added)) - mm) / qty_abs
    return float(lp)

