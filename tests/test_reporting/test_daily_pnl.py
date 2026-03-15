"""Comprehensive tests for the daily_pnl module.

Covers DailyPnL model, DailyPnLCalculator.calculate_daily_pnl,
and DailyPnLCalculator.get_cumulative_stats.
"""

from decimal import Decimal
from datetime import date

import pytest

from src.reporting.daily_pnl import DailyPnL, DailyPnLCalculator, CumulativeStats


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def calculator() -> DailyPnLCalculator:
    """Default calculator with initial_capital=75."""
    return DailyPnLCalculator(initial_capital=Decimal("75"))


@pytest.fixture
def report_date() -> date:
    """Fixed date for deterministic tests."""
    return date(2026, 3, 14)


# ---------------------------------------------------------------------------
# calculate_daily_pnl tests
# ---------------------------------------------------------------------------


class TestCalculateDailyPnl:
    """Tests for DailyPnLCalculator.calculate_daily_pnl."""

    def test_no_trades_flat_balance(
        self, calculator: DailyPnLCalculator, report_date: date
    ) -> None:
        """No trades with equal start/end balance produces zero P&L."""
        result = calculator.calculate_daily_pnl(
            trades=[],
            start_balance=Decimal("100"),
            end_balance=Decimal("100"),
            report_date=report_date,
        )

        assert result.date == report_date
        assert result.start_balance == Decimal("100")
        assert result.end_balance == Decimal("100")
        assert result.realized_pnl == Decimal("0")
        assert result.unrealized_pnl == Decimal("0")
        assert result.fees == Decimal("0")
        assert result.net_pnl == Decimal("0")
        assert result.pnl_pct == Decimal("0")
        assert result.trades_count == 0
        assert result.wins == 0
        assert result.losses == 0
        assert result.strategies_used == []

    def test_one_winning_trade(
        self, calculator: DailyPnLCalculator, report_date: date
    ) -> None:
        """Single winning trade is counted correctly."""
        trades = [
            {"pnl": Decimal("5"), "fees": Decimal("0.50"), "strategy": "trend_follower"}
        ]
        result = calculator.calculate_daily_pnl(
            trades=trades,
            start_balance=Decimal("100"),
            end_balance=Decimal("104.50"),
            report_date=report_date,
        )

        assert result.realized_pnl == Decimal("5")
        assert result.fees == Decimal("0.50")
        assert result.net_pnl == Decimal("4.50")
        assert result.trades_count == 1
        assert result.wins == 1
        assert result.losses == 0
        assert result.strategies_used == ["trend_follower"]

    def test_one_losing_trade(
        self, calculator: DailyPnLCalculator, report_date: date
    ) -> None:
        """Single losing trade is counted correctly."""
        trades = [
            {"pnl": Decimal("-3"), "fees": Decimal("0.30"), "strategy": "scalper"}
        ]
        result = calculator.calculate_daily_pnl(
            trades=trades,
            start_balance=Decimal("100"),
            end_balance=Decimal("96.70"),
            report_date=report_date,
        )

        assert result.realized_pnl == Decimal("-3")
        assert result.fees == Decimal("0.30")
        assert result.net_pnl == Decimal("-3.30")
        assert result.trades_count == 1
        assert result.wins == 0
        assert result.losses == 1

    def test_mixed_trades(
        self, calculator: DailyPnLCalculator, report_date: date
    ) -> None:
        """Two winning trades and one losing trade are tallied correctly."""
        trades = [
            {"pnl": Decimal("4"), "fees": Decimal("0.20"), "strategy": "trend_follower"},
            {"pnl": Decimal("2"), "fees": Decimal("0.10"), "strategy": "breakout_trader"},
            {"pnl": Decimal("-1"), "fees": Decimal("0.15"), "strategy": "scalper"},
        ]
        # Realized: 4 + 2 + (-1) = 5, Fees: 0.45
        # end_balance constructed so net_pnl = 4.55
        result = calculator.calculate_daily_pnl(
            trades=trades,
            start_balance=Decimal("100"),
            end_balance=Decimal("104.55"),
            report_date=report_date,
        )

        assert result.realized_pnl == Decimal("5")
        assert result.fees == Decimal("0.45")
        assert result.net_pnl == Decimal("4.55")
        assert result.trades_count == 3
        assert result.wins == 2
        assert result.losses == 1

    def test_net_pnl_from_balance_difference(
        self, calculator: DailyPnLCalculator, report_date: date
    ) -> None:
        """net_pnl is always end_balance - start_balance regardless of trade data."""
        trades = [
            {"pnl": Decimal("2"), "fees": Decimal("0.10"), "strategy": "scalper"},
        ]
        result = calculator.calculate_daily_pnl(
            trades=trades,
            start_balance=Decimal("100"),
            end_balance=Decimal("110"),
            report_date=report_date,
        )

        # net_pnl = 110 - 100 = 10, regardless of realized pnl of 2
        assert result.net_pnl == Decimal("10")

    def test_fees_summed_correctly(
        self, calculator: DailyPnLCalculator, report_date: date
    ) -> None:
        """Total fees are the sum of all trade fees."""
        trades = [
            {"pnl": Decimal("1"), "fees": Decimal("0.25"), "strategy": "a"},
            {"pnl": Decimal("1"), "fees": Decimal("0.75"), "strategy": "b"},
            {"pnl": Decimal("1"), "fees": Decimal("1.00"), "strategy": "c"},
        ]
        result = calculator.calculate_daily_pnl(
            trades=trades,
            start_balance=Decimal("100"),
            end_balance=Decimal("101"),
            report_date=report_date,
        )

        assert result.fees == Decimal("2.00")

    def test_is_win_derived_from_pnl(
        self, calculator: DailyPnLCalculator, report_date: date
    ) -> None:
        """When is_win is absent, it is derived: pnl > 0 => win, else loss."""
        trades = [
            {"pnl": Decimal("3"), "fees": Decimal("0"), "strategy": "a"},
            {"pnl": Decimal("-1"), "fees": Decimal("0"), "strategy": "b"},
            {"pnl": Decimal("0"), "fees": Decimal("0"), "strategy": "c"},  # zero => not > 0 => loss
        ]
        result = calculator.calculate_daily_pnl(
            trades=trades,
            start_balance=Decimal("100"),
            end_balance=Decimal("102"),
            report_date=report_date,
        )

        assert result.wins == 1   # only pnl=3 is a win
        assert result.losses == 2  # pnl=-1 and pnl=0 are losses

    def test_explicit_is_win_flag(
        self, calculator: DailyPnLCalculator, report_date: date
    ) -> None:
        """Explicit is_win overrides pnl-based derivation."""
        trades = [
            # pnl is negative but is_win=True (e.g. stop-loss that was "correct")
            {"pnl": Decimal("-0.50"), "fees": Decimal("0"), "strategy": "a", "is_win": True},
            # pnl is positive but is_win=False (e.g. partial fill deemed loss)
            {"pnl": Decimal("1"), "fees": Decimal("0"), "strategy": "b", "is_win": False},
        ]
        result = calculator.calculate_daily_pnl(
            trades=trades,
            start_balance=Decimal("100"),
            end_balance=Decimal("100.50"),
            report_date=report_date,
        )

        assert result.wins == 1   # first trade: is_win=True despite negative pnl
        assert result.losses == 1  # second trade: is_win=False despite positive pnl

    def test_strategies_used_deduplicated_and_sorted(
        self, calculator: DailyPnLCalculator, report_date: date
    ) -> None:
        """strategies_used is a deduplicated, sorted list of strategy names."""
        trades = [
            {"pnl": Decimal("1"), "fees": Decimal("0"), "strategy": "scalper"},
            {"pnl": Decimal("1"), "fees": Decimal("0"), "strategy": "trend_follower"},
            {"pnl": Decimal("1"), "fees": Decimal("0"), "strategy": "scalper"},  # duplicate
            {"pnl": Decimal("1"), "fees": Decimal("0"), "strategy": "breakout_trader"},
        ]
        result = calculator.calculate_daily_pnl(
            trades=trades,
            start_balance=Decimal("100"),
            end_balance=Decimal("104"),
            report_date=report_date,
        )

        assert result.strategies_used == ["breakout_trader", "scalper", "trend_follower"]

    def test_zero_start_balance_no_division_error(
        self, calculator: DailyPnLCalculator, report_date: date
    ) -> None:
        """Zero start_balance does not raise; pnl_pct is 0."""
        result = calculator.calculate_daily_pnl(
            trades=[],
            start_balance=Decimal("0"),
            end_balance=Decimal("10"),
            report_date=report_date,
        )

        assert result.pnl_pct == Decimal("0")
        assert result.net_pnl == Decimal("10")


# ---------------------------------------------------------------------------
# get_cumulative_stats tests
# ---------------------------------------------------------------------------


class TestGetCumulativeStats:
    """Tests for DailyPnLCalculator.get_cumulative_stats."""

    def test_empty_list_returns_defaults(
        self, calculator: DailyPnLCalculator
    ) -> None:
        """An empty list of daily PnLs returns default CumulativeStats."""
        stats = calculator.get_cumulative_stats([])

        assert stats.total_pnl == Decimal("0")
        assert stats.total_pnl_pct == Decimal("0")
        assert stats.max_drawdown == Decimal("0")
        assert stats.sharpe_ratio == 0.0
        assert stats.win_rate == Decimal("0")
        assert stats.total_trades == 0
        assert stats.doubling_progress == Decimal("0")

    def test_single_day(
        self, calculator: DailyPnLCalculator, report_date: date
    ) -> None:
        """Single day stats are computed correctly."""
        daily = DailyPnL(
            date=report_date,
            start_balance=Decimal("75"),
            end_balance=Decimal("76"),
            net_pnl=Decimal("1"),
            trades_count=2,
            wins=1,
            losses=1,
        )
        stats = calculator.get_cumulative_stats([daily])

        assert stats.total_pnl == Decimal("1")
        assert stats.total_trades == 2
        assert stats.total_wins == 1
        assert stats.total_losses == 1
        assert stats.profitable_days == 1
        assert stats.losing_days == 0
        assert stats.best_day == Decimal("1")
        assert stats.worst_day == Decimal("1")
        assert stats.avg_daily_pnl == Decimal("1")
        # Sharpe requires >= 2 data points, so 0
        assert stats.sharpe_ratio == 0.0

    def test_multiple_days_total_pnl(
        self, calculator: DailyPnLCalculator, report_date: date
    ) -> None:
        """total_pnl is the sum of net_pnl across all days."""
        days = [
            DailyPnL(
                date=date(2026, 3, 12),
                start_balance=Decimal("75"),
                end_balance=Decimal("77"),
                net_pnl=Decimal("2"),
                trades_count=1,
                wins=1,
                losses=0,
            ),
            DailyPnL(
                date=date(2026, 3, 13),
                start_balance=Decimal("77"),
                end_balance=Decimal("76"),
                net_pnl=Decimal("-1"),
                trades_count=1,
                wins=0,
                losses=1,
            ),
            DailyPnL(
                date=date(2026, 3, 14),
                start_balance=Decimal("76"),
                end_balance=Decimal("79"),
                net_pnl=Decimal("3"),
                trades_count=2,
                wins=2,
                losses=0,
            ),
        ]
        stats = calculator.get_cumulative_stats(days)

        assert stats.total_pnl == Decimal("4")  # 2 + (-1) + 3
        assert stats.total_trades == 4  # 1 + 1 + 2
        assert stats.total_wins == 3
        assert stats.total_losses == 1
        assert stats.profitable_days == 2
        assert stats.losing_days == 1
        assert stats.best_day == Decimal("3")
        assert stats.worst_day == Decimal("-1")

    def test_max_drawdown(
        self, calculator: DailyPnLCalculator
    ) -> None:
        """Max drawdown tracks peak-to-trough correctly (up then down)."""
        days = [
            DailyPnL(
                date=date(2026, 3, 10),
                start_balance=Decimal("75"),
                end_balance=Decimal("80"),
                net_pnl=Decimal("5"),
                trades_count=1,
                wins=1,
                losses=0,
            ),
            DailyPnL(
                date=date(2026, 3, 11),
                start_balance=Decimal("80"),
                end_balance=Decimal("90"),
                net_pnl=Decimal("10"),
                trades_count=1,
                wins=1,
                losses=0,
            ),
            # Peak is now 90, drop to 72 => drawdown = (90-72)/90 = 20%
            DailyPnL(
                date=date(2026, 3, 12),
                start_balance=Decimal("90"),
                end_balance=Decimal("72"),
                net_pnl=Decimal("-18"),
                trades_count=2,
                wins=0,
                losses=2,
            ),
            # Recovery to 80, drawdown from peak 90 => (90-80)/90 = 11.1%
            DailyPnL(
                date=date(2026, 3, 13),
                start_balance=Decimal("72"),
                end_balance=Decimal("80"),
                net_pnl=Decimal("8"),
                trades_count=1,
                wins=1,
                losses=0,
            ),
        ]
        stats = calculator.get_cumulative_stats(days)

        # Max drawdown = (90 - 72) / 90 = 0.2 => stored as 20%
        assert stats.max_drawdown == Decimal("20")

    def test_sharpe_ratio_nonzero_with_varied_returns(
        self, calculator: DailyPnLCalculator
    ) -> None:
        """Sharpe ratio is non-zero when there are at least 2 days with varied returns."""
        days = [
            DailyPnL(
                date=date(2026, 3, 12),
                start_balance=Decimal("100"),
                end_balance=Decimal("105"),
                net_pnl=Decimal("5"),
                trades_count=1,
                wins=1,
                losses=0,
            ),
            DailyPnL(
                date=date(2026, 3, 13),
                start_balance=Decimal("105"),
                end_balance=Decimal("103"),
                net_pnl=Decimal("-2"),
                trades_count=1,
                wins=0,
                losses=1,
            ),
            DailyPnL(
                date=date(2026, 3, 14),
                start_balance=Decimal("103"),
                end_balance=Decimal("108"),
                net_pnl=Decimal("5"),
                trades_count=1,
                wins=1,
                losses=0,
            ),
        ]
        stats = calculator.get_cumulative_stats(days)

        assert stats.sharpe_ratio != 0.0
        # Positive mean return with some variance => positive Sharpe
        assert stats.sharpe_ratio > 0

    def test_win_rate_across_all_days(
        self, calculator: DailyPnLCalculator
    ) -> None:
        """win_rate is computed from total_wins / total_trades across all days."""
        days = [
            DailyPnL(
                date=date(2026, 3, 12),
                start_balance=Decimal("100"),
                end_balance=Decimal("102"),
                net_pnl=Decimal("2"),
                trades_count=3,
                wins=2,
                losses=1,
            ),
            DailyPnL(
                date=date(2026, 3, 13),
                start_balance=Decimal("102"),
                end_balance=Decimal("101"),
                net_pnl=Decimal("-1"),
                trades_count=2,
                wins=1,
                losses=1,
            ),
        ]
        stats = calculator.get_cumulative_stats(days)

        # total_wins=3, total_trades=5 => 60%
        assert stats.total_wins == 3
        assert stats.total_trades == 5
        assert stats.win_rate == Decimal("60")

    def test_doubling_progress_at_50_percent(self) -> None:
        """Balance at 1.5x initial capital => 50% doubling progress."""
        calc = DailyPnLCalculator(initial_capital=Decimal("100"))
        days = [
            DailyPnL(
                date=date(2026, 3, 14),
                start_balance=Decimal("140"),
                end_balance=Decimal("150"),
                net_pnl=Decimal("10"),
                trades_count=1,
                wins=1,
                losses=0,
            ),
        ]
        stats = calc.get_cumulative_stats(days)

        # progress = ((150 - 100) / (200 - 100)) * 100 = 50%
        assert stats.doubling_progress == Decimal("50")

    def test_doubling_progress_at_100_percent(self) -> None:
        """Balance at 2x initial capital => 100% doubling progress (capped)."""
        calc = DailyPnLCalculator(initial_capital=Decimal("100"))
        days = [
            DailyPnL(
                date=date(2026, 3, 14),
                start_balance=Decimal("195"),
                end_balance=Decimal("200"),
                net_pnl=Decimal("5"),
                trades_count=1,
                wins=1,
                losses=0,
            ),
        ]
        stats = calc.get_cumulative_stats(days)

        # progress = ((200 - 100) / (200 - 100)) * 100 = 100%
        assert stats.doubling_progress == Decimal("100")

    def test_doubling_progress_capped_above_100(self) -> None:
        """Balance above 2x initial is still capped at 100%."""
        calc = DailyPnLCalculator(initial_capital=Decimal("100"))
        days = [
            DailyPnL(
                date=date(2026, 3, 14),
                start_balance=Decimal("240"),
                end_balance=Decimal("250"),
                net_pnl=Decimal("10"),
                trades_count=1,
                wins=1,
                losses=0,
            ),
        ]
        stats = calc.get_cumulative_stats(days)

        # Raw: ((250 - 100) / 100) * 100 = 150%, but capped at 100
        assert stats.doubling_progress == Decimal("100")
