"""
Tests for the funding rate filter (Sprint 1.2).
"""

import pytest

from src.risk.funding_rate_filter import FundingRateFilter, FundingRateResult


class TestFundingRateFilter:
    """Unit tests for FundingRateFilter.evaluate()."""

    # ── Extreme positive funding: reject longs ──

    def test_extreme_positive_rejects_long(self):
        """Funding > 0.05% should reject long trades."""
        result = FundingRateFilter.evaluate(funding_rate=0.0006, signal_direction="long")
        assert result.should_trade is False
        assert result.confidence_adjustment == -20.0
        assert "crowded long" in result.reason.lower()

    def test_extreme_positive_allows_short(self):
        """Funding > 0.05% should NOT reject short trades (they earn funding)."""
        result = FundingRateFilter.evaluate(funding_rate=0.0006, signal_direction="short")
        assert result.should_trade is True

    # ── Extreme negative funding: reject shorts ──

    def test_extreme_negative_rejects_short(self):
        """Funding < -0.05% should reject short trades."""
        result = FundingRateFilter.evaluate(funding_rate=-0.0006, signal_direction="short")
        assert result.should_trade is False
        assert result.confidence_adjustment == -20.0
        assert "crowded short" in result.reason.lower()

    def test_extreme_negative_allows_long(self):
        """Funding < -0.05% should NOT reject long trades (they earn funding)."""
        result = FundingRateFilter.evaluate(funding_rate=-0.0006, signal_direction="long")
        assert result.should_trade is True

    # ── Contrarian bonus: elevated negative for longs ──

    def test_elevated_negative_bonus_long(self):
        """Funding < -0.03% should give bonus to longs (shorts paying longs)."""
        result = FundingRateFilter.evaluate(funding_rate=-0.0004, signal_direction="long")
        assert result.should_trade is True
        assert result.confidence_adjustment == 10.0
        assert "bonus" in result.reason.lower()

    # ── Contrarian bonus: elevated positive for shorts ──

    def test_elevated_positive_bonus_short(self):
        """Funding > 0.03% should give bonus to shorts (longs paying shorts)."""
        result = FundingRateFilter.evaluate(funding_rate=0.0004, signal_direction="short")
        assert result.should_trade is True
        assert result.confidence_adjustment == 10.0
        assert "bonus" in result.reason.lower()

    # ── Neutral funding ──

    def test_neutral_no_adjustment(self):
        """Small neutral funding should not affect trades."""
        result = FundingRateFilter.evaluate(funding_rate=0.0001, signal_direction="long")
        assert result.should_trade is True
        assert result.confidence_adjustment == 0.0
        assert "neutral" in result.reason.lower()

    def test_zero_funding_neutral(self):
        """Zero funding rate should be neutral."""
        result = FundingRateFilter.evaluate(funding_rate=0.0, signal_direction="short")
        assert result.should_trade is True
        assert result.confidence_adjustment == 0.0

    def test_slightly_positive_neutral_long(self):
        """Slightly positive funding (< 0.03%) should be neutral for longs."""
        result = FundingRateFilter.evaluate(funding_rate=0.0002, signal_direction="long")
        assert result.should_trade is True
        assert result.confidence_adjustment == 0.0

    def test_slightly_negative_neutral_short(self):
        """Slightly negative funding (> -0.03%) should be neutral for shorts."""
        result = FundingRateFilter.evaluate(funding_rate=-0.0002, signal_direction="short")
        assert result.should_trade is True
        assert result.confidence_adjustment == 0.0

    # ── Edge cases ──

    def test_exact_extreme_threshold_long(self):
        """Funding exactly at 0.05% should NOT reject (> not >=)."""
        result = FundingRateFilter.evaluate(funding_rate=0.0005, signal_direction="long")
        assert result.should_trade is True

    def test_just_above_extreme_threshold_long(self):
        """Funding just above 0.05% should reject."""
        result = FundingRateFilter.evaluate(
            funding_rate=0.00051, signal_direction="long"
        )
        assert result.should_trade is False

    def test_result_is_frozen(self):
        """FundingRateResult should be immutable."""
        result = FundingRateFilter.evaluate(funding_rate=0.0001, signal_direction="long")
        with pytest.raises(Exception):
            result.should_trade = False

    def test_case_insensitive_direction(self):
        """Direction should be case-insensitive."""
        result = FundingRateFilter.evaluate(funding_rate=0.0006, signal_direction="LONG")
        assert result.should_trade is False

    def test_funding_rate_stored_in_result(self):
        """Result should contain the evaluated funding rate."""
        result = FundingRateFilter.evaluate(funding_rate=0.00012, signal_direction="long")
        assert result.funding_rate == pytest.approx(0.00012)
