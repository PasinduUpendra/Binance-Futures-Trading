"""
Math sanity-check layer for anti-hallucination.

Provides static, purely arithmetic validation methods that catch impossible
numbers before they reach the execution layer.  Every method returns a
``(valid, details)`` tuple so callers get a human-readable explanation on
failure.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

logger = logging.getLogger("claude_quant.anti_hallucination.sanity_checks")


# ---------------------------------------------------------------------------
# Type alias used throughout
# ---------------------------------------------------------------------------

SanityResult = tuple[bool, str]


# ---------------------------------------------------------------------------
# SanityChecker
# ---------------------------------------------------------------------------


class SanityChecker:
    """Collection of static arithmetic sanity checks.

    All methods are ``@staticmethod`` — no instance state is needed.
    Each returns ``(valid: bool, details: str)``.
    """

    @staticmethod
    def check_position_math(
        balance: Decimal,
        size: Decimal,
        leverage: int,
        notional: Decimal,
    ) -> SanityResult:
        """Verify: ``notional == size * leverage`` and ``size <= balance * max_position_pct``.

        Parameters
        ----------
        balance : Decimal
            Current account balance.
        size : Decimal
            Margin (collateral) allocated to the position.
        leverage : int
            Leverage factor.
        notional : Decimal
            Total notional value of the position.

        Returns
        -------
        (valid, details)
        """
        try:
            expected_notional = size * Decimal(str(leverage))

            # Allow 0.1 % tolerance for rounding
            tol = Decimal("0.001")
            if expected_notional == Decimal("0"):
                if notional != Decimal("0"):
                    return (
                        False,
                        f"Expected notional 0 (size={size}, lev={leverage}) but got {notional}",
                    )
                return True, "OK — zero position"

            diff = abs(notional - expected_notional) / expected_notional
            if diff > tol:
                return (
                    False,
                    f"Notional mismatch: size({size}) * leverage({leverage}) = "
                    f"{expected_notional}, but reported {notional} (diff={diff:.4%})",
                )

            # Size must not exceed balance
            if size > balance:
                return (
                    False,
                    f"Position size {size} exceeds balance {balance}",
                )

            # Max 15 % of balance per trade (IMMUTABLE RULE #4)
            max_size = balance * Decimal("0.15")
            if size > max_size:
                return (
                    False,
                    f"Position size {size} exceeds 15% of balance ({max_size})",
                )

            return True, "Position math OK"

        except (InvalidOperation, ZeroDivisionError) as exc:
            return False, f"Arithmetic error in position math: {exc}"

    @staticmethod
    def check_pnl_math(
        entry: Decimal,
        exit_price: Decimal,
        size: Decimal,
        direction: str,
        reported_pnl: Decimal,
    ) -> SanityResult:
        """Verify that the reported P&L matches the entry/exit/size/direction.

        For longs:  PnL = (exit - entry) / entry * notional_value
        For shorts: PnL = (entry - exit) / entry * notional_value

        We compare at the Decimal level.  ``size`` here is the *notional*
        (quantity * entry, or margin * leverage) depending on convention.

        Parameters
        ----------
        entry, exit_price : Decimal
        size : Decimal
            Notional value of the position.
        direction : str
            ``"long"`` or ``"short"``.
        reported_pnl : Decimal

        Returns
        -------
        (valid, details)
        """
        try:
            if entry <= Decimal("0"):
                return False, f"Entry price must be positive, got {entry}"

            if direction == "long":
                expected = (exit_price - entry) / entry * size
            elif direction == "short":
                expected = (entry - exit_price) / entry * size
            else:
                return False, f"Unknown direction '{direction}'"

            # Tolerance: allow 1 % difference (fees/funding may cause drift)
            if expected == Decimal("0"):
                if abs(reported_pnl) > Decimal("0.01"):
                    return (
                        False,
                        f"Expected PnL ~0 but reported {reported_pnl}",
                    )
                return True, "PnL math OK (both ~0)"

            diff = abs(reported_pnl - expected) / abs(expected)
            if diff > Decimal("0.01"):
                return (
                    False,
                    f"PnL mismatch: expected {expected:.4f}, reported {reported_pnl:.4f} "
                    f"(diff={diff:.4%}) — entry={entry}, exit={exit_price}, "
                    f"size={size}, dir={direction}",
                )

            return True, f"PnL math OK (expected={expected:.4f}, reported={reported_pnl:.4f})"

        except (InvalidOperation, ZeroDivisionError) as exc:
            return False, f"Arithmetic error in PnL check: {exc}"

    @staticmethod
    def check_leverage_bounds(
        leverage: int,
        max_allowed: int,
    ) -> SanityResult:
        """Leverage must be between 1 and *max_allowed* (inclusive).

        The system-wide absolute maximum is 10x (IMMUTABLE RULE #2).

        Parameters
        ----------
        leverage : int
        max_allowed : int
            Context-dependent maximum (may be reduced by circuit breaker).
        """
        absolute_max = 10  # HARDCODED — IMMUTABLE RULE #2

        if leverage < 1:
            return False, f"Leverage {leverage} is below minimum 1x"

        if leverage > absolute_max:
            return (
                False,
                f"Leverage {leverage}x exceeds absolute maximum {absolute_max}x "
                f"(IMMUTABLE RULE)",
            )

        if leverage > max_allowed:
            return (
                False,
                f"Leverage {leverage}x exceeds current allowed maximum {max_allowed}x "
                f"(circuit breaker or risk limit)",
            )

        return True, f"Leverage {leverage}x within bounds (max allowed={max_allowed}x)"

    @staticmethod
    def check_liquidation_distance(
        entry: Decimal,
        liquidation: Decimal,
        leverage: int,
    ) -> SanityResult:
        """Liquidation price must be >5 % away from entry.

        A thin liquidation buffer dramatically increases the probability of
        cascade liquidation in fast markets.

        Parameters
        ----------
        entry : Decimal
        liquidation : Decimal
        leverage : int

        Returns
        -------
        (valid, details)
        """
        try:
            if entry <= Decimal("0"):
                return False, f"Entry price must be positive, got {entry}"
            if liquidation <= Decimal("0"):
                return False, f"Liquidation price must be positive, got {liquidation}"

            distance_pct = abs(entry - liquidation) / entry
            min_distance = Decimal("0.05")  # 5 %

            if distance_pct < min_distance:
                return (
                    False,
                    f"Liquidation distance {distance_pct:.2%} is below minimum "
                    f"{min_distance:.0%} (entry={entry}, liq={liquidation}, "
                    f"lev={leverage}x)",
                )

            # Cross-check: theoretical liquidation distance ~ 1/leverage
            # Allow generous tolerance because maintenance margin varies.
            theoretical = Decimal("1") / Decimal(str(leverage))
            if distance_pct > theoretical * Decimal("1.5"):
                logger.info(
                    "Liquidation distance %s is wider than theoretical %s "
                    "(maintenance margin may be large or calculation is off)",
                    f"{distance_pct:.2%}",
                    f"{theoretical:.2%}",
                )

            return (
                True,
                f"Liquidation distance OK: {distance_pct:.2%} >= {min_distance:.0%} "
                f"(entry={entry}, liq={liquidation})",
            )

        except (InvalidOperation, ZeroDivisionError) as exc:
            return False, f"Arithmetic error in liquidation check: {exc}"

    @staticmethod
    def check_balance_consistency(
        reported_balance: Decimal,
        positions: list[dict[str, Decimal]],
        free_margin: Decimal,
    ) -> SanityResult:
        """Verify: balance == free_margin + sum(position margins).

        Parameters
        ----------
        reported_balance : Decimal
            The total account balance as reported by the exchange.
        positions : list[dict[str, Decimal]]
            List of open positions, each with at least a ``"margin"`` key.
        free_margin : Decimal
            Unused margin.

        Returns
        -------
        (valid, details)
        """
        try:
            total_margin = sum(
                Decimal(str(p.get("margin", 0))) for p in positions
            )
            expected_balance = free_margin + total_margin

            if reported_balance <= Decimal("0"):
                return False, f"Reported balance {reported_balance} is non-positive"

            # Allow 0.5 % tolerance for unrealised PnL / funding rate drift
            tol = Decimal("0.005")
            diff = abs(reported_balance - expected_balance) / reported_balance

            if diff > tol:
                return (
                    False,
                    f"Balance inconsistency: reported={reported_balance}, "
                    f"computed={expected_balance} (free={free_margin} + "
                    f"margins={total_margin}), diff={diff:.4%}",
                )

            return (
                True,
                f"Balance consistent: {reported_balance} ~ "
                f"{free_margin} (free) + {total_margin} (margins)",
            )

        except (InvalidOperation, ZeroDivisionError) as exc:
            return False, f"Arithmetic error in balance check: {exc}"

    # -- convenience: run all checks that apply to a pre-trade context -------

    @classmethod
    def run_pre_trade_checks(
        cls,
        balance: Decimal,
        size: Decimal,
        leverage: int,
        notional: Decimal,
        max_leverage: int,
        entry: Decimal,
        liquidation: Decimal,
    ) -> tuple[bool, list[str]]:
        """Run position math, leverage bounds, and liquidation distance checks.

        Returns ``(all_passed, list_of_details)``.
        """
        results: list[SanityResult] = [
            cls.check_position_math(balance, size, leverage, notional),
            cls.check_leverage_bounds(leverage, max_leverage),
            cls.check_liquidation_distance(entry, liquidation, leverage),
        ]

        all_passed = all(ok for ok, _ in results)
        details = [detail for _, detail in results]

        if not all_passed:
            logger.warning(
                "Pre-trade sanity checks FAILED: %s",
                " | ".join(d for ok, d in results if not ok),
            )

        return all_passed, details
