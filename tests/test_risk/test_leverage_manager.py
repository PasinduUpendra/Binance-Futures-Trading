"""
Tests for src.risk.leverage_manager — LeverageManager class.

Covers:
- Leverage lookup across all confidence/regime tiers
- Circuit-breaker capping behaviour
- Edge cases (confidence boundaries, QUIET regime, DEAD level)
- Liquidation buffer calculations (long, short, zero leverage)
"""

from decimal import Decimal

import pytest

from src.risk.leverage_manager import (
    LeverageManager,
    LeverageResult,
    LiquidationBuffer,
    MarketRegime,
)
from src.risk.circuit_breaker import CircuitBreakerLevel


# -------------------------------------------------------------------------
# Leverage table tier tests
# -------------------------------------------------------------------------


class TestDetermineLeverageTiers:
    """Verify that each confidence/regime band returns the expected midpoint."""

    def test_high_confidence_trending(self) -> None:
        """85% confidence + TRENDING + GREEN -> 8x (midpoint of 7-10)."""
        result = LeverageManager.determine_leverage(
            confidence=85,
            regime=MarketRegime.TRENDING,
            circuit_breaker_level=CircuitBreakerLevel.GREEN,
        )
        assert result.leverage == 8
        assert result.raw_leverage == 8
        assert result.capped_by_cb is False

    def test_good_confidence_trending(self) -> None:
        """70% confidence + TRENDING + GREEN -> 6x (midpoint of 5-7)."""
        result = LeverageManager.determine_leverage(
            confidence=70,
            regime=MarketRegime.TRENDING,
            circuit_breaker_level=CircuitBreakerLevel.GREEN,
        )
        assert result.leverage == 6
        assert result.raw_leverage == 6

    def test_good_confidence_volatile(self) -> None:
        """70% confidence + VOLATILE + GREEN -> 4x (midpoint of 3-5)."""
        result = LeverageManager.determine_leverage(
            confidence=70,
            regime=MarketRegime.VOLATILE,
            circuit_breaker_level=CircuitBreakerLevel.GREEN,
        )
        assert result.leverage == 4
        assert result.raw_leverage == 4

    def test_moderate_confidence_trending(self) -> None:
        """50% confidence + TRENDING + GREEN -> 4x (midpoint of 3-5)."""
        result = LeverageManager.determine_leverage(
            confidence=50,
            regime=MarketRegime.TRENDING,
            circuit_breaker_level=CircuitBreakerLevel.GREEN,
        )
        assert result.leverage == 4
        assert result.raw_leverage == 4

    def test_moderate_confidence_volatile(self) -> None:
        """50% confidence + VOLATILE + GREEN -> 2x (midpoint of 2-3)."""
        result = LeverageManager.determine_leverage(
            confidence=50,
            regime=MarketRegime.VOLATILE,
            circuit_breaker_level=CircuitBreakerLevel.GREEN,
        )
        assert result.leverage == 2
        assert result.raw_leverage == 2

    def test_low_confidence_trending(self) -> None:
        """30% confidence + TRENDING + GREEN -> 2x (midpoint of 2-3)."""
        result = LeverageManager.determine_leverage(
            confidence=30,
            regime=MarketRegime.TRENDING,
            circuit_breaker_level=CircuitBreakerLevel.GREEN,
        )
        assert result.leverage == 2
        assert result.raw_leverage == 2

    def test_low_confidence_volatile(self) -> None:
        """30% confidence + VOLATILE + GREEN -> 1x (midpoint of 1-2)."""
        result = LeverageManager.determine_leverage(
            confidence=30,
            regime=MarketRegime.VOLATILE,
            circuit_breaker_level=CircuitBreakerLevel.GREEN,
        )
        assert result.leverage == 1
        assert result.raw_leverage == 1


# -------------------------------------------------------------------------
# Special / rejection cases
# -------------------------------------------------------------------------


class TestDetermineLeverageRejections:
    """Cases that must result in leverage=0 (no trade)."""

    def test_confidence_below_25_no_trade(self) -> None:
        """Confidence 20 (< 25) must be rejected regardless of regime."""
        result = LeverageManager.determine_leverage(
            confidence=20,
            regime=MarketRegime.TRENDING,
            circuit_breaker_level=CircuitBreakerLevel.GREEN,
        )
        assert result.leverage == 0
        assert result.raw_leverage == 0
        assert "too weak" in result.reason.lower() or "< 25" in result.reason

    def test_quiet_regime_no_trade(self) -> None:
        """QUIET regime must always return leverage=0."""
        result = LeverageManager.determine_leverage(
            confidence=90,
            regime=MarketRegime.QUIET,
            circuit_breaker_level=CircuitBreakerLevel.GREEN,
        )
        assert result.leverage == 0
        assert result.raw_leverage == 0
        assert "quiet" in result.reason.lower()

    def test_dead_cb_no_trade(self) -> None:
        """Circuit breaker DEAD must always return leverage=0."""
        result = LeverageManager.determine_leverage(
            confidence=90,
            regime=MarketRegime.TRENDING,
            circuit_breaker_level=CircuitBreakerLevel.DEAD,
        )
        assert result.leverage == 0
        assert result.raw_leverage == 0
        assert result.capped_by_cb is True


# -------------------------------------------------------------------------
# Circuit-breaker capping
# -------------------------------------------------------------------------


class TestCircuitBreakerCapping:
    """Verify that CB levels correctly cap leverage."""

    def test_yellow_cb_caps_at_5(self) -> None:
        """85% TRENDING would be 8x, but YELLOW caps at 5x."""
        result = LeverageManager.determine_leverage(
            confidence=85,
            regime=MarketRegime.TRENDING,
            circuit_breaker_level=CircuitBreakerLevel.YELLOW,
        )
        assert result.leverage == 5
        assert result.raw_leverage == 8
        assert result.capped_by_cb is True

    def test_red_cb_caps_at_3(self) -> None:
        """85% TRENDING would be 8x, but RED caps at 3x."""
        result = LeverageManager.determine_leverage(
            confidence=85,
            regime=MarketRegime.TRENDING,
            circuit_breaker_level=CircuitBreakerLevel.RED,
        )
        assert result.leverage == 3
        assert result.raw_leverage == 8
        assert result.capped_by_cb is True

    def test_green_cb_no_cap(self) -> None:
        """85% TRENDING at GREEN should not be capped (8 <= 10)."""
        result = LeverageManager.determine_leverage(
            confidence=85,
            regime=MarketRegime.TRENDING,
            circuit_breaker_level=CircuitBreakerLevel.GREEN,
        )
        assert result.leverage == 8
        assert result.raw_leverage == 8
        assert result.capped_by_cb is False

    def test_capped_by_cb_flag(self) -> None:
        """Explicitly verify capped_by_cb is True only when CB reduces leverage."""
        # Not capped: 50% VOLATILE GREEN -> 2x, GREEN cap 10 -> no cap
        uncapped = LeverageManager.determine_leverage(
            confidence=50,
            regime=MarketRegime.VOLATILE,
            circuit_breaker_level=CircuitBreakerLevel.GREEN,
        )
        assert uncapped.capped_by_cb is False

        # Capped: 70% TRENDING YELLOW -> raw 6 capped to 5
        capped = LeverageManager.determine_leverage(
            confidence=70,
            regime=MarketRegime.TRENDING,
            circuit_breaker_level=CircuitBreakerLevel.YELLOW,
        )
        assert capped.capped_by_cb is True
        assert capped.raw_leverage == 6
        assert capped.leverage == 5


# -------------------------------------------------------------------------
# String regime conversion
# -------------------------------------------------------------------------


class TestRegimeStringConversion:
    """Passing a lowercase string should work identically to the enum."""

    def test_regime_string_conversion(self) -> None:
        enum_result = LeverageManager.determine_leverage(
            confidence=70,
            regime=MarketRegime.TRENDING,
            circuit_breaker_level=CircuitBreakerLevel.GREEN,
        )
        string_result = LeverageManager.determine_leverage(
            confidence=70,
            regime="trending",
            circuit_breaker_level=CircuitBreakerLevel.GREEN,
        )
        assert enum_result.leverage == string_result.leverage
        assert enum_result.raw_leverage == string_result.raw_leverage
        assert enum_result.capped_by_cb == string_result.capped_by_cb


# -------------------------------------------------------------------------
# Confidence boundary tests
# -------------------------------------------------------------------------


class TestConfidenceBoundaries:
    """Exact boundary values for the 25-threshold."""

    def test_confidence_boundary_25(self) -> None:
        """Exactly 25 is inside the lowest tier — should get leverage > 0."""
        result = LeverageManager.determine_leverage(
            confidence=25,
            regime=MarketRegime.TRENDING,
            circuit_breaker_level=CircuitBreakerLevel.GREEN,
        )
        assert result.leverage > 0

    def test_confidence_boundary_24(self) -> None:
        """Exactly 24 is below threshold — should be rejected."""
        result = LeverageManager.determine_leverage(
            confidence=24,
            regime=MarketRegime.TRENDING,
            circuit_breaker_level=CircuitBreakerLevel.GREEN,
        )
        assert result.leverage == 0
        assert result.raw_leverage == 0


# -------------------------------------------------------------------------
# Liquidation buffer tests
# -------------------------------------------------------------------------


class TestLiquidationBuffer:
    """Verify liquidation price estimation and safety classification."""

    def test_liquidation_buffer_long_safe(self) -> None:
        """5x long from 3000 -> buffer = 20%, should be safe."""
        buf = LeverageManager.calculate_liquidation_buffer(
            entry_price=Decimal("3000"),
            leverage=5,
            direction="long",
        )
        assert buf.entry_price == Decimal("3000")
        # liq = 3000 * (1 - 1/5) = 3000 * 0.8 = 2400
        assert buf.estimated_liquidation_price == Decimal("2400.00000000")
        # buffer = (3000 - 2400) / 3000 = 0.2
        assert buf.buffer_pct == Decimal("0.2000")
        assert buf.is_safe is True
        assert buf.leverage == 5

    def test_liquidation_buffer_long_unsafe(self) -> None:
        """25x long from 3000 -> buffer = 4%, should be unsafe."""
        buf = LeverageManager.calculate_liquidation_buffer(
            entry_price=Decimal("3000"),
            leverage=25,
            direction="long",
        )
        # liq = 3000 * (1 - 1/25) = 3000 * 0.96 = 2880
        assert buf.estimated_liquidation_price == Decimal("2880.00000000")
        # buffer = 120 / 3000 = 0.04
        assert buf.buffer_pct == Decimal("0.0400")
        assert buf.is_safe is False

    def test_liquidation_buffer_short(self) -> None:
        """5x short from 3000 -> liq = 3600, buffer 20%, safe."""
        buf = LeverageManager.calculate_liquidation_buffer(
            entry_price=Decimal("3000"),
            leverage=5,
            direction="short",
        )
        # liq = 3000 * (1 + 1/5) = 3000 * 1.2 = 3600
        assert buf.estimated_liquidation_price == Decimal("3600.00000000")
        assert buf.buffer_pct == Decimal("0.2000")
        assert buf.is_safe is True

    def test_liquidation_buffer_zero_leverage(self) -> None:
        """Leverage=0 means no liquidation risk — always safe."""
        buf = LeverageManager.calculate_liquidation_buffer(
            entry_price=Decimal("3000"),
            leverage=0,
            direction="long",
        )
        assert buf.estimated_liquidation_price == Decimal("0")
        assert buf.buffer_pct == Decimal("1")
        assert buf.is_safe is True
        assert buf.leverage == 0


# -------------------------------------------------------------------------
# Return-type checks
# -------------------------------------------------------------------------


class TestReturnTypes:
    """Ensure the returned objects are the correct Pydantic models."""

    def test_determine_leverage_returns_leverage_result(self) -> None:
        result = LeverageManager.determine_leverage(
            confidence=70,
            regime=MarketRegime.TRENDING,
            circuit_breaker_level=CircuitBreakerLevel.GREEN,
        )
        assert isinstance(result, LeverageResult)

    def test_calculate_liquidation_buffer_returns_liquidation_buffer(self) -> None:
        buf = LeverageManager.calculate_liquidation_buffer(
            entry_price=Decimal("3000"),
            leverage=5,
            direction="long",
        )
        assert isinstance(buf, LiquidationBuffer)
