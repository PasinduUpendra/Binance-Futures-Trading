"""
Tests for fee calculation.
"""

import pytest
from decimal import Decimal

from src.execution.fee_calculator import FeeCalculator


class TestFeeCalculator:
    """Test suite for fee calculations."""

    def setup_method(self):
        self.calc = FeeCalculator()

    def test_taker_fee(self):
        """Taker fee should be 0.05% of notional (VIP 0, verified via API)."""
        fee = self.calc.calculate_fees(Decimal("1000.0"), is_maker=False)
        expected = Decimal("1000.0") * Decimal("0.0005")
        assert fee == expected

    def test_maker_fee(self):
        """Maker fee should be 0.02% of notional."""
        fee = self.calc.calculate_fees(Decimal("1000.0"), is_maker=True)
        expected = Decimal("1000.0") * Decimal("0.0002")
        assert fee == expected

    def test_total_cost_includes_both_fees(self):
        """Total cost should include entry + exit fees."""
        result = self.calc.calculate_total_cost(
            entry_price=Decimal("100"),
            exit_price=Decimal("105"),
            size=Decimal("1"),
            leverage=5,
        )
        # Result is a dict with fee breakdown
        assert isinstance(result, dict)
        # Should have some fee components
        total_fees = sum(v for k, v in result.items() if "fee" in k.lower())
        assert total_fees > 0

    def test_zero_notional(self):
        """Zero notional should have zero fees."""
        fee = self.calc.calculate_fees(Decimal("0"), is_maker=False)
        assert fee == Decimal("0")
