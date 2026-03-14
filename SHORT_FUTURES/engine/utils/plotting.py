"""Plotting Utilities"""

import logging
from typing import List
import numpy as np
from .data_structures import MCSimResult, _MC_RESET, _MC_BOLD, _MC_GREEN, _MC_RED, _MC_CYAN, _MC_WHITE, _MC_DIM

log = logging.getLogger(__name__)

def plot_pnl_chart(
    wallet_history: List[float],
    starting_wallet: float,
    interval_minutes: int = 15,
    max_width: int = 80,
    max_height: int = 20,
):
    """
    Draw an ASCII equity-curve chart with a day-labeled x-axis.
    wallet_history:    list of wallet values (one per candle)
    interval_minutes:  candle size in minutes (e.g. 15 for 15m candles)
    """
    if not wallet_history or len(wallet_history) < 2:
        return

    pnl_history = [(w - starting_wallet) / starting_wallet * 100 for w in wallet_history]

    min_pnl = min(pnl_history)
    max_pnl = max(pnl_history)

    if min_pnl == max_pnl:
        print(f"\n[PnL Chart] Flat: {pnl_history[-1]:.2f}%\n")
        return

    pnl_range = max_pnl - min_pnl
    if pnl_range < 0.01:
        pnl_range = 0.01

    # Downsample if too many points
    step = max(1, len(pnl_history) // max_width)
    sampled = pnl_history[::step]
    chart_width = len(sampled)

    # Normalize to chart height
    chart_data = [int((p - min_pnl) / pnl_range * (max_height - 1)) for p in sampled]

    # Zero line row (if 0% is within the y range)
    zero_row = None
    if min_pnl <= 0.0 <= max_pnl:
        zero_row = int((0.0 - min_pnl) / pnl_range * (max_height - 1))

    # ── Draw chart body ──
    print("\n" + "=" * (chart_width + 2))
    for row in range(max_height - 1, -1, -1):
        line = "│"
        for height in chart_data:
            if height == row:
                line += "█"
            elif zero_row is not None and row == zero_row:
                line += "·"
            else:
                line += " "
        line += "│"
        if row == max_height - 1:
            line += f" {max_pnl:+.2f}%"
        elif zero_row is not None and row == zero_row:
            line += "  0.00%"
        elif row == 0:
            line += f" {min_pnl:+.2f}%"
        print(line)

    # ── X-axis: day ticks and labels ──
    candles_per_day = 1440.0 / max(1, interval_minutes)
    total_candles = len(pnl_history)
    total_days = total_candles / candles_per_day

    tick_row  = list("─" * chart_width)
    label_row = list(" " * chart_width)

    if total_days >= 1:
        # Choose a day step so labels don't crowd (aim for at most chart_width/4 ticks)
        day_step = 1
        while (total_days / day_step) > (chart_width / 4):
            day_step += 1

        day = 0
        while day <= total_days:
            col = int(day * candles_per_day) // step
            if col < chart_width:
                tick_row[col] = "┬"
                label = str(int(day))
                start = col - len(label) // 2
                for k, ch in enumerate(label):
                    lc = start + k
                    if 0 <= lc < chart_width:
                        label_row[lc] = ch
            day += day_step

    print("└" + "".join(tick_row) + "┘")
    print(" " + "".join(label_row) + "  (days)")
    print(f"Current PnL: {pnl_history[-1]:+.2f}% | Max: {max_pnl:+.2f}% | Min: {min_pnl:+.2f}% | Span: {total_days:.1f} days\n")



def _mc_bar(fraction: float, width: int = 30) -> str:
    filled = max(0, min(width, int(round(fraction * width))))
    return f"{'█' * filled}{'░' * (width - filled)}"

def print_monte_carlo_report(
    results: List[MCSimResult],
    starting_wallet: float,
    n_source_trades: int,
    symbol: str = "",
    interval: str = ""
):
    """Print a formatted Monte Carlo report to the terminal."""
    n = len(results)
    wallets = np.array([r.final_wallet for r in results])
    pnls = np.array([r.pnl_usdt for r in results])
    pnl_pcts = np.array([r.pnl_pct for r in results])
    dds = np.array([r.max_drawdown_pct for r in results])
    streaks = np.array([r.max_losing_streak for r in results])
    wrs = np.array([r.winrate for r in results])
    sharpes = np.array([r.sharpe for r in results])
    ruined = np.array([r.ruined for r in results])

    def _pc(v: float) -> str:
        return _MC_GREEN if v > 0 else (_MC_RED if v < 0 else _MC_WHITE)

    tag = f"  {symbol} {interval}m" if symbol else ""

    log.info("")
    log.info(f"{_MC_CYAN}{'═' * 64}{_MC_RESET}")
    log.info(f"{_MC_BOLD}{_MC_WHITE}  MONTE CARLO SIMULATION{tag}{_MC_RESET}")
    log.info(f"{_MC_CYAN}{'═' * 64}{_MC_RESET}")
    log.info(f"  Source trades:     {_MC_BOLD}{n_source_trades}{_MC_RESET}")
    log.info(f"  Simulations:       {_MC_BOLD}{n:,}{_MC_RESET}")
    log.info(f"  Starting wallet:   {_MC_BOLD}{starting_wallet:.2f} USDT{_MC_RESET}")

    # Wallet
    log.info(f"{_MC_CYAN}{'─' * 64}{_MC_RESET}")
    log.info(f"  {_MC_BOLD}WALLET DISTRIBUTION{_MC_RESET}")
    log.info(f"    Mean:            {_pc(np.mean(wallets) - starting_wallet)}{np.mean(wallets):.2f} USDT{_MC_RESET}")
    log.info(f"    Median:          {_pc(np.median(wallets) - starting_wallet)}{np.median(wallets):.2f} USDT{_MC_RESET}")
    log.info(f"    Std Dev:         {np.std(wallets):.2f} USDT")
    log.info(f"    5th pctl:        {_pc(np.percentile(wallets, 5) - starting_wallet)}{np.percentile(wallets, 5):.2f} USDT{_MC_RESET}  {_MC_DIM}(worst 5%){_MC_RESET}")
    log.info(f"    95th pctl:       {_pc(np.percentile(wallets, 95) - starting_wallet)}{np.percentile(wallets, 95):.2f} USDT{_MC_RESET}  {_MC_DIM}(best 5%){_MC_RESET}")

    # PnL
    log.info(f"{_MC_CYAN}{'─' * 64}{_MC_RESET}")
    log.info(f"  {_MC_BOLD}PnL DISTRIBUTION{_MC_RESET}")
    log.info(f"    Mean:            {_pc(np.mean(pnls))}{np.mean(pnls):+.4f} USDT  ({np.mean(pnl_pcts):+.2f}%){_MC_RESET}")
    log.info(f"    Median:          {_pc(np.median(pnls))}{np.median(pnls):+.4f} USDT  ({np.median(pnl_pcts):+.2f}%){_MC_RESET}")
    log.info(f"    5th pctl:        {_pc(np.percentile(pnls, 5))}{np.percentile(pnls, 5):+.4f} USDT  ({np.percentile(pnl_pcts, 5):+.2f}%){_MC_RESET}")
    log.info(f"    95th pctl:       {_pc(np.percentile(pnls, 95))}{np.percentile(pnls, 95):+.4f} USDT  ({np.percentile(pnl_pcts, 95):+.2f}%){_MC_RESET}")

    # Probabilities
    profit_p = float(np.mean(pnls > 0))
    ruin_p = float(np.mean(ruined))
    log.info(f"{_MC_CYAN}{'─' * 64}{_MC_RESET}")
    log.info(f"  {_MC_BOLD}PROBABILITIES{_MC_RESET}")
    p_col = _MC_GREEN if profit_p >= 0.5 else _MC_RED
    r_col = _MC_RED if ruin_p > 0.05 else _MC_GREEN
    log.info(f"    Profit:          {p_col}{profit_p*100:.1f}%{_MC_RESET}  {_mc_bar(profit_p)}")
    log.info(f"    Ruin:            {r_col}{ruin_p*100:.1f}%{_MC_RESET}  {_mc_bar(ruin_p)}")

    # Drawdown
    log.info(f"{_MC_CYAN}{'─' * 64}{_MC_RESET}")
    log.info(f"  {_MC_BOLD}MAX DRAWDOWN{_MC_RESET}")
    log.info(f"    Mean:            {_MC_RED}{np.mean(dds):.2f}%{_MC_RESET}")
    log.info(f"    95th pctl:       {_MC_RED}{np.percentile(dds, 95):.2f}%{_MC_RESET}  {_MC_DIM}(expect in 1-of-20 runs){_MC_RESET}")
    log.info(f"    Worst:           {_MC_RED}{np.max(dds):.2f}%{_MC_RESET}")

    # Losing streak
    log.info(f"{_MC_CYAN}{'─' * 64}{_MC_RESET}")
    log.info(f"  {_MC_BOLD}LOSING STREAK{_MC_RESET}")
    log.info(f"    Mean:            {np.mean(streaks):.1f} trades")
    log.info(f"    95th pctl:       {np.percentile(streaks, 95):.0f} trades")
    log.info(f"    Worst:           {int(np.max(streaks))} trades")

    # Win rate + Sharpe
    log.info(f"{_MC_CYAN}{'─' * 64}{_MC_RESET}")
    log.info(f"  {_MC_BOLD}WIN RATE{_MC_RESET}")
    log.info(f"    Mean:            {np.mean(wrs):.2f}%")
    log.info(f"    5th-95th:        {np.percentile(wrs, 5):.2f}% — {np.percentile(wrs, 95):.2f}%")

    log.info(f"{_MC_CYAN}{'─' * 64}{_MC_RESET}")
    log.info(f"  {_MC_BOLD}SHARPE RATIO{_MC_RESET}")
    log.info(f"    Mean:            {np.mean(sharpes):.4f}")
    log.info(f"    5th-95th:        {np.percentile(sharpes, 5):.4f} — {np.percentile(sharpes, 95):.4f}")

    # Confidence interval
    w5 = float(np.percentile(wallets, 5))
    w95 = float(np.percentile(wallets, 95))
    p5 = float(np.percentile(pnl_pcts, 5))
    p95 = float(np.percentile(pnl_pcts, 95))
    log.info(f"{_MC_CYAN}{'─' * 64}{_MC_RESET}")
    log.info(f"  {_MC_BOLD}90% CONFIDENCE INTERVAL{_MC_RESET}")
    log.info(f"    Wallet:  {_pc(w5 - starting_wallet)}{w5:.2f}{_MC_RESET}  ←→  {_pc(w95 - starting_wallet)}{w95:.2f} USDT{_MC_RESET}")
    log.info(f"    PnL:     {_pc(p5)}{p5:+.2f}%{_MC_RESET}  ←→  {_pc(p95)}{p95:+.2f}%{_MC_RESET}")
    log.info(f"{_MC_CYAN}{'═' * 64}{_MC_RESET}")
    log.info("")
