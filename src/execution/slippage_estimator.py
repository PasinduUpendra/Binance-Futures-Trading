"""
Slippage estimator for Binance Futures trades.

Estimates expected slippage by walking through orderbook depth to simulate
fill prices for a given order size. All monetary values use Decimal.
"""

from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("claude_quant.execution.slippage_estimator")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class OrderBookLevel(BaseModel):
    """A single price level in the order book."""

    price: Decimal
    amount: Decimal


class SlippageEstimate(BaseModel):
    """Result of a slippage estimation."""

    symbol: str
    order_size: Decimal
    side: str = Field(description="'buy' or 'sell'")
    mid_price: Decimal
    expected_fill_price: Decimal
    slippage_pct: Decimal = Field(description="Slippage as a percentage (e.g. 0.05 = 0.05%)")
    slippage_amount: Decimal = Field(description="Absolute price impact")
    levels_consumed: int = Field(description="Number of orderbook levels consumed")
    fully_fillable: bool = Field(description="Whether the full order can be filled from visible book")


# ---------------------------------------------------------------------------
# SlippageEstimator
# ---------------------------------------------------------------------------


class SlippageEstimator:
    """Estimates slippage based on orderbook depth analysis.

    Walks through the orderbook to calculate the volume-weighted average
    price (VWAP) for a given order size, then computes the slippage
    relative to the mid-price.
    """

    def estimate_slippage(
        self,
        symbol: str,
        order_size: Decimal,
        orderbook: dict[str, Any],
        side: str = "buy",
    ) -> SlippageEstimate:
        """Estimate slippage for a market order of the given size.

        Parameters
        ----------
        symbol : str
            Trading pair, e.g. ``"BTC/USDT:USDT"``.
        order_size : Decimal
            Order size in base currency units.
        orderbook : dict[str, Any]
            Orderbook data. Expected to have ``"bids"`` and ``"asks"`` keys,
            each containing lists of ``[price, amount]`` pairs or
            ``{"price": ..., "amount": ...}`` dicts. Also accepts our
            ``OrderBookData`` Pydantic model's ``.model_dump()`` output.
        side : str
            ``"buy"`` (consumes asks) or ``"sell"`` (consumes bids).

        Returns
        -------
        SlippageEstimate
            Detailed slippage analysis.
        """
        bids_raw = orderbook.get("bids", [])
        asks_raw = orderbook.get("asks", [])

        bids = self._parse_levels(bids_raw)
        asks = self._parse_levels(asks_raw)

        if not bids or not asks:
            logger.warning(
                "SLIPPAGE_ESTIMATE symbol=%s — empty orderbook", symbol
            )
            return SlippageEstimate(
                symbol=symbol,
                order_size=order_size,
                side=side,
                mid_price=Decimal("0"),
                expected_fill_price=Decimal("0"),
                slippage_pct=Decimal("0"),
                slippage_amount=Decimal("0"),
                levels_consumed=0,
                fully_fillable=False,
            )

        # Mid price = (best bid + best ask) / 2
        best_bid = bids[0].price
        best_ask = asks[0].price
        mid_price = ((best_bid + best_ask) / Decimal("2")).quantize(
            Decimal("0.00000001"), rounding=ROUND_HALF_UP
        )

        # For a buy order, we consume the ask side (ascending prices)
        # For a sell order, we consume the bid side (descending prices)
        if side.lower() == "buy":
            levels = asks  # asks are sorted ascending (cheapest first)
        else:
            levels = bids  # bids are sorted descending (most expensive first)

        # Walk through levels to calculate VWAP
        remaining = order_size
        total_cost = Decimal("0")
        levels_consumed = 0
        fully_fillable = True

        for level in levels:
            if remaining <= Decimal("0"):
                break

            fill_at_level = min(remaining, level.amount)
            total_cost += fill_at_level * level.price
            remaining -= fill_at_level
            levels_consumed += 1

        if remaining > Decimal("0"):
            fully_fillable = False
            logger.warning(
                "SLIPPAGE_ESTIMATE symbol=%s — orderbook too shallow. "
                "Unfilled: %s of %s",
                symbol,
                remaining,
                order_size,
            )

        filled_amount = order_size - remaining
        if filled_amount > Decimal("0"):
            vwap = (total_cost / filled_amount).quantize(
                Decimal("0.00000001"), rounding=ROUND_HALF_UP
            )
        else:
            vwap = mid_price

        # Slippage = how far VWAP is from mid price
        if mid_price > Decimal("0"):
            if side.lower() == "buy":
                slippage_amount = vwap - mid_price
            else:
                slippage_amount = mid_price - vwap

            slippage_pct = (
                (abs(slippage_amount) / mid_price) * Decimal("100")
            ).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
        else:
            slippage_amount = Decimal("0")
            slippage_pct = Decimal("0")

        result = SlippageEstimate(
            symbol=symbol,
            order_size=order_size,
            side=side.lower(),
            mid_price=mid_price,
            expected_fill_price=vwap,
            slippage_pct=slippage_pct,
            slippage_amount=abs(slippage_amount),
            levels_consumed=levels_consumed,
            fully_fillable=fully_fillable,
        )

        logger.info(
            "SLIPPAGE_ESTIMATE symbol=%s side=%s size=%s mid=%s "
            "vwap=%s slippage=%s%% levels=%d fillable=%s",
            symbol,
            side,
            order_size,
            mid_price,
            vwap,
            slippage_pct,
            levels_consumed,
            fully_fillable,
        )

        return result

    def adjust_entry_for_slippage(
        self,
        price: Decimal,
        side: str,
        slippage_pct: Decimal,
    ) -> Decimal:
        """Adjust an entry price to account for expected slippage.

        Parameters
        ----------
        price : Decimal
            Original entry price.
        side : str
            ``"buy"`` or ``"sell"``.
        slippage_pct : Decimal
            Expected slippage percentage (e.g. ``Decimal("0.05")`` for 0.05%).

        Returns
        -------
        Decimal
            Price adjusted for expected slippage. For buys this is higher,
            for sells this is lower.
        """
        slippage_factor = slippage_pct / Decimal("100")

        if side.lower() == "buy":
            # Buying: expect to pay more due to slippage
            adjusted = price * (Decimal("1") + slippage_factor)
        else:
            # Selling: expect to receive less due to slippage
            adjusted = price * (Decimal("1") - slippage_factor)

        adjusted = adjusted.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)

        logger.debug(
            "ADJUST_ENTRY side=%s original=%s slippage=%s%% adjusted=%s",
            side,
            price,
            slippage_pct,
            adjusted,
        )
        return adjusted

    def estimate_market_impact(
        self,
        symbol: str,
        order_size: Decimal,
        orderbook: dict[str, Any],
        side: str = "buy",
    ) -> Decimal:
        """Convenience method: estimate slippage and return just the percentage.

        Parameters
        ----------
        symbol : str
            Trading pair.
        order_size : Decimal
            Order size in base currency.
        orderbook : dict[str, Any]
            Orderbook data.
        side : str
            ``"buy"`` or ``"sell"``.

        Returns
        -------
        Decimal
            Estimated slippage percentage.
        """
        estimate = self.estimate_slippage(symbol, order_size, orderbook, side)
        return estimate.slippage_pct

    # -- internal helpers ----------------------------------------------------

    @staticmethod
    def _parse_levels(raw_levels: list[Any]) -> list[OrderBookLevel]:
        """Parse orderbook levels from various formats.

        Supports:
        - List of [price, amount] tuples/lists
        - List of {"price": ..., "amount": ...} dicts
        - List of OrderBookLevel / OrderBookEntry Pydantic models
        """
        levels: list[OrderBookLevel] = []

        for item in raw_levels:
            if isinstance(item, dict):
                price = Decimal(str(item.get("price", 0)))
                amount = Decimal(str(item.get("amount", 0)))
            elif isinstance(item, (list, tuple)):
                price = Decimal(str(item[0]))
                amount = Decimal(str(item[1]))
            elif hasattr(item, "price") and hasattr(item, "amount"):
                # Pydantic model with price/amount attributes
                price = Decimal(str(item.price))
                amount = Decimal(str(item.amount))
            else:
                logger.warning("Unrecognized orderbook level format: %s", type(item))
                continue

            if price > 0 and amount > 0:
                levels.append(OrderBookLevel(price=price, amount=amount))

        return levels
