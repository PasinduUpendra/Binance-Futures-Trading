"""
Comprehensive tests for src.risk.position_sizer — PositionSizer.calculate_size.

Covers all circuit-breaker levels, hard caps, minimums, leverage clipping,
and computed fields (notional_value, pct_of_balance, capped, reason).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.risk.circuit_breaker import (
    CircuitBreakerConstraints,
    CircuitBreakerLevel,
    CircuitBreakerState,
)
from src.risk.position_sizer import PositionSize, PositionSizer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_DEFAULT_BALANCE = Decimal("68.33")


def _make_cb_state(
    level: str = "GREEN",
    trading_allowed: bool = True,
    balance: Decimal = _DEFAULT_BALANCE,
) -> CircuitBreakerState:
    """Build a minimal CircuitBreakerState for testing."""
    levels = {
        "GREEN": (10, 3, Decimal("1.0")),
        "YELLOW": (5, 2, Decimal("0.5")),
        "RED": (3, 1, Decimal("0.25")),
        "DEAD": (0, 0, Decimal("0")),
    }
    max_lev, max_pos, size_mult = levels[level]
    # For DEAD, trading is always blocked.
    if level == "DEAD":
        trading_allowed = False
    return CircuitBreakerState(
        level=CircuitBreakerLevel(level),
        constraints=CircuitBreakerConstraints(
            level=CircuitBreakerLevel(level),
            max_leverage=max_lev,
            max_positions=max_pos,
            size_multiplier=size_mult,
            trading_allowed=trading_allowed,
            reason="" if trading_allowed else "Trading halted",
        ),
        balance=balance,
        daily_loss_pct=Decimal("0"),
    )


@pytest.fixture
def sizer() -> PositionSizer:
    return PositionSizer()


# ---------------------------------------------------------------------------
# 1. GREEN level — normal sizing
# ---------------------------------------------------------------------------
class TestGreenLevel:
    def test_normal_sizing(self, sizer: PositionSizer) -> None:
        """GREEN/55%WR/2:1RR should produce a valid, non-zero position."""
        cb = _make_cb_state("GREEN")
        result = sizer.calculate_size(
            balance=_DEFAULT_BALANCE,
            win_rate=Decimal("0.55"),
            rr_ratio=Decimal("2"),
            circuit_breaker_state=cb,
            requested_leverage=1,
        )

        assert isinstance(result, PositionSize)
        assert result.usd_amount > Decimal("0")
        assert result.usd_amount <= _DEFAULT_BALANCE * Decimal("0.15")
        assert result.leverage == 1
        assert result.notional_value == result.usd_amount * result.leverage

    def test_high_leverage(self, sizer: PositionSizer) -> None:
        """GREEN allows up to 10x; requesting 8x should pass through."""
        cb = _make_cb_state("GREEN")
        result = sizer.calculate_size(
            balance=_DEFAULT_BALANCE,
            win_rate=Decimal("0.55"),
            rr_ratio=Decimal("2"),
            circuit_breaker_state=cb,
            requested_leverage=8,
        )

        assert result.leverage == 8
        assert result.notional_value == (
            result.usd_amount * Decimal("8")
        ).quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# 2. YELLOW level — reduced sizing and leverage
# ---------------------------------------------------------------------------
class TestYellowLevel:
    def test_size_reduced_by_half(self, sizer: PositionSizer) -> None:
        """YELLOW multiplier is 0.5x — output should be ~half of GREEN.

        Uses $500 balance so reduced sizes stay above $5 minimum.
        """
        large_balance = Decimal("500")
        green_cb = _make_cb_state("GREEN")
        yellow_cb = _make_cb_state("YELLOW")

        green = sizer.calculate_size(
            large_balance, "0.55", "2", green_cb, requested_leverage=1
        )
        yellow = sizer.calculate_size(
            large_balance, "0.55", "2", yellow_cb, requested_leverage=1
        )

        # Yellow should be strictly smaller (0.5x multiplier applied).
        assert yellow.usd_amount < green.usd_amount
        # And roughly half (allow $0.50 tolerance for quantisation).
        expected_half = (green.usd_amount * Decimal("0.5")).quantize(
            Decimal("0.01")
        )
        assert abs(yellow.usd_amount - expected_half) <= Decimal("0.50")

    def test_leverage_capped_at_5(self, sizer: PositionSizer) -> None:
        """YELLOW caps leverage at 5x even if 10x requested."""
        cb = _make_cb_state("YELLOW")
        result = sizer.calculate_size(
            _DEFAULT_BALANCE, "0.55", "2", cb, requested_leverage=10
        )

        assert result.leverage == 5
        assert "clipped" in result.reason.lower() or "Leverage" in result.reason


# ---------------------------------------------------------------------------
# 3. RED level — survival mode
# ---------------------------------------------------------------------------
class TestRedLevel:
    def test_size_reduced_by_quarter(self, sizer: PositionSizer) -> None:
        """RED multiplier is 0.25x — output should be much smaller than GREEN.

        Uses $500 balance so reduced sizes stay above $5 minimum.
        """
        large_balance = Decimal("500")
        green_cb = _make_cb_state("GREEN")
        red_cb = _make_cb_state("RED")

        green = sizer.calculate_size(
            large_balance, "0.55", "2", green_cb, requested_leverage=1
        )
        red = sizer.calculate_size(
            large_balance, "0.55", "2", red_cb, requested_leverage=1
        )

        # RED size should be less than half of GREEN.
        assert red.usd_amount < green.usd_amount * Decimal("0.5")

    def test_leverage_capped_at_3(self, sizer: PositionSizer) -> None:
        """RED caps leverage at 3x."""
        cb = _make_cb_state("RED")
        result = sizer.calculate_size(
            _DEFAULT_BALANCE, "0.55", "2", cb, requested_leverage=10
        )

        assert result.leverage == 3


# ---------------------------------------------------------------------------
# 4. DEAD level — no trading
# ---------------------------------------------------------------------------
class TestDeadLevel:
    def test_dead_returns_zero(self, sizer: PositionSizer) -> None:
        """DEAD level blocks everything; position must be zero."""
        cb = _make_cb_state("DEAD")
        result = sizer.calculate_size(
            _DEFAULT_BALANCE, "0.55", "2", cb, requested_leverage=1
        )

        assert result.usd_amount == Decimal("0")
        assert result.leverage == 0
        assert result.notional_value == Decimal("0")
        assert result.capped is True
        assert "blocked" in result.reason.lower() or "halted" in result.reason.lower()


# ---------------------------------------------------------------------------
# 5. Trading not allowed (explicit flag)
# ---------------------------------------------------------------------------
class TestTradingNotAllowed:
    def test_returns_zero_when_disallowed(self, sizer: PositionSizer) -> None:
        """Even at GREEN level, if trading_allowed=False, result is zero."""
        cb = _make_cb_state("GREEN", trading_allowed=False)
        result = sizer.calculate_size(
            _DEFAULT_BALANCE, "0.55", "2", cb, requested_leverage=1
        )

        assert result.usd_amount == Decimal("0")
        assert result.leverage == 0
        assert result.capped is True


# ---------------------------------------------------------------------------
# 6. Hard cap — 15 % of balance
# ---------------------------------------------------------------------------
class TestMaxPositionCap:
    def test_cap_at_15_pct(self, sizer: PositionSizer) -> None:
        """A high-edge bet (80%WR, 3:1RR) at GREEN should not exceed 15%."""
        cb = _make_cb_state("GREEN")
        result = sizer.calculate_size(
            _DEFAULT_BALANCE, "0.80", "3", cb, requested_leverage=1
        )

        max_allowed = (_DEFAULT_BALANCE * Decimal("0.15")).quantize(
            Decimal("0.01")
        )
        assert result.usd_amount <= max_allowed


# ---------------------------------------------------------------------------
# 7. Minimum $5 enforcement
# ---------------------------------------------------------------------------
class TestMinimumPosition:
    def test_bumped_to_minimum(self, sizer: PositionSizer) -> None:
        """
        With RED multiplier (0.25x) on a small balance ($35), the Kelly size
        may drop below $5 and must be bumped back up to $5 minimum.
        """
        cb = _make_cb_state("RED", balance=Decimal("35"))
        result = sizer.calculate_size(
            balance=Decimal("35"),
            win_rate="0.55",
            rr_ratio="2",
            circuit_breaker_state=cb,
            requested_leverage=1,
        )

        # With balance $35, 0.25x multiplier, half-kelly ~7.5% fraction:
        # raw ~= 35 * 0.075 = 2.625, * 0.25 = 0.656 -- below $5.
        # Should be bumped to $5.
        assert result.usd_amount >= Decimal("5")
        assert "min" in result.reason.lower() or "bumped" in result.reason.lower()
        assert result.capped is True

    def test_balance_below_5_returns_zero(self, sizer: PositionSizer) -> None:
        """If balance itself is below $5, position is zero."""
        cb = _make_cb_state("GREEN", balance=Decimal("4"))
        result = sizer.calculate_size(
            balance=Decimal("4"),
            win_rate="0.55",
            rr_ratio="2",
            circuit_breaker_state=cb,
            requested_leverage=1,
        )

        # Kelly would give $4 * 0.075 = $0.30, below $5 min.
        # And since balance ($4) < $5, return zero.
        assert result.usd_amount == Decimal("0")
        assert result.capped is True


# ---------------------------------------------------------------------------
# 8. Zero balance
# ---------------------------------------------------------------------------
class TestZeroBalance:
    def test_zero_balance_returns_zero(self, sizer: PositionSizer) -> None:
        """Zero balance means no money to trade."""
        cb = _make_cb_state("GREEN", balance=Decimal("0"))
        result = sizer.calculate_size(
            balance=Decimal("0"),
            win_rate="0.55",
            rr_ratio="2",
            circuit_breaker_state=cb,
            requested_leverage=1,
        )

        assert result.usd_amount == Decimal("0")
        assert result.pct_of_balance == Decimal("0")


# ---------------------------------------------------------------------------
# 9. Very low win rate — negative EV
# ---------------------------------------------------------------------------
class TestLowWinRate:
    def test_low_win_rate_produces_zero_or_min(self, sizer: PositionSizer) -> None:
        """
        30% WR with 2:1 RR has negative Kelly expectation.
        Kelly returns 0 -> position should be 0 or bumped to min (then
        potentially blocked by balance check).
        """
        cb = _make_cb_state("GREEN")
        result = sizer.calculate_size(
            _DEFAULT_BALANCE, "0.30", "2", cb, requested_leverage=1
        )

        # Full Kelly = (0.3*2 - 0.7)/2 = -0.05 -> 0
        # Half Kelly = 0 -> position_size = 0
        # In calculate_size, raw_usd = 0, cb_usd = 0, below $5 min.
        # Balance ($68.33) >= $5, so bumped to $5 min.
        assert result.usd_amount == Decimal("5")
        assert result.capped is True


# ---------------------------------------------------------------------------
# 10. Leverage clipping
# ---------------------------------------------------------------------------
class TestLeverageClipping:
    def test_yellow_clips_10x_to_5x(self, sizer: PositionSizer) -> None:
        """Request 10x under YELLOW — should be clipped to 5x with a reason."""
        cb = _make_cb_state("YELLOW")
        result = sizer.calculate_size(
            _DEFAULT_BALANCE, "0.55", "2", cb, requested_leverage=10
        )

        assert result.leverage == 5
        assert "clipped" in result.reason.lower() or "Leverage" in result.reason

    def test_red_clips_8x_to_3x(self, sizer: PositionSizer) -> None:
        """Request 8x under RED — should be clipped to 3x."""
        cb = _make_cb_state("RED")
        result = sizer.calculate_size(
            _DEFAULT_BALANCE, "0.55", "2", cb, requested_leverage=8
        )

        assert result.leverage == 3
        assert "clipped" in result.reason.lower() or "Leverage" in result.reason

    def test_no_clipping_when_under_limit(self, sizer: PositionSizer) -> None:
        """Request 3x under GREEN (limit 10x) — no clipping."""
        cb = _make_cb_state("GREEN")
        result = sizer.calculate_size(
            _DEFAULT_BALANCE, "0.55", "2", cb, requested_leverage=3
        )

        assert result.leverage == 3
        # No leverage-clipping reason should appear.
        assert "clipped" not in result.reason.lower()


# ---------------------------------------------------------------------------
# 11. Notional value = usd_amount * leverage
# ---------------------------------------------------------------------------
class TestNotionalValue:
    @pytest.mark.parametrize("leverage", [1, 3, 5, 8])
    def test_notional_equals_amount_times_leverage(
        self, sizer: PositionSizer, leverage: int
    ) -> None:
        """notional_value must always equal usd_amount * leverage."""
        cb = _make_cb_state("GREEN")  # allows up to 10x
        result = sizer.calculate_size(
            _DEFAULT_BALANCE, "0.55", "2", cb, requested_leverage=leverage
        )

        expected = (result.usd_amount * Decimal(result.leverage)).quantize(
            Decimal("0.01")
        )
        assert result.notional_value == expected


# ---------------------------------------------------------------------------
# 12. pct_of_balance correctness
# ---------------------------------------------------------------------------
class TestPctOfBalance:
    def test_pct_correctly_computed(self, sizer: PositionSizer) -> None:
        """pct_of_balance should equal usd_amount / balance (quantised)."""
        cb = _make_cb_state("GREEN")
        result = sizer.calculate_size(
            _DEFAULT_BALANCE, "0.55", "2", cb, requested_leverage=1
        )

        expected_pct = (result.usd_amount / _DEFAULT_BALANCE).quantize(
            Decimal("0.0001")
        )
        assert result.pct_of_balance == expected_pct
        assert Decimal("0") < result.pct_of_balance <= Decimal("0.15")


# ---------------------------------------------------------------------------
# 13. Capped flag behaviour
# ---------------------------------------------------------------------------
class TestCappedFlag:
    def test_capped_true_when_cb_reduces(self, sizer: PositionSizer) -> None:
        """YELLOW multiplier reduces raw size -> capped should be True."""
        cb = _make_cb_state("YELLOW")
        result = sizer.calculate_size(
            _DEFAULT_BALANCE, "0.55", "2", cb, requested_leverage=1
        )

        assert result.capped is True

    def test_capped_false_when_no_adjustments(self, sizer: PositionSizer) -> None:
        """GREEN with moderate parameters — no adjustments, capped=False."""
        cb = _make_cb_state("GREEN")
        result = sizer.calculate_size(
            _DEFAULT_BALANCE, "0.55", "2", cb, requested_leverage=1
        )

        # At GREEN/1x leverage, the raw Kelly is within limits.
        # Only if no reasons accumulated is capped False.
        # Kelly gives ~5.12, which is >= $5 min and <= 15% cap.
        # Verify no adjustments were made.
        if result.reason == "":
            assert result.capped is False


# ---------------------------------------------------------------------------
# 14. Reason string contains explanation when capped
# ---------------------------------------------------------------------------
class TestReasonString:
    def test_reason_on_cb_multiplier(self, sizer: PositionSizer) -> None:
        """When CB multiplier reduces size, reason mentions it."""
        cb = _make_cb_state("YELLOW")
        result = sizer.calculate_size(
            _DEFAULT_BALANCE, "0.55", "2", cb, requested_leverage=1
        )

        assert "multiplier" in result.reason.lower() or "CB" in result.reason

    def test_reason_on_leverage_clip(self, sizer: PositionSizer) -> None:
        """When leverage is clipped, reason mentions the clipping."""
        cb = _make_cb_state("YELLOW")
        result = sizer.calculate_size(
            _DEFAULT_BALANCE, "0.55", "2", cb, requested_leverage=10
        )

        assert "leverage" in result.reason.lower() or "clipped" in result.reason.lower()

    def test_reason_on_min_bump(self, sizer: PositionSizer) -> None:
        """When size is bumped to $5 minimum, reason explains."""
        cb = _make_cb_state("RED")
        result = sizer.calculate_size(
            balance=Decimal("35"),
            win_rate="0.55",
            rr_ratio="2",
            circuit_breaker_state=cb,
            requested_leverage=1,
        )

        assert "min" in result.reason.lower() or "bumped" in result.reason.lower()

    def test_empty_reason_when_no_adjustments(self, sizer: PositionSizer) -> None:
        """When nothing is adjusted, reason is empty."""
        cb = _make_cb_state("GREEN")
        result = sizer.calculate_size(
            _DEFAULT_BALANCE, "0.55", "2", cb, requested_leverage=1
        )

        # GREEN, moderate parameters, 1x leverage — should have no adjustments.
        assert result.reason == ""
