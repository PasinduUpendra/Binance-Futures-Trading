"""
Kelly Criterion — optimal bet-sizing under uncertainty.

This module provides full-Kelly and half-Kelly calculations as well as a
convenience method that returns a dollar-denominated position size capped at
15 % of balance.  All arithmetic uses ``Decimal`` for precision.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

logger = logging.getLogger("claude_quant.risk.kelly_criterion")

# Hard cap: no single position may exceed this fraction of balance.
_MAX_POSITION_FRACTION: Decimal = Decimal("0.15")


class KellyCriterion:
    """
    Stateless calculator for Kelly-optimal position fractions.

    All public methods are ``@staticmethod`` so the class can be used
    without instantiation, but an instance is also fine if you prefer
    dependency-injection patterns.
    """

    @staticmethod
    def calculate(
        win_rate: Decimal | float | str,
        avg_win: Decimal | float | str,
        avg_loss: Decimal | float | str,
    ) -> Decimal:
        """
        Full-Kelly fraction: ``f* = (p * b - q) / b``

        where
            p = win probability,
            q = 1 - p,
            b = avg_win / avg_loss  (odds expressed as multiples of one unit of loss).

        Returns
        -------
        Decimal
            Optimal fraction of bankroll to risk.  Returns ``Decimal("0")``
            when the expected value is non-positive or inputs are invalid.
        """
        try:
            p = Decimal(str(win_rate))
            w = Decimal(str(avg_win))
            l = Decimal(str(avg_loss))  # noqa: E741
        except (InvalidOperation, ValueError) as exc:
            logger.warning("Invalid inputs to Kelly calculate: %s", exc)
            return Decimal("0")

        if p <= Decimal("0") or p >= Decimal("1"):
            logger.warning("Win rate out of valid range (0, 1): %s", p)
            return Decimal("0")
        if w <= Decimal("0") or l <= Decimal("0"):
            logger.warning("avg_win and avg_loss must be > 0. Got w=%s l=%s", w, l)
            return Decimal("0")

        q = Decimal("1") - p
        b = w / l  # "odds" — how many units you win per unit risked

        # f* = (p * b - q) / b
        kelly = (p * b - q) / b

        if kelly <= Decimal("0"):
            logger.info(
                "Negative-EV bet detected (kelly=%.6f). Returning 0.", kelly
            )
            return Decimal("0")

        # Cap at hard maximum.
        if kelly > _MAX_POSITION_FRACTION:
            logger.info(
                "Kelly %.4f exceeds max fraction %.4f — capping.",
                kelly,
                _MAX_POSITION_FRACTION,
            )
            kelly = _MAX_POSITION_FRACTION

        return kelly

    @staticmethod
    def half_kelly(
        win_rate: Decimal | float | str,
        avg_win: Decimal | float | str,
        avg_loss: Decimal | float | str,
    ) -> Decimal:
        """
        Half-Kelly: ``0.5 * f*``

        A conservative variant that halves variance at the cost of
        roughly 25 % of long-run growth.
        """
        full = KellyCriterion.calculate(win_rate, avg_win, avg_loss)
        half = full * Decimal("0.5")
        # Still respect the max cap (unlikely to breach after halving, but safe).
        return min(half, _MAX_POSITION_FRACTION)

    @staticmethod
    def position_size(
        balance: Decimal | float | str,
        win_rate: Decimal | float | str,
        risk_reward_ratio: Decimal | float | str,
    ) -> Decimal:
        """
        Convenience method: convert a Kelly fraction into a USD amount.

        Uses *half-Kelly* internally for safety.  The risk-reward ratio
        is interpreted as ``avg_win / avg_loss`` (e.g. 2.0 means you
        expect to win $2 for every $1 you risk).

        Returns
        -------
        Decimal
            USD amount to risk on the trade.  At most 15 % of balance,
            at least ``Decimal("0")``.
        """
        try:
            bal = Decimal(str(balance))
            rr = Decimal(str(risk_reward_ratio))
        except (InvalidOperation, ValueError) as exc:
            logger.warning("Invalid inputs to position_size: %s", exc)
            return Decimal("0")

        if bal <= Decimal("0"):
            return Decimal("0")

        # avg_loss normalised to 1, avg_win = rr.
        fraction = KellyCriterion.half_kelly(win_rate, rr, Decimal("1"))
        if fraction <= Decimal("0"):
            return Decimal("0")

        usd = (bal * fraction).quantize(Decimal("0.01"))
        max_usd = (bal * _MAX_POSITION_FRACTION).quantize(Decimal("0.01"))
        return min(usd, max_usd)
