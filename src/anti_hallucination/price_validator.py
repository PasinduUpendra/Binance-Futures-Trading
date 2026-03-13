"""
Price validation layer for anti-hallucination.

Cross-references prices against live exchange data to ensure the system
never trades on fabricated, stale, or impossible price values.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("claude_quant.anti_hallucination.price_validator")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ValidationResult(BaseModel):
    """Outcome of a single price validation check."""

    valid: bool
    price: Decimal
    source: str
    timestamp: datetime
    issues: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# PriceValidator
# ---------------------------------------------------------------------------


class PriceValidator:
    """Validates that prices originate from the exchange and are plausible.

    Parameters
    ----------
    market_data_client : Any
        An instance of ``MarketDataClient`` (or duck-typed equivalent) that
        exposes ``fetch_ticker(symbol)`` returning an object with ``last``,
        ``high``, ``low``, ``bid``, ``ask``, and ``timestamp`` attributes.
    """

    def __init__(self, market_data_client: Any) -> None:
        self._client = market_data_client

    # -- public API ----------------------------------------------------------

    async def validate_price(
        self,
        symbol: str,
        price: Decimal,
        source: str,
    ) -> ValidationResult:
        """Cross-reference *price* against the exchange ticker.

        Checks performed:
        1. Price is within the 24-h high/low range reported by the exchange.
        2. Price is within 1% of the exchange's last traded price (guards
           against using yesterday's close as today's price).
        3. The exchange ticker itself is not stale (< 10 min old).

        Returns a ``ValidationResult`` that is ``valid=True`` only when all
        checks pass.
        """
        issues: list[str] = []
        now = datetime.now(tz=timezone.utc)

        try:
            ticker = await self._client.fetch_ticker(symbol)
        except Exception as exc:
            logger.error("Failed to fetch ticker for %s: %s", symbol, exc)
            return ValidationResult(
                valid=False,
                price=price,
                source=source,
                timestamp=now,
                issues=[f"Exchange API error: {exc}"],
            )

        exchange_last: Decimal = Decimal(str(ticker.last))
        exchange_high: Decimal = Decimal(str(ticker.high))
        exchange_low: Decimal = Decimal(str(ticker.low))
        exchange_ts: datetime = ticker.timestamp

        # 1. Within daily high/low
        if price < exchange_low or price > exchange_high:
            issues.append(
                f"Price {price} outside 24h range [{exchange_low}, {exchange_high}]"
            )

        # 2. Close to last traded price (within 1 %)
        if exchange_last != Decimal("0"):
            deviation = abs(price - exchange_last) / exchange_last
            if deviation > Decimal("0.01"):
                issues.append(
                    f"Price {price} deviates {deviation:.4%} from exchange last "
                    f"{exchange_last} (>1%)"
                )

        # 3. Exchange data freshness
        if not self.check_staleness(exchange_ts):
            age_seconds = (now - exchange_ts).total_seconds()
            issues.append(
                f"Exchange ticker stale: {age_seconds:.0f}s old (max 600s)"
            )

        valid = len(issues) == 0
        if not valid:
            logger.warning(
                "Price validation FAILED for %s from %s: %s",
                symbol,
                source,
                "; ".join(issues),
            )
        else:
            logger.debug(
                "Price validated for %s: %s from %s", symbol, price, source
            )

        return ValidationResult(
            valid=valid,
            price=price,
            source=source,
            timestamp=now,
            issues=issues,
        )

    @staticmethod
    def cross_validate(
        price1: Decimal,
        price2: Decimal,
        tolerance: Decimal = Decimal("0.001"),
    ) -> bool:
        """Return *True* if two price sources agree within *tolerance* (0.1 % default).

        Parameters
        ----------
        price1 : Decimal
            Price from the first source.
        price2 : Decimal
            Price from the second source.
        tolerance : Decimal
            Maximum relative difference (e.g. ``0.001`` = 0.1 %).
        """
        if price1 == Decimal("0") and price2 == Decimal("0"):
            return True
        if price1 == Decimal("0") or price2 == Decimal("0"):
            return False

        mid = (price1 + price2) / Decimal("2")
        relative_diff = abs(price1 - price2) / mid

        within = relative_diff <= tolerance
        if not within:
            logger.warning(
                "Cross-validation FAILED: %s vs %s (diff=%s, tol=%s)",
                price1,
                price2,
                relative_diff,
                tolerance,
            )
        return within

    @staticmethod
    def check_staleness(
        timestamp: datetime,
        max_age_seconds: int = 600,
    ) -> bool:
        """Return *True* if *timestamp* is within *max_age_seconds* of now (UTC).

        Parameters
        ----------
        timestamp : datetime
            The timestamp to check (must be timezone-aware or assumed UTC).
        max_age_seconds : int
            Maximum acceptable age in seconds.  Default 600 (10 minutes).
        """
        now = datetime.now(tz=timezone.utc)

        # Ensure timezone-aware comparison
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)

        age = (now - timestamp).total_seconds()
        fresh = 0 <= age <= max_age_seconds

        if not fresh:
            logger.warning(
                "Staleness check FAILED: timestamp=%s, age=%.1fs, max=%ds",
                timestamp.isoformat(),
                age,
                max_age_seconds,
            )
        return fresh
