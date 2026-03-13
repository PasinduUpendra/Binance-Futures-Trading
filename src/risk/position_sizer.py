"""
Position Sizer — translates Kelly-optimal fractions into concrete trade sizes
that respect circuit-breaker constraints.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from pydantic import BaseModel, Field

from src.risk.circuit_breaker import CircuitBreakerState
from src.risk.kelly_criterion import KellyCriterion

logger = logging.getLogger("claude_quant.risk.position_sizer")

# Hard limits (matching CLAUDE.md immutable rules).
_MIN_POSITION_USD: Decimal = Decimal("5")
_MAX_POSITION_PCT: Decimal = Decimal("0.15")  # 15 % of balance


class PositionSize(BaseModel):
    """Computed position-size details attached to a trade proposal."""

    model_config = {"frozen": True}

    usd_amount: Decimal = Field(
        description="Dollar amount allocated to the position (margin)."
    )
    pct_of_balance: Decimal = Field(
        description="Position size as a fraction of total balance (0-1)."
    )
    leverage: int = Field(
        ge=0, description="Leverage to apply (bounded by circuit breaker)."
    )
    notional_value: Decimal = Field(
        description="Effective notional exposure (usd_amount * leverage)."
    )
    capped: bool = Field(
        default=False,
        description="True if the raw Kelly size was capped down by a limit.",
    )
    reason: str = Field(
        default="",
        description="Human-readable explanation of sizing decisions.",
    )


class PositionSizer:
    """
    Stateless position-size calculator.

    Combines :class:`KellyCriterion` output with :class:`CircuitBreakerState`
    constraints to produce a fully validated :class:`PositionSize`.
    """

    def __init__(self) -> None:
        self._kelly = KellyCriterion()

    def calculate_size(
        self,
        balance: Decimal | float | str,
        win_rate: Decimal | float | str,
        rr_ratio: Decimal | float | str,
        circuit_breaker_state: CircuitBreakerState,
        requested_leverage: int = 1,
    ) -> PositionSize:
        """
        Compute a safe position size.

        Parameters
        ----------
        balance:
            Current account balance in USD.
        win_rate:
            Historical win probability (0-1).
        rr_ratio:
            Risk-reward ratio (avg_win / avg_loss).
        circuit_breaker_state:
            The latest state from :meth:`CircuitBreaker.is_trading_allowed`.
        requested_leverage:
            Leverage desired by the strategy (will be clipped to CB cap).

        Returns
        -------
        PositionSize
        """
        balance = Decimal(str(balance))
        constraints = circuit_breaker_state.constraints
        reasons: list[str] = []

        # --- 0. Trading blocked? ------------------------------------------
        if not constraints.trading_allowed:
            reasons.append(f"Trading blocked: {constraints.reason}")
            return PositionSize(
                usd_amount=Decimal("0"),
                pct_of_balance=Decimal("0"),
                leverage=0,
                notional_value=Decimal("0"),
                capped=True,
                reason=" | ".join(reasons),
            )

        # --- 1. Kelly-optimal USD amount ----------------------------------
        raw_usd = self._kelly.position_size(balance, win_rate, rr_ratio)

        # --- 2. Apply circuit-breaker size multiplier ---------------------
        cb_usd = (raw_usd * constraints.size_multiplier).quantize(Decimal("0.01"))
        if cb_usd < raw_usd:
            reasons.append(
                f"CB multiplier {constraints.size_multiplier} reduced "
                f"${raw_usd} -> ${cb_usd}."
            )

        # --- 3. Enforce 15 % of balance cap (hard rule) -------------------
        max_usd = (balance * _MAX_POSITION_PCT).quantize(Decimal("0.01"))
        if cb_usd > max_usd:
            reasons.append(
                f"Capped at 15% of balance (${max_usd})."
            )
            cb_usd = max_usd

        # --- 4. Enforce minimum position ----------------------------------
        capped = bool(reasons)
        if cb_usd < _MIN_POSITION_USD:
            if balance < _MIN_POSITION_USD:
                reasons.append(
                    f"Balance ${balance} below minimum position ${_MIN_POSITION_USD}."
                )
                return PositionSize(
                    usd_amount=Decimal("0"),
                    pct_of_balance=Decimal("0"),
                    leverage=0,
                    notional_value=Decimal("0"),
                    capped=True,
                    reason=" | ".join(reasons),
                )
            # Bump to the minimum.
            reasons.append(
                f"Kelly size ${cb_usd} below min — bumped to ${_MIN_POSITION_USD}."
            )
            cb_usd = _MIN_POSITION_USD
            capped = True

        # --- 5. Resolve leverage ------------------------------------------
        leverage = min(requested_leverage, constraints.max_leverage)
        if leverage < 1:
            leverage = 1  # 1x = no leverage, but still a valid trade
        if leverage != requested_leverage:
            reasons.append(
                f"Leverage clipped from {requested_leverage}x to {leverage}x "
                f"(CB level {constraints.level.value})."
            )

        # --- 6. Notional value --------------------------------------------
        notional = (cb_usd * Decimal(leverage)).quantize(Decimal("0.01"))

        pct = (cb_usd / balance).quantize(Decimal("0.0001")) if balance > 0 else Decimal("0")

        logger.info(
            "Position sized: $%.2f (%.2f%% of $%.2f) x %dx = $%.2f notional. %s",
            cb_usd,
            pct * 100,
            balance,
            leverage,
            notional,
            " | ".join(reasons) if reasons else "No adjustments.",
        )

        return PositionSize(
            usd_amount=cb_usd,
            pct_of_balance=pct,
            leverage=leverage,
            notional_value=notional,
            capped=capped,
            reason=" | ".join(reasons),
        )
