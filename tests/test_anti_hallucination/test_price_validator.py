"""
Tests for anti-hallucination price validator (Layer 2).

Validates that PriceValidator correctly detects out-of-range prices,
stale tickers, cross-validation failures, and API errors.
"""

import pytest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from src.anti_hallucination.price_validator import PriceValidator, ValidationResult


# ---------------------------------------------------------------------------
# Helper factory
# ---------------------------------------------------------------------------


def _make_ticker(
    last: float = 3450.0,
    high: float = 3500.0,
    low: float = 3400.0,
    bid: float = 3449.0,
    ask: float = 3451.0,
    age_seconds: int = 30,
) -> MagicMock:
    """Build a mock ticker object matching the exchange client contract."""
    ticker = MagicMock()
    ticker.last = last
    ticker.high = high
    ticker.low = low
    ticker.bid = bid
    ticker.ask = ask
    ticker.timestamp = datetime.now(tz=timezone.utc) - timedelta(seconds=age_seconds)
    return ticker


# ---------------------------------------------------------------------------
# TestValidatePrice
# ---------------------------------------------------------------------------


class TestValidatePrice:
    """Tests for PriceValidator.validate_price (async)."""

    @pytest.mark.asyncio
    async def test_valid_price_within_range_close_to_last_and_fresh(self):
        """A price inside the 24h range, within 1% of last, and fresh passes."""
        client = MagicMock()
        client.fetch_ticker = AsyncMock(return_value=_make_ticker())
        validator = PriceValidator(client)

        result = await validator.validate_price(
            symbol="ETH/USDT:USDT",
            price=Decimal("3450"),
            source="strategy",
        )

        assert result.valid is True
        assert result.issues == []
        assert result.price == Decimal("3450")
        assert result.source == "strategy"

    @pytest.mark.asyncio
    async def test_price_outside_24h_range_fails(self):
        """A price outside the 24h high/low range triggers a validation failure."""
        client = MagicMock()
        client.fetch_ticker = AsyncMock(
            return_value=_make_ticker(last=3450.0, high=3500.0, low=3400.0)
        )
        validator = PriceValidator(client)

        result = await validator.validate_price(
            symbol="ETH/USDT:USDT",
            price=Decimal("3600"),
            source="signal",
        )

        assert result.valid is False
        assert any("outside 24h range" in issue for issue in result.issues)

    @pytest.mark.asyncio
    async def test_price_more_than_1pct_from_last_fails(self):
        """A price deviating >1% from the exchange last triggers failure."""
        client = MagicMock()
        # last=3450, a price of 3420 is ~0.87% off — still within range.
        # Use a price of 3400: (3450-3400)/3450 = 1.449% deviation but
        # 3400 equals the low so it won't fail range check. Use 3410:
        # (3450-3410)/3450 = 1.16% > 1% and 3410 is between 3400-3500.
        client.fetch_ticker = AsyncMock(
            return_value=_make_ticker(last=3450.0, high=3500.0, low=3400.0)
        )
        validator = PriceValidator(client)

        result = await validator.validate_price(
            symbol="ETH/USDT:USDT",
            price=Decimal("3410"),
            source="signal",
        )

        assert result.valid is False
        assert any(">1%" in issue for issue in result.issues)

    @pytest.mark.asyncio
    async def test_stale_ticker_fails(self):
        """A ticker older than 600 seconds triggers a staleness failure."""
        client = MagicMock()
        client.fetch_ticker = AsyncMock(
            return_value=_make_ticker(age_seconds=700)
        )
        validator = PriceValidator(client)

        result = await validator.validate_price(
            symbol="ETH/USDT:USDT",
            price=Decimal("3450"),
            source="signal",
        )

        assert result.valid is False
        assert any("stale" in issue.lower() for issue in result.issues)

    @pytest.mark.asyncio
    async def test_api_error_returns_invalid(self):
        """An API exception during fetch_ticker yields invalid with API error."""
        client = MagicMock()
        client.fetch_ticker = AsyncMock(side_effect=ConnectionError("timeout"))
        validator = PriceValidator(client)

        result = await validator.validate_price(
            symbol="ETH/USDT:USDT",
            price=Decimal("3450"),
            source="signal",
        )

        assert result.valid is False
        assert any("API error" in issue for issue in result.issues)


# ---------------------------------------------------------------------------
# TestCrossValidate
# ---------------------------------------------------------------------------


class TestCrossValidate:
    """Tests for PriceValidator.cross_validate (static)."""

    def test_prices_within_tolerance_pass(self):
        """Two prices within 0.1% tolerance should agree."""
        assert PriceValidator.cross_validate(
            Decimal("3450"), Decimal("3451")
        ) is True

    def test_prices_far_apart_fail(self):
        """Two prices far apart (100 vs 110) should disagree."""
        assert PriceValidator.cross_validate(
            Decimal("100"), Decimal("110")
        ) is False

    def test_both_zero_pass(self):
        """Two zero prices are considered matching."""
        assert PriceValidator.cross_validate(
            Decimal("0"), Decimal("0")
        ) is True

    def test_one_zero_fails(self):
        """One zero price means sources disagree."""
        assert PriceValidator.cross_validate(
            Decimal("0"), Decimal("100")
        ) is False


# ---------------------------------------------------------------------------
# TestStaleness
# ---------------------------------------------------------------------------


class TestStaleness:
    """Tests for PriceValidator.check_staleness (static)."""

    def test_fresh_timestamp_passes(self):
        """A timestamp 30 seconds old is fresh."""
        ts = datetime.now(tz=timezone.utc) - timedelta(seconds=30)
        assert PriceValidator.check_staleness(ts) is True

    def test_stale_timestamp_fails(self):
        """A timestamp 700 seconds old is stale."""
        ts = datetime.now(tz=timezone.utc) - timedelta(seconds=700)
        assert PriceValidator.check_staleness(ts) is False

    def test_future_timestamp_fails(self):
        """A timestamp in the future is rejected (age < 0)."""
        ts = datetime.now(tz=timezone.utc) + timedelta(seconds=60)
        assert PriceValidator.check_staleness(ts) is False

    def test_naive_datetime_treated_as_utc(self):
        """A naive datetime (no tzinfo) is treated as UTC and passes if fresh."""
        ts = datetime.now(tz=timezone.utc).replace(tzinfo=None) - timedelta(seconds=30)
        assert PriceValidator.check_staleness(ts) is True
