"""
Tests for anti-hallucination sanity checks.
"""

import pytest
from decimal import Decimal

from src.anti_hallucination.sanity_checks import SanityChecker


class TestSanityChecker:
    """Test suite for mathematical sanity checks."""

    def test_valid_position_math(self):
        """Correct position math should pass."""
        valid, details = SanityChecker.check_position_math(
            balance=Decimal("75"), size=Decimal("10"), leverage=5, notional=Decimal("50")
        )
        assert valid is True

    def test_invalid_position_math(self):
        """Incorrect position math should fail."""
        valid, details = SanityChecker.check_position_math(
            balance=Decimal("75"), size=Decimal("10"), leverage=5, notional=Decimal("100")
        )
        assert valid is False

    def test_position_exceeds_balance(self):
        """Position larger than 15% of balance should fail."""
        valid, details = SanityChecker.check_position_math(
            balance=Decimal("75"), size=Decimal("20"), leverage=1, notional=Decimal("20")
        )
        # 20/75 = 26.7% > 15% limit
        assert valid is False

    def test_valid_pnl_math_long(self):
        """Correct long P&L should pass."""
        # PnL = (exit - entry) / entry * notional = (105-100)/100 * 100 = 5
        valid, details = SanityChecker.check_pnl_math(
            entry=Decimal("100"), exit_price=Decimal("105"),
            size=Decimal("100"), direction="long", reported_pnl=Decimal("5")
        )
        assert valid is True

    def test_valid_pnl_math_short(self):
        """Correct short P&L should pass."""
        # PnL = (entry - exit) / entry * notional = (100-95)/100 * 100 = 5
        valid, details = SanityChecker.check_pnl_math(
            entry=Decimal("100"), exit_price=Decimal("95"),
            size=Decimal("100"), direction="short", reported_pnl=Decimal("5")
        )
        assert valid is True

    def test_invalid_pnl_math(self):
        """Incorrect P&L should fail."""
        valid, details = SanityChecker.check_pnl_math(
            entry=Decimal("100"), exit_price=Decimal("105"),
            size=Decimal("100"), direction="long", reported_pnl=Decimal("20")
        )
        assert valid is False

    def test_leverage_within_bounds(self):
        """Leverage within max should pass."""
        valid, details = SanityChecker.check_leverage_bounds(5, 10)
        assert valid is True

    def test_leverage_exceeds_bounds(self):
        """Leverage exceeding max should fail."""
        valid, details = SanityChecker.check_leverage_bounds(15, 10)
        assert valid is False

    def test_zero_leverage(self):
        """Zero leverage should fail."""
        valid, details = SanityChecker.check_leverage_bounds(0, 10)
        assert valid is False

    def test_liquidation_distance_safe(self):
        """Liquidation distance > 5% should pass."""
        valid, details = SanityChecker.check_liquidation_distance(
            entry=Decimal("100"), liquidation=Decimal("90"), leverage=5
        )
        assert valid is True

    def test_liquidation_distance_too_close(self):
        """Liquidation distance < 5% should fail."""
        valid, details = SanityChecker.check_liquidation_distance(
            entry=Decimal("100"), liquidation=Decimal("98"), leverage=5
        )
        assert valid is False
