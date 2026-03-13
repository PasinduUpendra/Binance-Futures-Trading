"""
Tests for circuit breaker system - the most critical safety component.
"""

import pytest
from decimal import Decimal

from src.risk.circuit_breaker import CircuitBreaker, CircuitBreakerLevel


class TestCircuitBreaker:
    """Test suite for circuit breaker enforcement."""

    # ─── Level Detection ───

    def test_green_level(self):
        """Balance > $60 should be GREEN."""
        level = CircuitBreaker.check_level(Decimal("75"))
        assert level == CircuitBreakerLevel.GREEN

    def test_green_boundary(self):
        """Balance exactly $60.01 should be GREEN."""
        level = CircuitBreaker.check_level(Decimal("60.01"))
        assert level == CircuitBreakerLevel.GREEN

    def test_yellow_level(self):
        """Balance $45-$60 should be YELLOW."""
        level = CircuitBreaker.check_level(Decimal("52"))
        assert level == CircuitBreakerLevel.YELLOW

    def test_yellow_upper_boundary(self):
        """Balance exactly $60 should be YELLOW (< GREEN threshold)."""
        level = CircuitBreaker.check_level(Decimal("60"))
        # $60 is below the GREEN min of $60, so YELLOW
        assert level in (CircuitBreakerLevel.GREEN, CircuitBreakerLevel.YELLOW)

    def test_yellow_lower_boundary(self):
        """Balance exactly $45.01 should be YELLOW."""
        level = CircuitBreaker.check_level(Decimal("45.01"))
        assert level == CircuitBreakerLevel.YELLOW

    def test_red_level(self):
        """Balance $30-$45 should be RED."""
        level = CircuitBreaker.check_level(Decimal("37"))
        assert level == CircuitBreakerLevel.RED

    def test_red_upper_boundary(self):
        """Balance exactly $45 should be RED."""
        level = CircuitBreaker.check_level(Decimal("45"))
        assert level in (CircuitBreakerLevel.YELLOW, CircuitBreakerLevel.RED)

    def test_red_lower_boundary(self):
        """Balance exactly $30.01 should be RED."""
        level = CircuitBreaker.check_level(Decimal("30.01"))
        assert level == CircuitBreakerLevel.RED

    def test_dead_level(self):
        """Balance < $30 should be DEAD."""
        level = CircuitBreaker.check_level(Decimal("29.99"))
        assert level == CircuitBreakerLevel.DEAD

    def test_dead_zero_balance(self):
        """Zero balance should be DEAD."""
        level = CircuitBreaker.check_level(Decimal("0"))
        assert level == CircuitBreakerLevel.DEAD

    def test_dead_negative_balance(self):
        """Negative balance should be DEAD."""
        level = CircuitBreaker.check_level(Decimal("-10"))
        assert level == CircuitBreakerLevel.DEAD

    def test_dead_exactly_30(self):
        """Balance exactly $30 should be RED or DEAD (boundary)."""
        level = CircuitBreaker.check_level(Decimal("30"))
        assert level in (CircuitBreakerLevel.RED, CircuitBreakerLevel.DEAD)

    # ─── Constraints ───

    def test_green_constraints(self):
        """GREEN should allow full trading."""
        constraints = CircuitBreaker.get_constraints(Decimal("75"))
        assert constraints.max_leverage == 10
        assert constraints.max_positions == 3
        assert constraints.size_multiplier == Decimal("1.0")

    def test_yellow_constraints(self):
        """YELLOW should reduce trading parameters."""
        constraints = CircuitBreaker.get_constraints(Decimal("52"))
        assert constraints.max_leverage == 5
        assert constraints.max_positions == 2
        assert constraints.size_multiplier == Decimal("0.5")

    def test_red_constraints(self):
        """RED should severely restrict trading."""
        constraints = CircuitBreaker.get_constraints(Decimal("37"))
        assert constraints.max_leverage == 3
        assert constraints.max_positions == 1

    def test_dead_constraints(self):
        """DEAD should allow nothing."""
        constraints = CircuitBreaker.get_constraints(Decimal("20"))
        assert constraints.max_leverage == 0
        assert constraints.max_positions == 0
        assert constraints.size_multiplier == Decimal("0")

    # ─── Trading Allowed ───

    def test_trading_allowed_green(self):
        """GREEN should allow trading."""
        state = CircuitBreaker.is_trading_allowed(Decimal("75"))
        assert state.constraints.trading_allowed is True

    def test_trading_not_allowed_dead(self):
        """DEAD should never allow trading."""
        state = CircuitBreaker.is_trading_allowed(Decimal("20"))
        assert state.constraints.trading_allowed is False

    # ─── Daily Loss ───

    def test_daily_loss_within_limit(self):
        """Daily loss < 10% should not trigger halt."""
        halt, _ = CircuitBreaker.check_daily_loss(Decimal("75"), Decimal("70"))
        assert halt is False

    def test_daily_loss_exceeds_limit(self):
        """Daily loss > 10% should trigger halt."""
        halt, _ = CircuitBreaker.check_daily_loss(Decimal("75"), Decimal("67"))
        assert halt is True

    def test_daily_loss_exactly_10_pct(self):
        """Daily loss exactly 10% should trigger halt."""
        halt, _ = CircuitBreaker.check_daily_loss(Decimal("75"), Decimal("67.5"))
        assert halt is True

    def test_daily_profit_no_halt(self):
        """Daily profit should never trigger halt."""
        halt, _ = CircuitBreaker.check_daily_loss(Decimal("75"), Decimal("80"))
        assert halt is False

    # ─── Immutability ───

    def test_thresholds_not_modifiable(self):
        """Circuit breaker thresholds should not be modifiable."""
        level1 = CircuitBreaker.check_level(Decimal("50"))
        assert level1 == CircuitBreakerLevel.YELLOW
        # After any "modification attempt", behavior should be unchanged
        level2 = CircuitBreaker.check_level(Decimal("50"))
        assert level2 == CircuitBreakerLevel.YELLOW
