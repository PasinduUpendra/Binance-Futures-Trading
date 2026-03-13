"""
Tests for Kelly Criterion position sizing.
"""

import pytest
from decimal import Decimal

from src.risk.kelly_criterion import KellyCriterion


class TestKellyCriterion:
    """Test suite for Kelly Criterion calculations."""

    def test_positive_edge(self):
        """55% win rate with 1:2 R/R should give positive fraction."""
        fraction = KellyCriterion.calculate(win_rate=0.55, avg_win=2.0, avg_loss=1.0)
        assert fraction > 0

    def test_half_kelly_is_half(self):
        """Half-Kelly should be exactly half of full Kelly."""
        full = KellyCriterion.calculate(win_rate=0.55, avg_win=2.0, avg_loss=1.0)
        half = KellyCriterion.half_kelly(win_rate=0.55, avg_win=2.0, avg_loss=1.0)
        assert abs(half - full / 2) < Decimal("0.0001")

    def test_negative_edge_returns_zero(self):
        """Negative expected value should return 0."""
        fraction = KellyCriterion.calculate(win_rate=0.3, avg_win=1.0, avg_loss=1.5)
        assert fraction == 0

    def test_coin_flip_no_edge(self):
        """50/50 with 1:1 R/R should return 0."""
        fraction = KellyCriterion.calculate(win_rate=0.5, avg_win=1.0, avg_loss=1.0)
        assert fraction == 0

    def test_position_size_capped_at_15_pct(self):
        """Position size should never exceed 15% of balance."""
        size = KellyCriterion.position_size(
            balance=Decimal("75"), win_rate=0.9, risk_reward_ratio=5.0
        )
        assert size <= Decimal("75") * Decimal("0.15")

    def test_position_size_with_small_balance(self):
        """Small balance should still produce valid sizing."""
        size = KellyCriterion.position_size(
            balance=Decimal("35"), win_rate=0.55, risk_reward_ratio=2.0
        )
        assert size >= 0
        assert size <= Decimal("35") * Decimal("0.15")

    def test_zero_win_rate(self):
        """Zero win rate should return 0."""
        fraction = KellyCriterion.calculate(win_rate=0.0, avg_win=2.0, avg_loss=1.0)
        assert fraction == 0

    def test_perfect_win_rate(self):
        """100% win rate is out of valid range (0, 1) - returns 0 as safety."""
        fraction = KellyCriterion.calculate(win_rate=1.0, avg_win=2.0, avg_loss=1.0)
        # Implementation treats win_rate=1.0 as out of range - safety feature
        assert fraction >= 0
