"""
Fee calculator for Binance Futures trades.

Provides accurate fee calculations including maker/taker rates, BNB
discounts, and fee-adjusted take-profit levels. All monetary values
use Decimal for precision.
"""

from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP

logger = logging.getLogger("claude_quant.execution.fee_calculator")

# ---------------------------------------------------------------------------
# Binance Futures fee schedule (default VIP 0 tier)
# ---------------------------------------------------------------------------

# Standard rates (as of 2025)
DEFAULT_MAKER_FEE = Decimal("0.0002")  # 0.02%
DEFAULT_TAKER_FEE = Decimal("0.0004")  # 0.04%

# BNB discount rates (10% discount when paying fees with BNB)
BNB_DISCOUNT = Decimal("0.10")  # 10% discount


class FeeCalculator:
    """Calculator for Binance Futures trading fees.

    Supports maker/taker fee tiers, BNB discount, and fee-adjusted
    take-profit calculations.

    Parameters
    ----------
    maker_fee : Decimal
        Maker fee rate (default 0.02% = 0.0002).
    taker_fee : Decimal
        Taker fee rate (default 0.04% = 0.0004).
    use_bnb_discount : bool
        If True, apply the 10% BNB fee discount.
    """

    def __init__(
        self,
        maker_fee: Decimal = DEFAULT_MAKER_FEE,
        taker_fee: Decimal = DEFAULT_TAKER_FEE,
        use_bnb_discount: bool = False,
    ) -> None:
        self._base_maker_fee = maker_fee
        self._base_taker_fee = taker_fee
        self._use_bnb_discount = use_bnb_discount

        if use_bnb_discount:
            self._maker_fee = maker_fee * (Decimal("1") - BNB_DISCOUNT)
            self._taker_fee = taker_fee * (Decimal("1") - BNB_DISCOUNT)
        else:
            self._maker_fee = maker_fee
            self._taker_fee = taker_fee

        logger.info(
            "FeeCalculator initialized: maker=%s taker=%s bnb_discount=%s",
            self._maker_fee,
            self._taker_fee,
            use_bnb_discount,
        )

    @property
    def maker_fee_rate(self) -> Decimal:
        """Current maker fee rate (after discounts)."""
        return self._maker_fee

    @property
    def taker_fee_rate(self) -> Decimal:
        """Current taker fee rate (after discounts)."""
        return self._taker_fee

    def calculate_fees(
        self,
        notional_value: Decimal,
        is_maker: bool = False,
    ) -> Decimal:
        """Calculate the fee for a single trade.

        Parameters
        ----------
        notional_value : Decimal
            The notional value of the trade (price * quantity).
        is_maker : bool
            If True, use maker fee rate; otherwise taker fee rate.

        Returns
        -------
        Decimal
            Fee amount in quote currency (USDT).
        """
        rate = self._maker_fee if is_maker else self._taker_fee
        fee = (abs(notional_value) * rate).quantize(
            Decimal("0.00000001"), rounding=ROUND_HALF_UP
        )
        logger.debug(
            "CALC_FEE notional=%s is_maker=%s rate=%s fee=%s",
            notional_value,
            is_maker,
            rate,
            fee,
        )
        return fee

    def calculate_total_cost(
        self,
        entry_price: Decimal,
        exit_price: Decimal,
        size: Decimal,
        leverage: int = 1,
        entry_is_maker: bool = False,
        exit_is_maker: bool = False,
    ) -> dict[str, Decimal]:
        """Calculate total cost of a round-trip trade including fees.

        Parameters
        ----------
        entry_price : Decimal
            Entry price per unit.
        exit_price : Decimal
            Exit price per unit.
        size : Decimal
            Position size in base currency units.
        leverage : int
            Leverage used (affects margin requirement, not fees).
        entry_is_maker : bool
            Whether the entry order is a maker order.
        exit_is_maker : bool
            Whether the exit order is a maker order.

        Returns
        -------
        dict[str, Decimal]
            Dictionary with keys:
            - ``entry_notional``: Entry notional value
            - ``exit_notional``: Exit notional value
            - ``entry_fee``: Fee on entry trade
            - ``exit_fee``: Fee on exit trade
            - ``total_fees``: Sum of entry + exit fees
            - ``gross_pnl``: PnL before fees
            - ``net_pnl``: PnL after fees
            - ``margin_used``: Margin required (notional / leverage)
            - ``net_roi``: Net return on margin (net_pnl / margin_used)
        """
        entry_notional = abs(entry_price * size)
        exit_notional = abs(exit_price * size)

        entry_fee = self.calculate_fees(entry_notional, is_maker=entry_is_maker)
        exit_fee = self.calculate_fees(exit_notional, is_maker=exit_is_maker)
        total_fees = entry_fee + exit_fee

        gross_pnl = (exit_price - entry_price) * size
        net_pnl = gross_pnl - total_fees

        leverage_dec = Decimal(str(leverage))
        margin_used = entry_notional / leverage_dec if leverage_dec > 0 else entry_notional

        net_roi = (
            (net_pnl / margin_used).quantize(
                Decimal("0.00000001"), rounding=ROUND_HALF_UP
            )
            if margin_used > 0
            else Decimal("0")
        )

        result = {
            "entry_notional": entry_notional,
            "exit_notional": exit_notional,
            "entry_fee": entry_fee,
            "exit_fee": exit_fee,
            "total_fees": total_fees,
            "gross_pnl": gross_pnl,
            "net_pnl": net_pnl,
            "margin_used": margin_used,
            "net_roi": net_roi,
        }

        logger.info(
            "CALC_TOTAL_COST entry=%s exit=%s size=%s leverage=%dx "
            "gross_pnl=%s fees=%s net_pnl=%s roi=%s",
            entry_price,
            exit_price,
            size,
            leverage,
            gross_pnl,
            total_fees,
            net_pnl,
            net_roi,
        )

        return result

    def adjust_tp_for_fees(
        self,
        entry_price: Decimal,
        raw_tp: Decimal,
        size: Decimal,
        leverage: int = 1,
        is_long: bool = True,
        entry_is_maker: bool = False,
        exit_is_maker: bool = False,
    ) -> Decimal:
        """Adjust a take-profit price so that the net profit after fees
        matches the profit that the raw TP would yield *without* fees.

        This ensures the trader actually realizes the target profit even
        after paying entry and exit fees.

        Parameters
        ----------
        entry_price : Decimal
            Entry price.
        raw_tp : Decimal
            The "naive" take-profit price (before fee adjustment).
        size : Decimal
            Position size in base currency.
        leverage : int
            Leverage used (for logging only; fees are on notional).
        is_long : bool
            True for a long position, False for short.
        entry_is_maker : bool
            Whether entry fill is maker.
        exit_is_maker : bool
            Whether exit fill is expected to be maker.

        Returns
        -------
        Decimal
            Adjusted take-profit price that nets the target profit after fees.
        """
        # Target gross profit (what the raw TP would give without any fees)
        if is_long:
            target_gross_profit = (raw_tp - entry_price) * size
        else:
            target_gross_profit = (entry_price - raw_tp) * size

        # Fees on entry side
        entry_notional = abs(entry_price * size)
        entry_fee = self.calculate_fees(entry_notional, is_maker=entry_is_maker)

        # We need to find exit_price such that:
        #   gross_pnl - entry_fee - exit_fee = target_gross_profit
        # where gross_pnl = (exit_price - entry_price) * size  (for long)
        #   and exit_fee = |exit_price * size| * exit_fee_rate
        #
        # For a long:
        #   (exit_price - entry_price) * size - entry_fee - exit_price * size * r = target_gross_profit
        #   exit_price * size - entry_price * size - entry_fee - exit_price * size * r = target_gross_profit
        #   exit_price * size * (1 - r) = target_gross_profit + entry_price * size + entry_fee
        #   exit_price = (target_gross_profit + entry_price * size + entry_fee) / (size * (1 - r))
        #
        # For a short:
        #   (entry_price - exit_price) * size - entry_fee - exit_price * size * r = target_gross_profit
        #   entry_price * size - exit_price * size - entry_fee - exit_price * size * r = target_gross_profit
        #   exit_price * size * (1 + r) = entry_price * size - entry_fee - target_gross_profit
        #   exit_price = (entry_price * size - entry_fee - target_gross_profit) / (size * (1 + r))

        exit_fee_rate = self._maker_fee if exit_is_maker else self._taker_fee

        if is_long:
            numerator = target_gross_profit + entry_price * size + entry_fee
            denominator = size * (Decimal("1") - exit_fee_rate)
            adjusted_tp = (numerator / denominator).quantize(
                Decimal("0.00000001"), rounding=ROUND_HALF_UP
            )
        else:
            numerator = entry_price * size - entry_fee - target_gross_profit
            denominator = size * (Decimal("1") + exit_fee_rate)
            adjusted_tp = (numerator / denominator).quantize(
                Decimal("0.00000001"), rounding=ROUND_HALF_UP
            )

        logger.info(
            "ADJUST_TP entry=%s raw_tp=%s adjusted_tp=%s is_long=%s "
            "size=%s leverage=%dx target_profit=%s entry_fee=%s",
            entry_price,
            raw_tp,
            adjusted_tp,
            is_long,
            size,
            leverage,
            target_gross_profit,
            entry_fee,
        )

        return adjusted_tp

    def minimum_profitable_move(
        self,
        entry_price: Decimal,
        size: Decimal,
        entry_is_maker: bool = False,
        exit_is_maker: bool = False,
    ) -> Decimal:
        """Calculate the minimum price movement needed to break even after fees.

        Parameters
        ----------
        entry_price : Decimal
            Entry price.
        size : Decimal
            Position size.
        entry_is_maker : bool
            Whether entry is a maker order.
        exit_is_maker : bool
            Whether exit is a maker order.

        Returns
        -------
        Decimal
            Minimum price change (absolute) needed to break even.
        """
        entry_notional = abs(entry_price * size)
        entry_fee = self.calculate_fees(entry_notional, is_maker=entry_is_maker)

        # For break-even: (exit_price - entry_price) * size = entry_fee + exit_fee
        # exit_fee = exit_price * size * r
        # Let delta = exit_price - entry_price
        # delta * size = entry_fee + (entry_price + delta) * size * r
        # delta * size = entry_fee + entry_price * size * r + delta * size * r
        # delta * size * (1 - r) = entry_fee + entry_price * size * r
        # delta = (entry_fee + entry_price * size * r) / (size * (1 - r))

        exit_fee_rate = self._maker_fee if exit_is_maker else self._taker_fee
        numerator = entry_fee + entry_price * size * exit_fee_rate
        denominator = size * (Decimal("1") - exit_fee_rate)
        min_move = (numerator / denominator).quantize(
            Decimal("0.00000001"), rounding=ROUND_HALF_UP
        )

        logger.debug(
            "MIN_PROFITABLE_MOVE entry=%s size=%s min_move=%s (%.4f%%)",
            entry_price,
            size,
            min_move,
            float(min_move / entry_price * 100),
        )
        return min_move
