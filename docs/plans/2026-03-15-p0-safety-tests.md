# P0 Safety-Critical Unit Tests Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Write comprehensive unit tests for the 3 P0 untested modules: `order_manager.py` (15+ tests), `price_validator.py` (8+ tests), `signal_validator.py` (8+ tests).

**Architecture:** Each test file follows existing project conventions — class-based grouping, `pytest` + `AsyncMock`/`MagicMock` for exchange mocking, `Decimal` for all monetary assertions. No real exchange calls.

**Tech Stack:** pytest, unittest.mock (AsyncMock, MagicMock, patch), decimal.Decimal, datetime

**Test runner:** `.venv/bin/python -m pytest tests/ -v`

---

## Conventions (Match Existing Tests)

- File: `tests/test_<module>/test_<name>.py`
- Classes: `class TestComponentName:` with `# ─── Section ───` separators
- Fixtures: use `conftest.py` `mock_exchange` where applicable
- All monetary values: `Decimal("...")`
- Async tests: `@pytest.mark.asyncio` + `async def test_...(self):`
- No real API calls — everything mocked

---

### Task 1: Order Manager — Helpers & Parsing (5 tests)

**Files:**
- Create: `tests/test_execution/test_order_manager.py`

**Step 1: Write the failing tests**

```python
"""Tests for order_manager.py — the module that moves real money."""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.execution.order_manager import (
    OrderManager,
    OrderResult,
    OrderSide,
    OrderState,
    OrderStatus,
    _generate_client_order_id,
    _parse_order_state,
    _to_decimal,
    _utc_from_ms,
)


class TestHelpers:
    """Unit tests for module-level helper functions."""

    # ─── _to_decimal ───

    def test_to_decimal_from_float(self):
        assert _to_decimal(43100.5) == Decimal("43100.5")

    def test_to_decimal_none_returns_zero(self):
        assert _to_decimal(None) == Decimal("0")

    # ─── _utc_from_ms ───

    def test_utc_from_ms_valid(self):
        ts = _utc_from_ms(1704067200000)
        assert ts.tzinfo == timezone.utc
        assert ts.year == 2024

    def test_utc_from_ms_none_returns_now(self):
        before = datetime.now(tz=timezone.utc)
        result = _utc_from_ms(None)
        after = datetime.now(tz=timezone.utc)
        assert before <= result <= after

    # ─── _parse_order_state ───

    def test_parse_order_state_closed(self):
        assert _parse_order_state("closed") == OrderState.CLOSED

    def test_parse_order_state_cancelled_british(self):
        assert _parse_order_state("cancelled") == OrderState.CANCELED

    def test_parse_order_state_none(self):
        assert _parse_order_state(None) == OrderState.UNKNOWN

    def test_parse_order_state_unknown_string(self):
        assert _parse_order_state("partial") == OrderState.UNKNOWN

    # ─── _generate_client_order_id ───

    def test_client_order_id_prefix(self):
        oid = _generate_client_order_id()
        assert oid.startswith("cq_")
        assert len(oid) == 19  # "cq_" + 16 hex chars

    def test_client_order_id_unique(self):
        ids = {_generate_client_order_id() for _ in range(100)}
        assert len(ids) == 100
```

**Step 2: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_execution/test_order_manager.py::TestHelpers -v`
Expected: 9 PASSED

**Step 3: Commit**

```bash
git add tests/test_execution/test_order_manager.py
git commit -m "test: add order_manager helper function tests (9 tests)"
```

---

### Task 2: Order Manager — Parsing Methods (4 tests)

**Files:**
- Modify: `tests/test_execution/test_order_manager.py`

**Step 1: Add parsing tests to the file**

```python
class TestParsing:
    """Tests for _parse_order_result and _parse_order_status."""

    def _make_raw_order(self, **overrides):
        """Factory for raw ccxt order responses."""
        base = {
            "id": "12345",
            "symbol": "ETH/USDT:USDT",
            "side": "buy",
            "type": "market",
            "amount": 0.05,
            "price": None,
            "stopPrice": None,
            "status": "closed",
            "filled": 0.05,
            "remaining": 0.0,
            "average": 3450.0,
            "cost": 172.50,
            "fee": {"cost": 0.0863, "currency": "USDT"},
            "timestamp": 1704067200000,
            "lastTradeTimestamp": 1704067200100,
        }
        base.update(overrides)
        return base

    def _make_om(self):
        """Create an OrderManager without connecting."""
        return OrderManager(api_key="k", api_secret="s", testnet=True)

    def test_parse_order_result_fields(self):
        om = self._make_om()
        raw = self._make_raw_order()
        result = om._parse_order_result(raw, "cq_abc123", verified=True)

        assert isinstance(result, OrderResult)
        assert result.order_id == "12345"
        assert result.client_order_id == "cq_abc123"
        assert result.symbol == "ETH/USDT:USDT"
        assert result.side == OrderSide.BUY
        assert result.amount == Decimal("0.05")
        assert result.average_fill_price == Decimal("3450.0")
        assert result.fee == Decimal("0.0863")
        assert result.fee_currency == "USDT"
        assert result.verified is True

    def test_parse_order_result_no_fee(self):
        om = self._make_om()
        raw = self._make_raw_order(fee=None)
        result = om._parse_order_result(raw, "cq_x")
        assert result.fee == Decimal("0")
        assert result.fee_currency is None

    def test_parse_order_status_fields(self):
        om = self._make_om()
        raw = self._make_raw_order()
        status = om._parse_order_status(raw)

        assert isinstance(status, OrderStatus)
        assert status.order_id == "12345"
        assert status.status == OrderState.CLOSED
        assert status.filled == Decimal("0.05")
        assert status.remaining == Decimal("0.0")
        assert status.last_trade_timestamp is not None

    def test_parse_order_status_no_stop_price(self):
        om = self._make_om()
        raw = self._make_raw_order(stopPrice=None)
        status = om._parse_order_status(raw)
        assert status.stop_price is None
```

**Step 2: Run tests**

Run: `.venv/bin/python -m pytest tests/test_execution/test_order_manager.py::TestParsing -v`
Expected: 4 PASSED

**Step 3: Commit**

```bash
git add tests/test_execution/test_order_manager.py
git commit -m "test: add order_manager parsing method tests (4 tests)"
```

---

### Task 3: Order Manager — Idempotent Submission (8 tests)

**Files:**
- Modify: `tests/test_execution/test_order_manager.py`

**Step 1: Add idempotent tests**

```python
import ccxt.async_support as ccxt_async


def _make_connected_om(mock_exchange) -> OrderManager:
    """Create an OrderManager with a pre-injected mock exchange."""
    om = OrderManager(api_key="k", api_secret="s", testnet=True, verify_orders=False)
    om._exchange = mock_exchange
    return om


def _raw_order_response(**overrides):
    """Minimal raw ccxt order response."""
    base = {
        "id": "99999",
        "symbol": "ETH/USDT:USDT",
        "side": "buy",
        "type": "market",
        "amount": 0.05,
        "price": None,
        "stopPrice": None,
        "status": "closed",
        "filled": 0.05,
        "remaining": 0.0,
        "average": 3450.0,
        "cost": 172.50,
        "fee": {"cost": 0.086, "currency": "USDT"},
        "timestamp": 1704067200000,
    }
    base.update(overrides)
    return base


class TestIdempotentSubmission:
    """Tests for _submit_order_idempotent — the core safety mechanism."""

    # ─── Happy path ───

    @pytest.mark.asyncio
    async def test_success_first_attempt(self):
        """Order placed on first try, returned with correct fields."""
        exchange = MagicMock()
        exchange.create_order = AsyncMock(return_value=_raw_order_response())
        om = _make_connected_om(exchange)

        result = await om._submit_order_idempotent(
            symbol="ETH/USDT:USDT", order_type="market",
            side="buy", amount=0.05,
        )

        assert result is not None
        assert result.order_id == "99999"
        assert result.amount == Decimal("0.05")
        exchange.create_order.assert_called_once()

    # ─── Timeout → query → found existing ───

    @pytest.mark.asyncio
    async def test_timeout_then_found_existing(self):
        """Timeout on create, but query finds the order already placed."""
        exchange = MagicMock()
        exchange.create_order = AsyncMock(
            side_effect=ccxt_async.RequestTimeout("timeout")
        )
        # _query_by_client_order_id will use these:
        exchange.market = MagicMock(return_value={"id": "ETHUSDT"})
        exchange.fapiPrivateGetOrder = AsyncMock(return_value={
            "orderId": "99999", "symbol": "ETHUSDT", "side": "BUY",
            "type": "MARKET", "origQty": "0.05", "executedQty": "0.05",
            "price": "0", "stopPrice": "0", "status": "FILLED",
            "time": 1704067200000,
        })
        exchange.parse_order = MagicMock(return_value=_raw_order_response())
        om = _make_connected_om(exchange)

        result = await om._submit_order_idempotent(
            symbol="ETH/USDT:USDT", order_type="market",
            side="buy", amount=0.05,
        )

        assert result is not None
        assert result.verified is True  # Queried from exchange directly
        # Should NOT retry after finding existing
        assert exchange.create_order.call_count == 1

    # ─── Timeout → query → NOT found → retry with new ID ───

    @pytest.mark.asyncio
    async def test_timeout_then_not_found_retries(self):
        """Timeout on create, query returns not found, retries with new ID."""
        exchange = MagicMock()
        # First attempt: timeout. Second attempt: success.
        exchange.create_order = AsyncMock(
            side_effect=[
                ccxt_async.NetworkError("network"),
                _raw_order_response(),
            ]
        )
        # Query returns not found (exception)
        exchange.market = MagicMock(return_value={"id": "ETHUSDT"})
        exchange.fapiPrivateGetOrder = AsyncMock(
            side_effect=Exception("order not found")
        )
        om = _make_connected_om(exchange)

        result = await om._submit_order_idempotent(
            symbol="ETH/USDT:USDT", order_type="market",
            side="buy", amount=0.05,
        )

        assert result is not None
        assert exchange.create_order.call_count == 2
        # Verify different client_order_ids were used
        call1_params = exchange.create_order.call_args_list[0]
        call2_params = exchange.create_order.call_args_list[1]
        coid1 = call1_params.kwargs.get("params", {}).get("newClientOrderId")
        coid2 = call2_params.kwargs.get("params", {}).get("newClientOrderId")
        assert coid1 != coid2

    # ─── InsufficientFunds → returns None, no retry ───

    @pytest.mark.asyncio
    async def test_insufficient_funds_returns_none(self):
        """InsufficientFunds should return None immediately, no retry."""
        exchange = MagicMock()
        exchange.create_order = AsyncMock(
            side_effect=ccxt_async.InsufficientFunds("not enough")
        )
        om = _make_connected_om(exchange)

        result = await om._submit_order_idempotent(
            symbol="ETH/USDT:USDT", order_type="market",
            side="buy", amount=0.05,
        )

        assert result is None
        assert exchange.create_order.call_count == 1  # No retry

    # ─── InvalidOrder → returns None, no retry ───

    @pytest.mark.asyncio
    async def test_invalid_order_returns_none(self):
        """InvalidOrder should return None immediately, no retry."""
        exchange = MagicMock()
        exchange.create_order = AsyncMock(
            side_effect=ccxt_async.InvalidOrder("bad order")
        )
        om = _make_connected_om(exchange)

        result = await om._submit_order_idempotent(
            symbol="ETH/USDT:USDT", order_type="market",
            side="buy", amount=0.05,
        )

        assert result is None
        assert exchange.create_order.call_count == 1

    # ─── ExchangeError → raises immediately ───

    @pytest.mark.asyncio
    async def test_exchange_error_raises_immediately(self):
        """ExchangeError should raise immediately, not retry."""
        exchange = MagicMock()
        exchange.create_order = AsyncMock(
            side_effect=ccxt_async.ExchangeError("server error")
        )
        om = _make_connected_om(exchange)

        with pytest.raises(ccxt_async.ExchangeError):
            await om._submit_order_idempotent(
                symbol="ETH/USDT:USDT", order_type="market",
                side="buy", amount=0.05,
            )

        assert exchange.create_order.call_count == 1

    # ─── DDoS → retries (order was NOT placed) ───

    @pytest.mark.asyncio
    async def test_ddos_retries_without_query(self):
        """DDoS means order definitely NOT placed — retry without querying."""
        exchange = MagicMock()
        exchange.create_order = AsyncMock(
            side_effect=[
                ccxt_async.DDoSProtection("rate limited"),
                _raw_order_response(),
            ]
        )
        om = _make_connected_om(exchange)

        result = await om._submit_order_idempotent(
            symbol="ETH/USDT:USDT", order_type="market",
            side="buy", amount=0.05,
        )

        assert result is not None
        assert exchange.create_order.call_count == 2
        # Should NOT have called fapiPrivateGetOrder (no query needed)
        assert not hasattr(exchange, 'fapiPrivateGetOrder') or \
            not exchange.fapiPrivateGetOrder.called

    # ─── All retries exhausted → RuntimeError ───

    @pytest.mark.asyncio
    async def test_all_retries_exhausted_raises(self):
        """If all 3 attempts fail with transient errors, raise RuntimeError."""
        exchange = MagicMock()
        exchange.create_order = AsyncMock(
            side_effect=ccxt_async.DDoSProtection("rate limited")
        )
        om = _make_connected_om(exchange)

        with pytest.raises(RuntimeError, match="failed after 3"):
            await om._submit_order_idempotent(
                symbol="ETH/USDT:USDT", order_type="market",
                side="buy", amount=0.05,
            )

        assert exchange.create_order.call_count == 3
```

**Step 2: Run tests**

Run: `.venv/bin/python -m pytest tests/test_execution/test_order_manager.py::TestIdempotentSubmission -v`
Expected: 8 PASSED

**Step 3: Commit**

```bash
git add tests/test_execution/test_order_manager.py
git commit -m "test: add idempotent order submission tests (8 tests)"
```

---

### Task 4: Order Manager — Public Order Methods (6 tests)

**Files:**
- Modify: `tests/test_execution/test_order_manager.py`

**Step 1: Add public API tests**

```python
class TestPublicOrderMethods:
    """Tests for place_market_order, place_stop_loss, place_take_profit, set_leverage."""

    @pytest.mark.asyncio
    async def test_place_market_order_delegates(self):
        """place_market_order delegates to _submit_order_idempotent."""
        exchange = MagicMock()
        exchange.create_order = AsyncMock(return_value=_raw_order_response())
        om = _make_connected_om(exchange)

        result = await om.place_market_order("ETH/USDT:USDT", "buy", Decimal("0.05"))

        assert result is not None
        assert result.order_id == "99999"
        # Verify market order type
        call_kwargs = exchange.create_order.call_args
        assert call_kwargs.kwargs["type"] == "market"

    @pytest.mark.asyncio
    async def test_place_stop_loss_passes_stop_price(self):
        """place_stop_loss must pass stopPrice in params."""
        exchange = MagicMock()
        exchange.create_order = AsyncMock(return_value=_raw_order_response())
        om = _make_connected_om(exchange)

        await om.place_stop_loss("ETH/USDT:USDT", "sell", Decimal("0.05"), Decimal("3400"))

        call_kwargs = exchange.create_order.call_args
        assert call_kwargs.kwargs["type"] == "STOP_MARKET"
        assert call_kwargs.kwargs["params"]["stopPrice"] == 3400.0

    @pytest.mark.asyncio
    async def test_place_take_profit_passes_stop_price(self):
        """place_take_profit must pass stopPrice in params."""
        exchange = MagicMock()
        exchange.create_order = AsyncMock(return_value=_raw_order_response())
        om = _make_connected_om(exchange)

        await om.place_take_profit("ETH/USDT:USDT", "sell", Decimal("0.05"), Decimal("3600"))

        call_kwargs = exchange.create_order.call_args
        assert call_kwargs.kwargs["type"] == "TAKE_PROFIT_MARKET"
        assert call_kwargs.kwargs["params"]["stopPrice"] == 3600.0

    @pytest.mark.asyncio
    async def test_set_leverage_valid(self):
        """set_leverage within 1-10 should succeed."""
        exchange = MagicMock()
        exchange.set_leverage = AsyncMock()
        om = _make_connected_om(exchange)

        await om.set_leverage("ETH/USDT:USDT", 5)
        exchange.set_leverage.assert_called_once_with(5, "ETH/USDT:USDT")

    @pytest.mark.asyncio
    async def test_set_leverage_too_high(self):
        """Leverage > 10 must raise ValueError."""
        exchange = MagicMock()
        om = _make_connected_om(exchange)

        with pytest.raises(ValueError, match="1-10"):
            await om.set_leverage("ETH/USDT:USDT", 15)

    @pytest.mark.asyncio
    async def test_set_leverage_too_low(self):
        """Leverage < 1 must raise ValueError."""
        exchange = MagicMock()
        om = _make_connected_om(exchange)

        with pytest.raises(ValueError, match="1-10"):
            await om.set_leverage("ETH/USDT:USDT", 0)
```

**Step 2: Run tests**

Run: `.venv/bin/python -m pytest tests/test_execution/test_order_manager.py::TestPublicOrderMethods -v`
Expected: 6 PASSED

**Step 3: Commit**

```bash
git add tests/test_execution/test_order_manager.py
git commit -m "test: add public order method tests (6 tests)"
```

---

### Task 5: Order Manager — Cancel & Query (4 tests)

**Files:**
- Modify: `tests/test_execution/test_order_manager.py`

**Step 1: Add cancel/query tests**

```python
class TestCancelAndQuery:
    """Tests for cancel_order, cancel_open_orders, get_order_status, get_open_orders."""

    @pytest.mark.asyncio
    async def test_cancel_order_verifies(self):
        """cancel_order with verify_orders=True calls _verify_order_exists."""
        exchange = MagicMock()
        exchange.cancel_order = AsyncMock()
        exchange.fetch_order = AsyncMock(return_value=_raw_order_response(status="canceled"))
        om = OrderManager(api_key="k", api_secret="s", testnet=True, verify_orders=True)
        om._exchange = exchange

        await om.cancel_order("ETH/USDT:USDT", "12345")

        exchange.cancel_order.assert_called_once_with("12345", "ETH/USDT:USDT")
        exchange.fetch_order.assert_called_once()  # Verification GET

    @pytest.mark.asyncio
    async def test_cancel_open_orders_counts(self):
        """cancel_open_orders returns correct count."""
        exchange = MagicMock()
        exchange.fetch_open_orders = AsyncMock(return_value=[
            _raw_order_response(id="111", status="open"),
            _raw_order_response(id="222", status="open"),
        ])
        exchange.cancel_order = AsyncMock()
        exchange.fetch_order = AsyncMock(return_value=_raw_order_response(status="canceled"))
        om = OrderManager(api_key="k", api_secret="s", testnet=True, verify_orders=True)
        om._exchange = exchange

        count = await om.cancel_open_orders("ETH/USDT:USDT")

        assert count == 2

    @pytest.mark.asyncio
    async def test_get_order_status(self):
        """get_order_status returns parsed OrderStatus."""
        exchange = MagicMock()
        exchange.fetch_order = AsyncMock(return_value=_raw_order_response(status="open", filled=0.0))
        om = _make_connected_om(exchange)

        status = await om.get_order_status("ETH/USDT:USDT", "12345")

        assert isinstance(status, OrderStatus)
        assert status.status == OrderState.OPEN
        assert status.filled == Decimal("0.0")

    @pytest.mark.asyncio
    async def test_require_exchange_not_connected(self):
        """_require_exchange raises RuntimeError if not connected."""
        om = OrderManager(api_key="k", api_secret="s", testnet=True)

        with pytest.raises(RuntimeError, match="not connected"):
            om._require_exchange()
```

**Step 2: Run tests**

Run: `.venv/bin/python -m pytest tests/test_execution/test_order_manager.py::TestCancelAndQuery -v`
Expected: 4 PASSED

**Step 3: Commit**

```bash
git add tests/test_execution/test_order_manager.py
git commit -m "test: add cancel/query order tests (4 tests)"
```

---

### Task 6: Price Validator (8 tests)

**Files:**
- Create: `tests/test_anti_hallucination/test_price_validator.py`

**Step 1: Write the test file**

```python
"""Tests for price_validator.py — anti-hallucination Layer 2."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.anti_hallucination.price_validator import PriceValidator, ValidationResult


def _make_ticker(last=3450.0, high=3500.0, low=3400.0, bid=3449.0, ask=3451.0, age_seconds=30):
    """Factory for mock ticker responses."""
    ticker = MagicMock()
    ticker.last = last
    ticker.high = high
    ticker.low = low
    ticker.bid = bid
    ticker.ask = ask
    ticker.timestamp = datetime.now(tz=timezone.utc) - timedelta(seconds=age_seconds)
    return ticker


class TestValidatePrice:
    """Tests for the main validate_price method."""

    @pytest.mark.asyncio
    async def test_valid_price_passes(self):
        """Price within range, close to last, fresh ticker → valid."""
        client = MagicMock()
        client.fetch_ticker = AsyncMock(return_value=_make_ticker())
        pv = PriceValidator(client)

        result = await pv.validate_price("ETH/USDT:USDT", Decimal("3450"), "test")

        assert result.valid is True
        assert len(result.issues) == 0

    @pytest.mark.asyncio
    async def test_price_outside_daily_range_fails(self):
        """Price outside 24h high/low → invalid."""
        client = MagicMock()
        client.fetch_ticker = AsyncMock(return_value=_make_ticker(high=3500, low=3400))
        pv = PriceValidator(client)

        result = await pv.validate_price("ETH/USDT:USDT", Decimal("3600"), "test")

        assert result.valid is False
        assert any("outside 24h range" in i for i in result.issues)

    @pytest.mark.asyncio
    async def test_price_deviates_more_than_1pct(self):
        """Price >1% from exchange last → invalid."""
        client = MagicMock()
        client.fetch_ticker = AsyncMock(return_value=_make_ticker(last=3450))
        pv = PriceValidator(client)

        # 3450 * 1.02 = 3519 — 2% deviation
        result = await pv.validate_price("ETH/USDT:USDT", Decimal("3519"), "test")

        assert result.valid is False
        assert any(">1%" in i for i in result.issues)

    @pytest.mark.asyncio
    async def test_stale_ticker_fails(self):
        """Ticker older than 10 minutes → invalid."""
        client = MagicMock()
        client.fetch_ticker = AsyncMock(return_value=_make_ticker(age_seconds=700))
        pv = PriceValidator(client)

        result = await pv.validate_price("ETH/USDT:USDT", Decimal("3450"), "test")

        assert result.valid is False
        assert any("stale" in i.lower() for i in result.issues)

    @pytest.mark.asyncio
    async def test_api_error_returns_invalid(self):
        """If fetch_ticker raises, return invalid with error message."""
        client = MagicMock()
        client.fetch_ticker = AsyncMock(side_effect=ConnectionError("timeout"))
        pv = PriceValidator(client)

        result = await pv.validate_price("ETH/USDT:USDT", Decimal("3450"), "test")

        assert result.valid is False
        assert any("API error" in i for i in result.issues)


class TestCrossValidate:
    """Tests for the cross_validate static method."""

    def test_prices_within_tolerance(self):
        assert PriceValidator.cross_validate(Decimal("100"), Decimal("100.05")) is True

    def test_prices_outside_tolerance(self):
        assert PriceValidator.cross_validate(Decimal("100"), Decimal("110")) is False

    def test_both_zero(self):
        assert PriceValidator.cross_validate(Decimal("0"), Decimal("0")) is True

    def test_one_zero(self):
        assert PriceValidator.cross_validate(Decimal("100"), Decimal("0")) is False


class TestStaleness:
    """Tests for check_staleness static method."""

    def test_fresh_timestamp(self):
        ts = datetime.now(tz=timezone.utc) - timedelta(seconds=30)
        assert PriceValidator.check_staleness(ts) is True

    def test_stale_timestamp(self):
        ts = datetime.now(tz=timezone.utc) - timedelta(seconds=700)
        assert PriceValidator.check_staleness(ts) is False

    def test_future_timestamp_fails(self):
        ts = datetime.now(tz=timezone.utc) + timedelta(seconds=60)
        assert PriceValidator.check_staleness(ts) is False

    def test_naive_datetime_treated_as_utc(self):
        ts = datetime.utcnow() - timedelta(seconds=30)  # naive
        assert PriceValidator.check_staleness(ts) is True
```

**Step 2: Run tests**

Run: `.venv/bin/python -m pytest tests/test_anti_hallucination/test_price_validator.py -v`
Expected: 12 PASSED

**Step 3: Commit**

```bash
git add tests/test_anti_hallucination/test_price_validator.py
git commit -m "test: add price_validator anti-hallucination tests (12 tests)"
```

---

### Task 7: Signal Validator (10 tests)

**Files:**
- Create: `tests/test_anti_hallucination/test_signal_validator.py`

**Step 1: Write the test file**

```python
"""Tests for signal_validator.py — anti-hallucination Layer 3."""

from decimal import Decimal

import pytest

from src.anti_hallucination.signal_validator import (
    SignalValidation,
    SignalValidator,
    TradingSignal,
)


def _make_signal(**overrides):
    """Factory for TradingSignal with sane defaults (long ETH)."""
    defaults = {
        "signal_id": "sig_001",
        "symbol": "ETH/USDT:USDT",
        "direction": "long",
        "strategy": "SupertrendTrend",
        "entry_price": Decimal("3450"),
        "stop_loss": Decimal("3300"),
        "take_profit": Decimal("3750"),
        "leverage": 5,
        "confidence": 75.0,
        "indicators": {"RSI": 65.0, "ADX": 30.0},
    }
    defaults.update(overrides)
    return TradingSignal(**defaults)


def _make_raw_data(**overrides):
    """Factory for raw_data dict with matching defaults."""
    defaults = {
        "bid": Decimal("3449"),
        "ask": Decimal("3451"),
        "indicators": {"RSI": 65.0, "ADX": 30.0},
        "candles": [{"close": 3450.0}],
    }
    defaults.update(overrides)
    return defaults


class TestValidateSignal:
    """Tests for the main validate_signal method."""

    def test_valid_signal_passes(self):
        sv = SignalValidator()
        result = sv.validate_signal(_make_signal(), _make_raw_data())
        assert result.valid is True
        assert len(result.issues) == 0

    def test_no_indicators_fails(self):
        sv = SignalValidator()
        signal = _make_signal(indicators={})
        result = sv.validate_signal(signal, _make_raw_data())
        assert result.valid is False
        assert any("no indicator" in i.lower() for i in result.issues)

    def test_vague_indicator_fails(self):
        sv = SignalValidator()
        signal = _make_signal(indicators={"trend": "bullish"})
        result = sv.validate_signal(signal, _make_raw_data(indicators={"trend": "bullish"}))
        assert result.valid is False
        assert any("vague" in i.lower() for i in result.issues)


class TestIndicatorValues:
    """Tests for _check_indicator_values."""

    def test_within_tolerance_passes(self):
        sv = SignalValidator()
        issues, checks = sv._check_indicator_values(
            {"RSI": 65.0}, {"RSI": 65.1}
        )
        assert len(issues) == 0
        assert checks["RSI"] is True

    def test_outside_tolerance_fails(self):
        sv = SignalValidator(indicator_tolerance=Decimal("0.005"))
        issues, checks = sv._check_indicator_values(
            {"RSI": 65.0}, {"RSI": 70.0}
        )
        assert len(issues) == 1
        assert checks["RSI"] is False

    def test_indicator_not_in_raw_data(self):
        sv = SignalValidator()
        issues, checks = sv._check_indicator_values(
            {"MACD": 0.5}, {}
        )
        assert any("not found in raw data" in i for i in issues)
        assert checks["MACD"] is False


class TestRiskReward:
    """Tests for _check_risk_reward."""

    def test_long_valid_rr(self):
        """Long: SL < entry < TP with R/R >= 2.0."""
        sv = SignalValidator()
        signal = _make_signal(entry_price=Decimal("100"), stop_loss=Decimal("95"), take_profit=Decimal("115"))
        issues = sv._check_risk_reward(signal)
        assert len(issues) == 0  # R/R = 15/5 = 3.0

    def test_long_bad_rr(self):
        """Long: R/R < 2.0 should fail."""
        sv = SignalValidator()
        signal = _make_signal(entry_price=Decimal("100"), stop_loss=Decimal("95"), take_profit=Decimal("104"))
        issues = sv._check_risk_reward(signal)
        assert any("R/R ratio" in i for i in issues)  # R/R = 4/5 = 0.8

    def test_short_valid_rr(self):
        """Short: SL > entry > TP with R/R >= 2.0."""
        sv = SignalValidator()
        signal = _make_signal(direction="short", entry_price=Decimal("100"), stop_loss=Decimal("105"), take_profit=Decimal("85"))
        issues = sv._check_risk_reward(signal)
        assert len(issues) == 0  # R/R = 15/5 = 3.0

    def test_sl_zero_fails(self):
        sv = SignalValidator()
        signal = _make_signal(stop_loss=Decimal("0"))
        issues = sv._check_risk_reward(signal)
        assert any("not set" in i for i in issues)


class TestEntryPrice:
    """Tests for _check_entry_price."""

    def test_within_spread_passes(self):
        sv = SignalValidator()
        signal = _make_signal(entry_price=Decimal("3450"))
        issues = sv._check_entry_price(signal, _make_raw_data())
        assert len(issues) == 0

    def test_outside_spread_fails(self):
        sv = SignalValidator()
        signal = _make_signal(entry_price=Decimal("4000"))
        issues = sv._check_entry_price(signal, _make_raw_data())
        assert any("outside realistic spread" in i for i in issues)

    def test_missing_bid_ask(self):
        sv = SignalValidator()
        signal = _make_signal()
        issues = sv._check_entry_price(signal, {"indicators": {}})
        assert any("Missing bid/ask" in i for i in issues)
```

**Step 2: Run tests**

Run: `.venv/bin/python -m pytest tests/test_anti_hallucination/test_signal_validator.py -v`
Expected: 13 PASSED

**Step 3: Commit**

```bash
git add tests/test_anti_hallucination/test_signal_validator.py
git commit -m "test: add signal_validator anti-hallucination tests (13 tests)"
```

---

### Task 8: Final Verification & Doc Update

**Step 1: Run full test suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: 228 + 31 + 12 + 13 = **284 tests passed** (approximate — exact count may vary if some helpers test as separate)

**Step 2: Update SSOT test count**

In `docs/SINGLE_SOURCE_OF_TRUTH.md`, update the test count from "228 tests" to the actual number. Also in `CLAUDE.md` Section 11 header.

**Step 3: Update SYSTEM_REVIEW.md test section**

Add new test files to the covered modules table. Move `order_manager.py`, `price_validator.py`, `signal_validator.py` from UNTESTED to COVERED.

**Step 4: Add CHANGELOG entry**

```markdown
#### Tests Added — P0 Safety-Critical Modules
- `tests/test_execution/test_order_manager.py` — 31 tests (idempotent submission, parsing, helpers, cancel/query, leverage validation)
- `tests/test_anti_hallucination/test_price_validator.py` — 12 tests (24h range, deviation, staleness, cross-validation, API errors)
- `tests/test_anti_hallucination/test_signal_validator.py` — 13 tests (indicator specificity, value matching, R/R, entry price, missing data)
```

**Step 5: Commit**

```bash
git add tests/ docs/ CHANGELOG.md CLAUDE.md
git commit -m "test: complete P0 safety-critical test suite (56 tests for order_manager, price_validator, signal_validator)"
```
