"""
Daily P&L calculation and cumulative statistics.

All monetary values use ``Decimal`` for precision.  Dates and timestamps
are UTC.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("claude_quant.reporting.daily_pnl")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class DailyPnL(BaseModel):
    """Single day's profit & loss summary."""

    date: date
    start_balance: Decimal
    end_balance: Decimal
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    net_pnl: Decimal = Decimal("0")
    pnl_pct: Decimal = Decimal("0")
    trades_count: int = 0
    wins: int = 0
    losses: int = 0
    strategies_used: list[str] = Field(default_factory=list)


class CumulativeStats(BaseModel):
    """Cumulative performance statistics across multiple days."""

    total_pnl: Decimal = Decimal("0")
    total_pnl_pct: Decimal = Decimal("0")
    max_drawdown: Decimal = Decimal("0")
    sharpe_ratio: float = 0.0
    win_rate: Decimal = Decimal("0")
    avg_daily_pnl: Decimal = Decimal("0")
    best_day: Decimal = Decimal("0")
    worst_day: Decimal = Decimal("0")
    total_trades: int = 0
    total_wins: int = 0
    total_losses: int = 0
    profitable_days: int = 0
    losing_days: int = 0
    doubling_progress: Decimal = Decimal("0")


# ---------------------------------------------------------------------------
# DailyPnLCalculator
# ---------------------------------------------------------------------------


class DailyPnLCalculator:
    """Computes daily P&L from a list of trades and balance snapshots.

    Parameters
    ----------
    initial_capital : Decimal
        The starting capital of the account (used for doubling progress).
        Defaults to $75 per CLAUDE.md.
    """

    def __init__(self, initial_capital: Decimal = Decimal("75")) -> None:
        self._initial_capital = initial_capital

    def calculate_daily_pnl(
        self,
        trades: list[dict[str, Any]],
        start_balance: Decimal,
        end_balance: Decimal,
        report_date: date | None = None,
    ) -> DailyPnL:
        """Calculate a single day's P&L.

        Parameters
        ----------
        trades : list[dict]
            Each trade dict must have at minimum:
              - ``"pnl"`` : Decimal — realised P&L
              - ``"fees"`` : Decimal — trading fees
              - ``"strategy"`` : str — strategy name
              - ``"is_win"`` : bool (optional, derived from pnl if absent)
        start_balance : Decimal
            Balance at the start of the UTC day.
        end_balance : Decimal
            Balance at the end of the UTC day.
        report_date : date | None
            The date for this report.  Defaults to today (UTC).

        Returns
        -------
        DailyPnL
        """
        if report_date is None:
            report_date = datetime.now(tz=timezone.utc).date()

        realized = Decimal("0")
        total_fees = Decimal("0")
        wins = 0
        losses = 0
        strategies: set[str] = set()

        for trade in trades:
            pnl = Decimal(str(trade.get("pnl", 0)))
            fee = Decimal(str(trade.get("fees", 0)))
            strategy = trade.get("strategy", "unknown")

            realized += pnl
            total_fees += fee
            strategies.add(strategy)

            is_win = trade.get("is_win")
            if is_win is None:
                is_win = pnl > Decimal("0")

            if is_win:
                wins += 1
            else:
                losses += 1

        # Net P&L: end_balance - start_balance accounts for unrealised moves
        net_pnl = end_balance - start_balance
        unrealized = net_pnl - realized + total_fees  # residual is unrealised

        # P&L percentage
        pnl_pct = Decimal("0")
        if start_balance > Decimal("0"):
            pnl_pct = (net_pnl / start_balance) * Decimal("100")

        daily = DailyPnL(
            date=report_date,
            start_balance=start_balance,
            end_balance=end_balance,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            fees=total_fees,
            net_pnl=net_pnl,
            pnl_pct=pnl_pct,
            trades_count=len(trades),
            wins=wins,
            losses=losses,
            strategies_used=sorted(strategies),
        )

        logger.info(
            "Daily P&L [%s]: net=%s (%s%%), trades=%d (W:%d/L:%d)",
            report_date,
            net_pnl,
            pnl_pct.quantize(Decimal("0.01")),
            len(trades),
            wins,
            losses,
        )

        return daily

    def get_cumulative_stats(
        self,
        all_daily_pnls: list[DailyPnL],
    ) -> CumulativeStats:
        """Compute cumulative statistics over all trading days.

        Parameters
        ----------
        all_daily_pnls : list[DailyPnL]
            Chronologically ordered list of daily P&L records.

        Returns
        -------
        CumulativeStats
        """
        if not all_daily_pnls:
            return CumulativeStats()

        total_pnl = Decimal("0")
        total_trades = 0
        total_wins = 0
        total_losses = 0
        profitable_days = 0
        losing_days = 0
        daily_returns: list[float] = []

        # For drawdown calculation
        peak_balance = all_daily_pnls[0].start_balance
        max_drawdown = Decimal("0")

        best_day = Decimal("-999999")
        worst_day = Decimal("999999")

        for daily in all_daily_pnls:
            total_pnl += daily.net_pnl
            total_trades += daily.trades_count
            total_wins += daily.wins
            total_losses += daily.losses

            if daily.net_pnl > Decimal("0"):
                profitable_days += 1
            elif daily.net_pnl < Decimal("0"):
                losing_days += 1

            if daily.net_pnl > best_day:
                best_day = daily.net_pnl
            if daily.net_pnl < worst_day:
                worst_day = daily.net_pnl

            # Daily return for Sharpe calculation
            if daily.start_balance > Decimal("0"):
                ret = float(daily.net_pnl / daily.start_balance)
                daily_returns.append(ret)

            # Max drawdown
            if daily.end_balance > peak_balance:
                peak_balance = daily.end_balance
            if peak_balance > Decimal("0"):
                drawdown = (peak_balance - daily.end_balance) / peak_balance
                if drawdown > max_drawdown:
                    max_drawdown = drawdown

        # Total P&L %
        first_balance = all_daily_pnls[0].start_balance
        total_pnl_pct = Decimal("0")
        if first_balance > Decimal("0"):
            total_pnl_pct = (total_pnl / first_balance) * Decimal("100")

        # Win rate
        win_rate = Decimal("0")
        if total_trades > 0:
            win_rate = (Decimal(str(total_wins)) / Decimal(str(total_trades))) * Decimal("100")

        # Average daily P&L
        n_days = len(all_daily_pnls)
        avg_daily_pnl = total_pnl / Decimal(str(n_days))

        # Sharpe ratio (annualised, assuming 365 trading days for crypto)
        sharpe = 0.0
        if len(daily_returns) >= 2:
            mean_ret = sum(daily_returns) / len(daily_returns)
            std_ret = _std(daily_returns)
            if std_ret > 0:
                sharpe = (mean_ret / std_ret) * math.sqrt(365)

        # Doubling progress: how far from initial capital to 2x initial
        latest_balance = all_daily_pnls[-1].end_balance
        target = self._initial_capital * Decimal("2")
        if target > self._initial_capital:
            progress = (
                (latest_balance - self._initial_capital)
                / (target - self._initial_capital)
            ) * Decimal("100")
            progress = max(Decimal("0"), min(progress, Decimal("100")))
        else:
            progress = Decimal("0")

        stats = CumulativeStats(
            total_pnl=total_pnl,
            total_pnl_pct=total_pnl_pct,
            max_drawdown=max_drawdown * Decimal("100"),  # as percentage
            sharpe_ratio=round(sharpe, 2),
            win_rate=win_rate,
            avg_daily_pnl=avg_daily_pnl,
            best_day=best_day if best_day != Decimal("-999999") else Decimal("0"),
            worst_day=worst_day if worst_day != Decimal("999999") else Decimal("0"),
            total_trades=total_trades,
            total_wins=total_wins,
            total_losses=total_losses,
            profitable_days=profitable_days,
            losing_days=losing_days,
            doubling_progress=progress,
        )

        logger.info(
            "Cumulative stats: PnL=%s (%s%%), DD=%s%%, Sharpe=%s, WR=%s%%, "
            "doubling=%s%%",
            stats.total_pnl,
            stats.total_pnl_pct.quantize(Decimal("0.01")),
            stats.max_drawdown.quantize(Decimal("0.01")),
            stats.sharpe_ratio,
            stats.win_rate.quantize(Decimal("0.01")),
            stats.doubling_progress.quantize(Decimal("0.01")),
        )

        return stats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _std(values: list[float]) -> float:
    """Population standard deviation (avoid importing numpy just for this)."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / (n - 1)  # sample std
    return math.sqrt(variance)
