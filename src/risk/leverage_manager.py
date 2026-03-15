"""
Leverage Manager — maps (confidence, regime, circuit-breaker level) to a
safe integer leverage value and computes liquidation buffers.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field

from src.risk.circuit_breaker import CircuitBreakerLevel

logger = logging.getLogger("claude_quant.risk.leverage_manager")

# Minimum acceptable distance from entry to estimated liquidation price.
_MIN_LIQUIDATION_BUFFER_PCT: Decimal = Decimal("0.05")  # 5 %


class MarketRegime(str, Enum):
    """Market-regime labels matching RegimeDetector output."""

    TRENDING = "trending"
    VOLATILE = "volatile"
    RANGING = "ranging"
    QUIET = "quiet"


class LeverageResult(BaseModel):
    """Outcome of a leverage determination."""

    model_config = {"frozen": True}

    leverage: int = Field(ge=0, description="Approved leverage (0 = no trade).")
    raw_leverage: int = Field(ge=0, description="Leverage before CB override.")
    capped_by_cb: bool = Field(
        default=False, description="True if circuit breaker reduced leverage."
    )
    reason: str = ""


class LiquidationBuffer(BaseModel):
    """Distance-to-liquidation analysis."""

    model_config = {"frozen": True}

    entry_price: Decimal
    estimated_liquidation_price: Decimal
    buffer_pct: Decimal = Field(description="Distance as a fraction (0-1).")
    is_safe: bool = Field(description="True if buffer >= 5%.")
    leverage: int


# ---------------------------------------------------------------------------
# Lookup table: (confidence_band, regime) -> (min_leverage, max_leverage)
# The midpoint is used as the default; callers can bias within the range.
# ---------------------------------------------------------------------------
_LEVERAGE_TABLE: list[
    tuple[int, int, set[MarketRegime], int, int]
] = [
    # (conf_min, conf_max, regimes, lev_min, lev_max)
    # High confidence trending: 7-10x (midpoint 8)
    (80, 100, {MarketRegime.TRENDING}, 7, 10),
    # Good confidence trending: 5-7x (midpoint 6)
    (60, 79, {MarketRegime.TRENDING}, 5, 7),
    # Good confidence volatile: 3-5x (midpoint 4)
    (60, 79, {MarketRegime.VOLATILE}, 3, 5),
    # Good confidence ranging: 3-5x (midpoint 4)
    (60, 79, {MarketRegime.RANGING}, 3, 5),
    # Moderate confidence trending: 3-5x (midpoint 4)
    (40, 59, {MarketRegime.TRENDING}, 3, 5),
    # Moderate confidence volatile: 2-3x (midpoint 2)
    (40, 59, {MarketRegime.VOLATILE}, 2, 3),
    # Moderate confidence ranging: 2-3x (midpoint 2)
    (40, 59, {MarketRegime.RANGING}, 2, 3),
    # Low confidence — conservative 2x (strategy gates already validated setup)
    (25, 39, {MarketRegime.TRENDING}, 2, 3),
    (25, 39, {MarketRegime.VOLATILE}, 1, 2),
    (25, 39, {MarketRegime.RANGING}, 2, 3),
]

# Circuit-breaker leverage caps by level (DEAD = 0 handled specially).
_CB_LEVERAGE_CAPS: dict[CircuitBreakerLevel, int] = {
    CircuitBreakerLevel.GREEN: 10,
    CircuitBreakerLevel.YELLOW: 5,
    CircuitBreakerLevel.RED: 3,
    CircuitBreakerLevel.DEAD: 0,
}


class LeverageManager:
    """Determines safe leverage and validates liquidation buffers."""

    @staticmethod
    def determine_leverage(
        confidence: int | float,
        regime: MarketRegime | str,
        circuit_breaker_level: CircuitBreakerLevel,
    ) -> LeverageResult:
        """
        Look up the leverage for a trade opportunity.

        Parameters
        ----------
        confidence:
            Model confidence in the signal, 0-100.
        regime:
            Current market regime label.
        circuit_breaker_level:
            Active circuit-breaker level.

        Returns
        -------
        LeverageResult
            ``leverage == 0`` means **do not trade**.
        """
        confidence = int(confidence)

        # Normalise regime to enum.
        if isinstance(regime, str):
            try:
                regime = MarketRegime(regime)
            except ValueError:
                logger.warning("Unknown regime '%s' — defaulting to VOLATILE.", regime)
                regime = MarketRegime.VOLATILE

        # --- QUIET regime: no trade ----------------------------------------
        if regime == MarketRegime.QUIET:
            return LeverageResult(
                leverage=0,
                raw_leverage=0,
                reason="Regime QUIET — no trading.",
            )

        # --- DEAD: instant reject -----------------------------------------
        if circuit_breaker_level == CircuitBreakerLevel.DEAD:
            return LeverageResult(
                leverage=0,
                raw_leverage=0,
                capped_by_cb=True,
                reason="Circuit breaker DEAD — no trading.",
            )

        # --- Confidence too low: no trade ---------------------------------
        if confidence < 25:
            return LeverageResult(
                leverage=0,
                raw_leverage=0,
                reason=f"Confidence {confidence} < 25 — signal too weak, no trade.",
            )

        # --- Lookup -------------------------------------------------------
        raw_leverage = 0
        for conf_min, conf_max, regimes, lev_min, lev_max in _LEVERAGE_TABLE:
            if conf_min <= confidence <= conf_max and regime in regimes:
                # Pick the midpoint of the range (rounded down).
                raw_leverage = (lev_min + lev_max) // 2
                break

        if raw_leverage == 0:
            # No matching tier — fall back to conservative default.
            raw_leverage = 2
            logger.info(
                "No exact tier match for confidence=%d regime=%s. "
                "Falling back to %dx leverage.",
                confidence,
                regime.value,
                raw_leverage,
            )

        # --- Apply circuit-breaker cap ------------------------------------
        cb_cap = _CB_LEVERAGE_CAPS[circuit_breaker_level]
        capped = raw_leverage > cb_cap
        leverage = min(raw_leverage, cb_cap)

        reason_parts: list[str] = [
            f"Tier lookup: {raw_leverage}x for confidence={confidence}, regime={regime.value}."
        ]
        if capped:
            reason_parts.append(
                f"Capped from {raw_leverage}x to {leverage}x by CB level "
                f"{circuit_breaker_level.value}."
            )

        result = LeverageResult(
            leverage=leverage,
            raw_leverage=raw_leverage,
            capped_by_cb=capped,
            reason=" ".join(reason_parts),
        )

        logger.info(
            "Leverage determined: %dx (raw %dx, CB %s). %s",
            result.leverage,
            result.raw_leverage,
            circuit_breaker_level.value,
            result.reason,
        )
        return result

    @staticmethod
    def calculate_liquidation_buffer(
        entry_price: Decimal | float | str,
        leverage: int,
        direction: str = "long",
    ) -> LiquidationBuffer:
        """
        Estimate the liquidation price and verify the buffer is >5 %.

        This uses a simplified exchange model:
            Long  liq = entry * (1 - 1/leverage)
            Short liq = entry * (1 + 1/leverage)

        Real exchange liquidation engines include funding, fees, and
        maintenance margin — this intentionally overestimates risk.

        Parameters
        ----------
        entry_price:
            The order entry price.
        leverage:
            Integer leverage.
        direction:
            ``"long"`` or ``"short"``.

        Returns
        -------
        LiquidationBuffer
        """
        entry = Decimal(str(entry_price))
        direction = direction.strip().lower()

        if leverage <= 0:
            # No leverage → no liquidation risk.
            return LiquidationBuffer(
                entry_price=entry,
                estimated_liquidation_price=Decimal("0"),
                buffer_pct=Decimal("1"),
                is_safe=True,
                leverage=0,
            )

        inverse_lev = Decimal("1") / Decimal(leverage)

        if direction == "long":
            liq_price = (entry * (Decimal("1") - inverse_lev)).quantize(
                Decimal("0.00000001")
            )
        elif direction == "short":
            liq_price = (entry * (Decimal("1") + inverse_lev)).quantize(
                Decimal("0.00000001")
            )
        else:
            raise ValueError(f"direction must be 'long' or 'short', got '{direction}'")

        buffer_pct = abs(entry - liq_price) / entry if entry > 0 else Decimal("0")
        is_safe = buffer_pct >= _MIN_LIQUIDATION_BUFFER_PCT

        result = LiquidationBuffer(
            entry_price=entry,
            estimated_liquidation_price=liq_price,
            buffer_pct=buffer_pct.quantize(Decimal("0.0001")),
            is_safe=is_safe,
            leverage=leverage,
        )

        if not is_safe:
            logger.warning(
                "Liquidation buffer %.2f%% is below minimum 5%% at %dx leverage! "
                "Entry=%.8f Liq=%.8f.",
                buffer_pct * 100,
                leverage,
                entry,
                liq_price,
            )

        return result
