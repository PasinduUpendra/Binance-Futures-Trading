"""
Performance tracker for Claude Quant trading system.

Computes strategy-level and portfolio-level performance metrics from
the trade journal. All monetary values use Decimal.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from pydantic import BaseModel, Field

from src.memory.trade_journal import TradeEntry, TradeJournal

logger = logging.getLogger("claude_quant.memory.performance_tracker")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class StrategyPerformance(BaseModel):
    """Aggregated performance metrics for a single strategy."""

    strategy: str
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_pnl: Decimal = Decimal("0")
    total_pnl: Decimal = Decimal("0")
    max_win: Decimal = Decimal("0")
    max_loss: Decimal = Decimal("0")
    avg_win: Decimal = Decimal("0")
    avg_loss: Decimal = Decimal("0")
    profit_factor: Decimal = Decimal("0")
    sharpe: float = 0.0
    best_regime: str = ""
    avg_duration: float | None = None


class DailyPnL(BaseModel):
    """PnL summary for a single day."""

    date: date
    pnl: Decimal = Decimal("0")
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0


# ---------------------------------------------------------------------------
# PerformanceTracker
# ---------------------------------------------------------------------------


class PerformanceTracker:
    """Computes performance metrics from the trade journal.

    Parameters
    ----------
    journal : TradeJournal
        The trade journal to read trade data from.
    """

    def __init__(self, journal: TradeJournal) -> None:
        self._journal = journal
        logger.info("PerformanceTracker initialized")

    def get_strategy_performance(
        self,
        strategy: str,
        regime: str | None = None,
    ) -> StrategyPerformance:
        """Compute performance metrics for a given strategy.

        Parameters
        ----------
        strategy : str
            Strategy name.
        regime : str | None
            If provided, filter trades to only those in this regime.

        Returns
        -------
        StrategyPerformance
            Aggregated performance metrics.
        """
        trades = self._journal.get_trades_by_strategy(strategy)

        if regime:
            trades = [t for t in trades if t.regime == regime]

        return self._compute_strategy_metrics(strategy, trades)

    def get_daily_pnl(self, target_date: date) -> DailyPnL:
        """Get PnL summary for a specific date.

        Parameters
        ----------
        target_date : date
            The date to get PnL for.

        Returns
        -------
        DailyPnL
            PnL summary for the date.
        """
        all_trades = self._journal.get_all_trades()

        day_trades = [
            t for t in all_trades
            if t.timestamp.date() == target_date and t.pnl is not None
        ]

        pnl = sum((t.pnl for t in day_trades if t.pnl is not None), Decimal("0"))
        wins = sum(1 for t in day_trades if t.pnl is not None and t.pnl > 0)
        losses = sum(1 for t in day_trades if t.pnl is not None and t.pnl < 0)

        result = DailyPnL(
            date=target_date,
            pnl=pnl,
            trade_count=len(day_trades),
            win_count=wins,
            loss_count=losses,
        )

        logger.debug(
            "GET_DAILY_PNL date=%s pnl=%s trades=%d wins=%d losses=%d",
            target_date,
            pnl,
            len(day_trades),
            wins,
            losses,
        )
        return result

    def get_cumulative_pnl(self) -> Decimal:
        """Get total cumulative PnL across all recorded trades.

        Returns
        -------
        Decimal
            Total PnL.
        """
        all_trades = self._journal.get_all_trades()
        total = sum(
            (t.pnl for t in all_trades if t.pnl is not None), Decimal("0")
        )
        logger.debug("GET_CUMULATIVE_PNL total=%s", total)
        return total

    def get_sharpe_ratio(self, period_days: int = 30) -> float:
        """Calculate the Sharpe ratio over a specified period.

        Uses daily PnL values annualized with a 365-day year.
        Risk-free rate is assumed to be 0 (appropriate for crypto).

        Parameters
        ----------
        period_days : int
            Number of days to look back (default 30).

        Returns
        -------
        float
            Annualized Sharpe ratio. Returns 0.0 if insufficient data.
        """
        all_trades = self._journal.get_all_trades()
        if not all_trades:
            return 0.0

        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=period_days)
        recent_trades = [
            t for t in all_trades
            if t.timestamp >= cutoff and t.pnl is not None
        ]

        if len(recent_trades) < 2:
            return 0.0

        # Group by date for daily returns
        daily_pnls: dict[date, Decimal] = {}
        for t in recent_trades:
            d = t.timestamp.date()
            if d not in daily_pnls:
                daily_pnls[d] = Decimal("0")
            if t.pnl is not None:
                daily_pnls[d] += t.pnl

        if len(daily_pnls) < 2:
            return 0.0

        pnl_values = [float(v) for v in daily_pnls.values()]
        mean_daily = sum(pnl_values) / len(pnl_values)
        variance = sum((x - mean_daily) ** 2 for x in pnl_values) / (
            len(pnl_values) - 1
        )
        std_daily = math.sqrt(variance) if variance > 0 else 0.0

        if std_daily == 0:
            return 0.0

        # Annualize: Sharpe = (mean_daily / std_daily) * sqrt(365)
        sharpe = (mean_daily / std_daily) * math.sqrt(365)

        logger.debug(
            "GET_SHARPE_RATIO period=%dd days_with_data=%d "
            "mean_daily=%.6f std_daily=%.6f sharpe=%.4f",
            period_days,
            len(daily_pnls),
            mean_daily,
            std_daily,
            sharpe,
        )
        return round(sharpe, 4)

    def get_win_rate_by_regime(self) -> dict[str, float]:
        """Calculate win rate broken down by market regime.

        Returns
        -------
        dict[str, float]
            Mapping of regime name to win rate (0.0 to 1.0).
        """
        all_trades = self._journal.get_all_trades()
        regime_trades: dict[str, list[TradeEntry]] = {}

        for t in all_trades:
            if t.pnl is None:
                continue
            regime = t.regime or "unknown"
            if regime not in regime_trades:
                regime_trades[regime] = []
            regime_trades[regime].append(t)

        result: dict[str, float] = {}
        for regime, trades in regime_trades.items():
            total = len(trades)
            wins = sum(1 for t in trades if t.pnl is not None and t.pnl > 0)
            result[regime] = round(wins / total, 4) if total > 0 else 0.0

        logger.debug("GET_WIN_RATE_BY_REGIME regimes=%s", result)
        return result

    def get_all_strategy_performances(self) -> list[StrategyPerformance]:
        """Compute performance for all strategies that have trades.

        Returns
        -------
        list[StrategyPerformance]
            Performance metrics per strategy.
        """
        all_trades = self._journal.get_all_trades()
        strategies: dict[str, list[TradeEntry]] = {}

        for t in all_trades:
            strat = t.strategy or "unknown"
            if strat not in strategies:
                strategies[strat] = []
            strategies[strat].append(t)

        return [
            self._compute_strategy_metrics(name, trades)
            for name, trades in strategies.items()
        ]

    # -- internal helpers ----------------------------------------------------

    def _compute_strategy_metrics(
        self, strategy: str, trades: list[TradeEntry]
    ) -> StrategyPerformance:
        """Compute aggregated metrics from a list of trades."""
        if not trades:
            return StrategyPerformance(strategy=strategy)

        trades_with_pnl = [t for t in trades if t.pnl is not None]
        if not trades_with_pnl:
            return StrategyPerformance(
                strategy=strategy, total_trades=len(trades)
            )

        wins = [t for t in trades_with_pnl if t.pnl is not None and t.pnl > 0]
        losses = [t for t in trades_with_pnl if t.pnl is not None and t.pnl <= 0]

        total_pnl = sum(
            (t.pnl for t in trades_with_pnl if t.pnl is not None),
            Decimal("0"),
        )
        avg_pnl = total_pnl / len(trades_with_pnl)

        max_win = max(
            (t.pnl for t in wins if t.pnl is not None), default=Decimal("0")
        )
        max_loss = min(
            (t.pnl for t in losses if t.pnl is not None), default=Decimal("0")
        )

        avg_win = (
            sum((t.pnl for t in wins if t.pnl is not None), Decimal("0"))
            / len(wins)
            if wins
            else Decimal("0")
        )
        avg_loss = (
            sum((t.pnl for t in losses if t.pnl is not None), Decimal("0"))
            / len(losses)
            if losses
            else Decimal("0")
        )

        # Profit factor = gross wins / |gross losses|
        gross_wins = sum(
            (t.pnl for t in wins if t.pnl is not None), Decimal("0")
        )
        gross_losses = abs(
            sum((t.pnl for t in losses if t.pnl is not None), Decimal("0"))
        )
        profit_factor = (
            (gross_wins / gross_losses).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
            if gross_losses > 0
            else Decimal("999.9999")
        )

        # Win rate
        win_rate = len(wins) / len(trades_with_pnl) if trades_with_pnl else 0.0

        # Sharpe (using individual trade PnLs)
        pnl_values = [float(t.pnl) for t in trades_with_pnl if t.pnl is not None]
        sharpe = self._calculate_sharpe_from_pnls(pnl_values)

        # Best regime
        best_regime = self._find_best_regime(trades_with_pnl)

        # Average duration
        durations = [t.duration for t in trades if t.duration is not None]
        avg_duration = sum(durations) / len(durations) if durations else None

        perf = StrategyPerformance(
            strategy=strategy,
            total_trades=len(trades),
            wins=len(wins),
            losses=len(losses),
            win_rate=round(win_rate, 4),
            avg_pnl=avg_pnl.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP),
            total_pnl=total_pnl,
            max_win=max_win,
            max_loss=max_loss,
            avg_win=avg_win.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP),
            avg_loss=avg_loss.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP),
            profit_factor=profit_factor,
            sharpe=sharpe,
            best_regime=best_regime,
            avg_duration=avg_duration,
        )

        logger.info(
            "STRATEGY_PERF strategy=%s trades=%d win_rate=%.2f%% "
            "total_pnl=%s sharpe=%.4f best_regime=%s",
            strategy,
            perf.total_trades,
            perf.win_rate * 100,
            perf.total_pnl,
            perf.sharpe,
            perf.best_regime,
        )
        return perf

    @staticmethod
    def _calculate_sharpe_from_pnls(pnl_values: list[float]) -> float:
        """Calculate a simple Sharpe-like ratio from a list of PnL values."""
        if len(pnl_values) < 2:
            return 0.0

        mean = sum(pnl_values) / len(pnl_values)
        variance = sum((x - mean) ** 2 for x in pnl_values) / (
            len(pnl_values) - 1
        )
        std = math.sqrt(variance) if variance > 0 else 0.0

        if std == 0:
            return 0.0

        return round(mean / std, 4)

    @staticmethod
    def _find_best_regime(trades: list[TradeEntry]) -> str:
        """Find the regime with the highest win rate."""
        regime_stats: dict[str, dict[str, int]] = {}

        for t in trades:
            if t.pnl is None:
                continue
            regime = t.regime or "unknown"
            if regime not in regime_stats:
                regime_stats[regime] = {"wins": 0, "total": 0}
            regime_stats[regime]["total"] += 1
            if t.pnl > 0:
                regime_stats[regime]["wins"] += 1

        if not regime_stats:
            return ""

        best_regime = ""
        best_rate = -1.0
        for regime, stats in regime_stats.items():
            rate = stats["wins"] / stats["total"] if stats["total"] > 0 else 0.0
            if rate > best_rate or (
                rate == best_rate
                and stats["total"] > regime_stats.get(best_regime, {}).get("total", 0)
            ):
                best_rate = rate
                best_regime = regime

        return best_regime
