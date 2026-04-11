"""
Tests for OrderManager — order placement, verification, cancellation.

Covers helpers, parsing, idempotent submission safety, public order methods,
cancel/query, and the _require_exchange guard.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import ccxt.async_support as ccxt_async
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

# ---------------------------------------------------------------------------
# Module-level helper factories
# ---------------------------------------------------------------------------


def _make_connected_om(mock_exchange: MagicMock) -> OrderManager:
    """Create an OrderManager with a pre-injected mock _exchange.

    verify_orders is False so tests can focus on the logic under test
    without needing to mock the verification GET call.
    """
    om = OrderManager(
        api_key="test_key",
        api_secret="test_secret",
        testnet=True,
        verify_orders=False,
    )
    om._exchange = mock_exchange
    return om


def _raw_order_response(**overrides: object) -> dict:
    """Return a minimal ccxt-style raw order response dict.

    All values are realistic defaults. Pass keyword overrides to change any
    field.
    """
    base: dict = {
        "id": "123456789",
        "symbol": "ETH/USDT:USDT",
        "side": "buy",
        "type": "market",
        "amount": 0.05,
        "price": None,
        "stopPrice": None,
        "status": "closed",
        "filled": 0.05,
        "remaining": 0.0,
        "average": 1850.0,
        "cost": 92.5,
        "fee": {"cost": 0.037, "currency": "USDT"},
        "timestamp": 1710000000000,
        "lastTradeTimestamp": 1710000001000,
    }
    base.update(overrides)
    return base


# =========================================================================
# 1. TestHelpers  (9 tests)
# =========================================================================


class TestHelpers:
    """Unit tests for free-standing helper functions."""

    # --- _to_decimal ---

    def test_to_decimal_from_float(self):
        """Float is converted via its str representation."""
        assert _to_decimal(1.5) == Decimal("1.5")

    def test_to_decimal_from_int(self):
        assert _to_decimal(42) == Decimal("42")

    def test_to_decimal_from_string(self):
        assert _to_decimal("0.0037") == Decimal("0.0037")

    def test_to_decimal_none_returns_zero(self):
        """None must map to Decimal('0'), not raise."""
        assert _to_decimal(None) == Decimal("0")

    # --- _utc_from_ms ---

    def test_utc_from_ms_valid(self):
        """Known epoch millis should produce a known UTC datetime."""
        dt = _utc_from_ms(1_710_000_000_000)
        assert dt.tzinfo is not None  # must be timezone-aware
        assert dt == datetime(2024, 3, 9, 16, 0, 0, tzinfo=timezone.utc)

    def test_utc_from_ms_none_returns_now(self):
        """None should fall back to approximately 'now'."""
        before = datetime.now(tz=timezone.utc)
        dt = _utc_from_ms(None)
        after = datetime.now(tz=timezone.utc)
        assert before <= dt <= after

    # --- _parse_order_state ---

    def test_parse_order_state_known(self):
        assert _parse_order_state("closed") == OrderState.CLOSED

    def test_parse_order_state_cancelled_british(self):
        """Binance sometimes returns 'cancelled' (double-l)."""
        assert _parse_order_state("cancelled") == OrderState.CANCELED

    def test_parse_order_state_none(self):
        assert _parse_order_state(None) == OrderState.UNKNOWN

    # --- _generate_client_order_id ---
    # (covered as part of the 9-test count for this class — see below)


class TestGenerateClientOrderId:
    """Additional helper tests for _generate_client_order_id."""

    def test_prefix(self):
        cid = _generate_client_order_id()
        assert cid.startswith("cq_")

    def test_uniqueness(self):
        ids = {_generate_client_order_id() for _ in range(100)}
        assert len(ids) == 100, "Client order IDs must be unique"


# =========================================================================
# 2. TestParsing  (4 tests)
# =========================================================================


class TestParsing:
    """Tests for _parse_order_result and _parse_order_status."""

    # --- _parse_order_result ---

    def test_parse_order_result_fields(self):
        """All fields should be correctly extracted from a raw response."""
        om = OrderManager(verify_orders=False)
        raw = _raw_order_response()
        result = om._parse_order_result(raw, client_order_id="cq_abc123")

        assert result.order_id == "123456789"
        assert result.client_order_id == "cq_abc123"
        assert result.symbol == "ETH/USDT:USDT"
        assert result.side == OrderSide.BUY
        assert result.order_type == "market"
        assert result.amount == Decimal("0.05")
        assert result.filled == Decimal("0.05")
        assert result.average_fill_price == Decimal("1850.0")
        assert result.cost == Decimal("92.5")
        assert result.fee == Decimal("0.037")
        assert result.fee_currency == "USDT"
        assert result.status == "closed"
        assert isinstance(result.timestamp, datetime)

    def test_parse_order_result_no_fee(self):
        """When 'fee' key is missing or None, fee should default to 0."""
        om = OrderManager(verify_orders=False)
        raw = _raw_order_response(fee=None)
        result = om._parse_order_result(raw, client_order_id="cq_nofee")
        assert result.fee == Decimal("0")
        assert result.fee_currency is None

    # --- _parse_order_status ---

    def test_parse_order_status_fields(self):
        om = OrderManager(verify_orders=False)
        raw = _raw_order_response(status="open", remaining=0.03, filled=0.02)
        status = om._parse_order_status(raw)

        assert status.order_id == "123456789"
        assert status.status == OrderState.OPEN
        assert status.filled == Decimal("0.02")
        assert status.remaining == Decimal("0.03")

    def test_parse_order_status_stop_price_none(self):
        """When stopPrice is None, stop_price should be None (not Decimal 0)."""
        om = OrderManager(verify_orders=False)
        raw = _raw_order_response(stopPrice=None)
        status = om._parse_order_status(raw)
        assert status.stop_price is None

    def test_parse_order_status_marks_conditional_orders(self):
        """Conditional futures orders should be flagged for cancel/fetch routing."""
        om = OrderManager(verify_orders=False)
        raw = _raw_order_response(
            id="1000000034591175",
            info={"algoId": "1000000034591175", "algoType": "CONDITIONAL"},
            stopPrice=1800.0,
            type="market",
            status="open",
        )
        status = om._parse_order_status(raw)
        assert status.is_conditional is True


# =========================================================================
# 3. TestIdempotentSubmission  (8 tests) — the CRITICAL safety mechanism
# =========================================================================


@pytest.mark.asyncio
class TestIdempotentSubmission:
    """Tests for _submit_order_idempotent retry/dedup logic."""

    # ─── Success first attempt ───

    @patch("src.execution.order_manager.asyncio.sleep", new_callable=AsyncMock)
    async def test_success_first_attempt(self, _mock_sleep: AsyncMock):
        """Order succeeds on the first try — returned immediately."""
        mock_ex = MagicMock()
        mock_ex.create_order = AsyncMock(return_value=_raw_order_response())
        om = _make_connected_om(mock_ex)

        result = await om._submit_order_idempotent(
            symbol="ETH/USDT:USDT",
            order_type="market",
            side="buy",
            amount=0.05,
        )

        assert result is not None
        assert result.order_id == "123456789"
        mock_ex.create_order.assert_awaited_once()

    # ─── Timeout -> query -> found existing ───

    @patch("src.execution.order_manager.asyncio.sleep", new_callable=AsyncMock)
    async def test_timeout_query_found_existing(self, _mock_sleep: AsyncMock):
        """Timeout on create_order, query finds the order -> return it, no retry."""
        mock_ex = MagicMock()
        mock_ex.create_order = AsyncMock(
            side_effect=ccxt_async.RequestTimeout("timeout")
        )

        # _query_by_client_order_id calls exchange.market() and fapiPrivateGetOrder
        mock_ex.market = MagicMock(return_value={"id": "ETHUSDT"})
        mock_ex.fapiPrivateGetOrder = AsyncMock(
            return_value={"orderId": "999", "status": "FILLED"}
        )
        mock_ex.parse_order = MagicMock(
            return_value=_raw_order_response(id="999", status="closed")
        )

        om = _make_connected_om(mock_ex)
        result = await om._submit_order_idempotent(
            symbol="ETH/USDT:USDT",
            order_type="market",
            side="buy",
            amount=0.05,
        )

        assert result is not None
        assert result.order_id == "999"
        assert result.verified is True
        # Only 1 create_order call — no retry because we found the existing order
        assert mock_ex.create_order.await_count == 1

    # ─── Timeout -> query -> not found -> retries with new client_oid ───

    @patch("src.execution.order_manager.asyncio.sleep", new_callable=AsyncMock)
    async def test_timeout_query_not_found_retries(self, _mock_sleep: AsyncMock):
        """Timeout, query says NOT found -> retry with a new client order ID."""
        mock_ex = MagicMock()

        # First call: timeout. Second call: success.
        mock_ex.create_order = AsyncMock(
            side_effect=[
                ccxt_async.RequestTimeout("timeout"),
                _raw_order_response(id="777"),
            ]
        )
        # _query_by_client_order_id raises an exception -> returns None (not found)
        mock_ex.market = MagicMock(return_value={"id": "ETHUSDT"})
        mock_ex.fapiPrivateGetOrder = AsyncMock(
            side_effect=ccxt_async.OrderNotFound("not found")
        )

        om = _make_connected_om(mock_ex)
        result = await om._submit_order_idempotent(
            symbol="ETH/USDT:USDT",
            order_type="market",
            side="buy",
            amount=0.05,
        )

        assert result is not None
        assert result.order_id == "777"
        assert mock_ex.create_order.await_count == 2

        # Verify each attempt used a DIFFERENT client_oid (newClientOrderId)
        call1_params = mock_ex.create_order.call_args_list[0].kwargs.get(
            "params"
        ) or mock_ex.create_order.call_args_list[0][1].get("params", {})
        call2_params = mock_ex.create_order.call_args_list[1].kwargs.get(
            "params"
        ) or mock_ex.create_order.call_args_list[1][1].get("params", {})
        assert call1_params["newClientOrderId"] != call2_params["newClientOrderId"]

    # ─── InsufficientFunds -> None, no retry ───

    @patch("src.execution.order_manager.asyncio.sleep", new_callable=AsyncMock)
    async def test_insufficient_funds_returns_none(self, _mock_sleep: AsyncMock):
        mock_ex = MagicMock()
        mock_ex.create_order = AsyncMock(
            side_effect=ccxt_async.InsufficientFunds("no funds")
        )
        om = _make_connected_om(mock_ex)

        result = await om._submit_order_idempotent(
            symbol="ETH/USDT:USDT",
            order_type="market",
            side="buy",
            amount=0.05,
        )

        assert result is None
        mock_ex.create_order.assert_awaited_once()

    # ─── InvalidOrder -> None, no retry ───

    @patch("src.execution.order_manager.asyncio.sleep", new_callable=AsyncMock)
    async def test_invalid_order_returns_none(self, _mock_sleep: AsyncMock):
        mock_ex = MagicMock()
        mock_ex.create_order = AsyncMock(
            side_effect=ccxt_async.InvalidOrder("bad order")
        )
        om = _make_connected_om(mock_ex)

        result = await om._submit_order_idempotent(
            symbol="ETH/USDT:USDT",
            order_type="market",
            side="buy",
            amount=0.05,
        )

        assert result is None
        mock_ex.create_order.assert_awaited_once()

    # ─── ExchangeError -> raises immediately, no retry ───

    @patch("src.execution.order_manager.asyncio.sleep", new_callable=AsyncMock)
    async def test_exchange_error_raises_immediately(self, _mock_sleep: AsyncMock):
        mock_ex = MagicMock()
        mock_ex.create_order = AsyncMock(
            side_effect=ccxt_async.ExchangeError("server error")
        )
        om = _make_connected_om(mock_ex)

        with pytest.raises(ccxt_async.ExchangeError, match="server error"):
            await om._submit_order_idempotent(
                symbol="ETH/USDT:USDT",
                order_type="market",
                side="buy",
                amount=0.05,
            )

        mock_ex.create_order.assert_awaited_once()

    # ─── DDoS -> retries WITHOUT querying (order definitely not placed) ───

    @patch("src.execution.order_manager.asyncio.sleep", new_callable=AsyncMock)
    async def test_ddos_retries_without_query(self, _mock_sleep: AsyncMock):
        """DDoSProtection means order was NOT placed, so we retry without query."""
        mock_ex = MagicMock()
        mock_ex.create_order = AsyncMock(
            side_effect=[
                ccxt_async.DDoSProtection("rate limited"),
                _raw_order_response(id="888"),
            ]
        )
        # market / fapiPrivateGetOrder should NOT be called at all
        mock_ex.market = MagicMock(side_effect=AssertionError("should not query"))
        mock_ex.fapiPrivateGetOrder = AsyncMock(
            side_effect=AssertionError("should not query")
        )

        om = _make_connected_om(mock_ex)
        result = await om._submit_order_idempotent(
            symbol="ETH/USDT:USDT",
            order_type="market",
            side="buy",
            amount=0.05,
        )

        assert result is not None
        assert result.order_id == "888"
        assert mock_ex.create_order.await_count == 2

    # ─── All 3 retries exhausted -> RuntimeError ───

    @patch("src.execution.order_manager.asyncio.sleep", new_callable=AsyncMock)
    async def test_all_retries_exhausted_raises_runtime_error(
        self, _mock_sleep: AsyncMock
    ):
        mock_ex = MagicMock()
        mock_ex.create_order = AsyncMock(
            side_effect=ccxt_async.DDoSProtection("rate limited")
        )
        om = _make_connected_om(mock_ex)

        with pytest.raises(RuntimeError, match="failed after 3 idempotent attempts"):
            await om._submit_order_idempotent(
                symbol="ETH/USDT:USDT",
                order_type="market",
                side="buy",
                amount=0.05,
            )

        assert mock_ex.create_order.await_count == 3


# =========================================================================
# 4. TestPublicOrderMethods  (6 tests)
# =========================================================================


@pytest.mark.asyncio
class TestPublicOrderMethods:
    """Tests for place_market_order, place_stop_loss, place_take_profit, set_leverage."""

    # ─── place_market_order ───

    @patch("src.execution.order_manager.asyncio.sleep", new_callable=AsyncMock)
    async def test_place_market_order(self, _mock_sleep: AsyncMock):
        mock_ex = MagicMock()
        mock_ex.create_order = AsyncMock(return_value=_raw_order_response())
        om = _make_connected_om(mock_ex)

        result = await om.place_market_order(
            symbol="ETH/USDT:USDT",
            side="buy",
            amount=Decimal("0.05"),
        )

        assert result is not None
        assert result.order_id == "123456789"
        # Verify create_order was called with type="market"
        call_kwargs = mock_ex.create_order.call_args
        assert call_kwargs.kwargs["type"] == "market"
        assert call_kwargs.kwargs["side"] == "buy"
        assert call_kwargs.kwargs["amount"] == 0.05

    # ─── place_stop_loss (stopPrice param) ───

    @patch("src.execution.order_manager.asyncio.sleep", new_callable=AsyncMock)
    async def test_place_stop_loss_has_stop_price_param(
        self, _mock_sleep: AsyncMock
    ):
        mock_ex = MagicMock()
        mock_ex.create_order = AsyncMock(
            return_value=_raw_order_response(type="STOP_MARKET", stopPrice=1800.0)
        )
        om = _make_connected_om(mock_ex)

        result = await om.place_stop_loss(
            symbol="ETH/USDT:USDT",
            side="sell",
            amount=Decimal("0.05"),
            stop_price=Decimal("1800"),
        )

        assert result is not None
        call_kwargs = mock_ex.create_order.call_args
        assert call_kwargs.kwargs["type"] == "STOP_MARKET"
        assert call_kwargs.kwargs["params"]["stopPrice"] == 1800.0

    # ─── place_take_profit (stopPrice param) ───

    @patch("src.execution.order_manager.asyncio.sleep", new_callable=AsyncMock)
    async def test_place_take_profit_has_stop_price_param(
        self, _mock_sleep: AsyncMock
    ):
        mock_ex = MagicMock()
        mock_ex.create_order = AsyncMock(
            return_value=_raw_order_response(
                type="TAKE_PROFIT_MARKET", stopPrice=2000.0
            )
        )
        om = _make_connected_om(mock_ex)

        result = await om.place_take_profit(
            symbol="ETH/USDT:USDT",
            side="sell",
            amount=Decimal("0.05"),
            stop_price=Decimal("2000"),
        )

        assert result is not None
        call_kwargs = mock_ex.create_order.call_args
        assert call_kwargs.kwargs["type"] == "TAKE_PROFIT_MARKET"
        assert call_kwargs.kwargs["params"]["stopPrice"] == 2000.0

    # ─── set_leverage ───

    @patch("src.execution.order_manager.asyncio.sleep", new_callable=AsyncMock)
    async def test_set_leverage_valid(self, _mock_sleep: AsyncMock):
        mock_ex = MagicMock()
        mock_ex.set_leverage = AsyncMock()
        om = _make_connected_om(mock_ex)

        await om.set_leverage("ETH/USDT:USDT", 5)
        mock_ex.set_leverage.assert_awaited_once_with(5, "ETH/USDT:USDT")

    @patch("src.execution.order_manager.asyncio.sleep", new_callable=AsyncMock)
    async def test_set_leverage_too_high(self, _mock_sleep: AsyncMock):
        """Leverage > 10 should raise ValueError (system cap)."""
        mock_ex = MagicMock()
        om = _make_connected_om(mock_ex)

        with pytest.raises(ValueError, match="Leverage must be 1-10"):
            await om.set_leverage("ETH/USDT:USDT", 20)

    @patch("src.execution.order_manager.asyncio.sleep", new_callable=AsyncMock)
    async def test_set_leverage_too_low(self, _mock_sleep: AsyncMock):
        """Leverage < 1 should raise ValueError."""
        mock_ex = MagicMock()
        om = _make_connected_om(mock_ex)

        with pytest.raises(ValueError, match="Leverage must be 1-10"):
            await om.set_leverage("ETH/USDT:USDT", 0)


# =========================================================================
# 5. TestCancelAndQuery  (4 tests)
# =========================================================================


@pytest.mark.asyncio
class TestCancelAndQuery:
    """Tests for cancel_order, cancel_open_orders, get_order_status, _require_exchange."""

    # ─── cancel_order verifies ───

    @patch("src.execution.order_manager.asyncio.sleep", new_callable=AsyncMock)
    async def test_cancel_order_verifies_cancellation(
        self, _mock_sleep: AsyncMock
    ):
        """cancel_order should call cancel then verify the status is canceled."""
        mock_ex = MagicMock()
        mock_ex.cancel_order = AsyncMock()
        mock_ex.fetch_order = AsyncMock(
            return_value=_raw_order_response(status="canceled")
        )

        om = OrderManager(verify_orders=True)
        om._exchange = mock_ex

        await om.cancel_order("ETH/USDT:USDT", "123456789")

        mock_ex.cancel_order.assert_awaited_once_with(
            "123456789",
            "ETH/USDT:USDT",
            params={},
        )
        mock_ex.fetch_order.assert_awaited_once_with(
            "123456789",
            "ETH/USDT:USDT",
            params={},
        )

    # ─── cancel_open_orders counts ───

    @patch("src.execution.order_manager.asyncio.sleep", new_callable=AsyncMock)
    async def test_cancel_open_orders_counts(self, _mock_sleep: AsyncMock):
        """cancel_open_orders should cancel both regular and conditional orders."""
        mock_ex = MagicMock()
        mock_ex.fetch_open_orders = AsyncMock(
            side_effect=[
                [_raw_order_response(id="aaa", status="open")],
                [
                    _raw_order_response(
                        id="bbb",
                        status="open",
                        info={"algoId": "bbb", "algoType": "CONDITIONAL"},
                    )
                ],
            ]
        )
        mock_ex.cancel_order = AsyncMock()
        # verify_orders=False so cancel_order skips the verify step
        om = _make_connected_om(mock_ex)

        count, all_ok = await om.cancel_open_orders("ETH/USDT:USDT")
        assert count == 2
        assert all_ok is True
        assert mock_ex.cancel_order.await_count == 2
        cancel_calls = mock_ex.cancel_order.await_args_list
        assert cancel_calls[0].args == ("aaa", "ETH/USDT:USDT")
        assert cancel_calls[0].kwargs == {"params": {}}
        assert cancel_calls[1].args == ("bbb", "ETH/USDT:USDT")
        assert cancel_calls[1].kwargs == {"params": {"trigger": True}}

    @patch("src.execution.order_manager.asyncio.sleep", new_callable=AsyncMock)
    async def test_get_open_orders_includes_conditional_orders(
        self, _mock_sleep: AsyncMock
    ):
        """get_open_orders should combine regular and trigger/conditional orders."""
        mock_ex = MagicMock()
        mock_ex.fetch_open_orders = AsyncMock(
            side_effect=[
                [_raw_order_response(id="aaa", status="open")],
                [
                    _raw_order_response(
                        id="bbb",
                        status="open",
                        info={"algoId": "bbb", "algoType": "CONDITIONAL"},
                    )
                ],
            ]
        )
        om = _make_connected_om(mock_ex)

        orders = await om.get_open_orders("ETH/USDT:USDT")

        assert [order.order_id for order in orders] == ["aaa", "bbb"]
        assert orders[0].is_conditional is False
        assert orders[1].is_conditional is True
        fetch_calls = mock_ex.fetch_open_orders.await_args_list
        assert fetch_calls[0].args == ("ETH/USDT:USDT",)
        assert fetch_calls[1].args == ("ETH/USDT:USDT",)
        assert fetch_calls[1].kwargs == {"params": {"trigger": True}}

    # ─── get_order_status returns OrderStatus ───

    @patch("src.execution.order_manager.asyncio.sleep", new_callable=AsyncMock)
    async def test_get_order_status_returns_order_status(
        self, _mock_sleep: AsyncMock
    ):
        mock_ex = MagicMock()
        mock_ex.fetch_order = AsyncMock(
            return_value=_raw_order_response(status="closed", filled=0.05, remaining=0.0)
        )
        om = _make_connected_om(mock_ex)

        status = await om.get_order_status("ETH/USDT:USDT", "123456789")

        assert isinstance(status, OrderStatus)
        assert status.status == OrderState.CLOSED
        assert status.filled == Decimal("0.05")

    # ─── _require_exchange raises when not connected ───

    async def test_require_exchange_raises_when_not_connected(self):
        om = OrderManager(verify_orders=False)
        # _exchange is None by default (not connected)
        with pytest.raises(RuntimeError, match="not connected"):
            om._require_exchange()

    # ─── cancel_open_orders raises when ALL cancel attempts fail ───

    @patch("src.execution.order_manager.asyncio.sleep", new_callable=AsyncMock)
    async def test_cancel_open_orders_raises_on_total_failure(
        self, _mock_sleep: AsyncMock,
    ):
        mock_ex = MagicMock()
        om = _make_connected_om(mock_ex)
        # Mock get_open_orders to return 2 orders
        om.get_open_orders = AsyncMock(
            return_value=[
                OrderStatus(
                    order_id="o1", symbol="ETH/USDT:USDT",
                    side=OrderSide.SELL, order_type="STOP_MARKET",
                    amount=Decimal("0.05"), filled=Decimal("0"),
                    remaining=Decimal("0.05"), status=OrderState.OPEN,
                    timestamp=datetime.now(timezone.utc),
                    is_conditional=True,
                ),
                OrderStatus(
                    order_id="o2", symbol="ETH/USDT:USDT",
                    side=OrderSide.SELL, order_type="TAKE_PROFIT_MARKET",
                    amount=Decimal("0.05"), filled=Decimal("0"),
                    remaining=Decimal("0.05"), status=OrderState.OPEN,
                    timestamp=datetime.now(timezone.utc),
                    is_conditional=True,
                ),
            ]
        )
        # Mock cancel_order to always fail
        om.cancel_order = AsyncMock(
            side_effect=ccxt_async.ExchangeError("cancel failed"),
        )

        with pytest.raises(RuntimeError, match="all .* cancel attempts failed"):
            await om.cancel_open_orders("ETH/USDT:USDT")

    @patch("src.execution.order_manager.asyncio.sleep", new_callable=AsyncMock)
    async def test_cancel_open_orders_returns_zero_for_no_orders(
        self, _mock_sleep: AsyncMock,
    ):
        mock_ex = MagicMock()
        om = _make_connected_om(mock_ex)
        om.get_open_orders = AsyncMock(return_value=[])

        result = await om.cancel_open_orders("ETH/USDT:USDT")
        assert result == (0, True)

    # ─── _query_by_client_order_id retries on network error ───

    @patch("src.execution.order_manager.asyncio.sleep", new_callable=AsyncMock)
    async def test_query_by_client_oid_retries_on_network_error(
        self, _mock_sleep: AsyncMock,
    ):
        mock_ex = MagicMock()
        market_info = {"id": "ETHUSDT", "symbol": "ETH/USDT:USDT"}
        mock_ex.market = MagicMock(return_value=market_info)

        # First call: NetworkError, second call: success
        order_resp = _raw_order_response(
            id="found123", status="closed", filled=0.05,
        )
        mock_ex.fapiPrivateGetOrder = AsyncMock(
            side_effect=[
                ccxt_async.NetworkError("timeout"),
                order_resp,
            ]
        )
        mock_ex.parse_order = MagicMock(return_value=order_resp)
        om = _make_connected_om(mock_ex)

        result = await om._query_by_client_order_id(
            "ETH/USDT:USDT", "cq_test123"
        )
        assert result is not None
        assert mock_ex.fapiPrivateGetOrder.call_count == 2

    # ─── cancel_open_orders partial failure returns (count, False) ───

    @patch("src.execution.order_manager.asyncio.sleep", new_callable=AsyncMock)
    async def test_cancel_open_orders_partial_failure(
        self, _mock_sleep: AsyncMock,
    ):
        """When one cancel succeeds and another raises, return
        (1, False) — partial success."""
        mock_ex = MagicMock()
        # Two orders: regular + conditional
        mock_ex.fetch_open_orders = AsyncMock(
            side_effect=[
                [_raw_order_response(id="ok1", status="open")],
                [
                    _raw_order_response(
                        id="fail1",
                        status="open",
                        info={"algoId": "fail1", "algoType": "CONDITIONAL"},
                    )
                ],
            ]
        )
        # First cancel succeeds, second raises
        mock_ex.cancel_order = AsyncMock(
            side_effect=[None, ccxt_async.ExchangeError("order not found")]
        )
        om = _make_connected_om(mock_ex)

        count, all_ok = await om.cancel_open_orders("ETH/USDT:USDT")
        assert count == 1
        assert all_ok is False

    # ─── _query_by_client_order_id retries on NOT_FOUND ───

    @patch("src.execution.order_manager.asyncio.sleep", new_callable=AsyncMock)
    async def test_query_by_client_oid_retries_on_not_found(
        self, _mock_sleep: AsyncMock,
    ):
        """When the first query raises a generic exception (NOT_FOUND),
        it should retry once (settlement lag). On success, return the order."""
        mock_ex = MagicMock()
        market_info = {"id": "ETHUSDT", "symbol": "ETH/USDT:USDT"}
        mock_ex.market = MagicMock(return_value=market_info)

        order_resp = _raw_order_response(
            id="delayed123", status="closed", filled=0.05,
        )
        # First call: NOT_FOUND (generic exception), second call: success
        mock_ex.fapiPrivateGetOrder = AsyncMock(
            side_effect=[
                ccxt_async.OrderNotFound("order does not exist"),
                order_resp,
            ]
        )
        mock_ex.parse_order = MagicMock(return_value=order_resp)
        om = _make_connected_om(mock_ex)

        result = await om._query_by_client_order_id(
            "ETH/USDT:USDT", "cq_delayed"
        )
        assert result is not None
        assert mock_ex.fapiPrivateGetOrder.call_count == 2


# =========================================================================
# F8/R-A1 — workingType=MARK_PRICE + priceProtect on SL/TP
# =========================================================================


class TestMarkPriceParams:
    """Verify SL and TP orders include workingType=MARK_PRICE and priceProtect."""

    @pytest.mark.asyncio
    async def test_stop_loss_forwards_mark_price_params(self):
        mock_ex = MagicMock()
        mock_ex.create_order = AsyncMock(return_value=_raw_order_response(
            id="sl_001", type="stop_market", status="open", filled=0.0,
        ))
        om = _make_connected_om(mock_ex)

        await om.place_stop_loss(
            symbol="BTC/USDT:USDT",
            side="sell",
            amount=Decimal("0.001"),
            stop_price=Decimal("60000"),
        )

        mock_ex.create_order.assert_called_once()
        call_kwargs = mock_ex.create_order.call_args
        params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params", {})
        assert params["workingType"] == "MARK_PRICE", \
            f"workingType must be MARK_PRICE, got {params.get('workingType')}"
        assert params["priceProtect"] == "true", \
            f"priceProtect must be 'true', got {params.get('priceProtect')}"
        assert params["reduceOnly"] is True

    @pytest.mark.asyncio
    async def test_take_profit_forwards_mark_price_params(self):
        mock_ex = MagicMock()
        mock_ex.create_order = AsyncMock(return_value=_raw_order_response(
            id="tp_001", type="take_profit_market", status="open", filled=0.0,
        ))
        om = _make_connected_om(mock_ex)

        await om.place_take_profit(
            symbol="BTC/USDT:USDT",
            side="sell",
            amount=Decimal("0.001"),
            stop_price=Decimal("70000"),
        )

        mock_ex.create_order.assert_called_once()
        call_kwargs = mock_ex.create_order.call_args
        params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params", {})
        assert params["workingType"] == "MARK_PRICE"
        assert params["priceProtect"] == "true"
        assert params["reduceOnly"] is True


# =========================================================================
# R-A3 — Native TRAILING_STOP_MARKET order
# =========================================================================


class TestTrailingStopMarket:
    """Verify TRAILING_STOP_MARKET orders are placed correctly."""

    @pytest.mark.asyncio
    async def test_trailing_stop_market_forwards_params(self):
        mock_ex = MagicMock()
        mock_ex.create_order = AsyncMock(return_value=_raw_order_response(
            id="ts_001", type="trailing_stop_market", status="open", filled=0.0,
        ))
        om = _make_connected_om(mock_ex)

        await om.place_trailing_stop_market(
            symbol="BTC/USDT:USDT",
            side="sell",
            amount=Decimal("0.001"),
            callback_rate=2.5,
            activation_price=Decimal("65000"),
        )

        mock_ex.create_order.assert_called_once()
        call_kwargs = mock_ex.create_order.call_args
        params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params", {})
        assert params["callbackRate"] == 2.5
        assert params["activationPrice"] == 65000.0
        assert params["reduceOnly"] is True
        assert params["workingType"] == "MARK_PRICE"

    @pytest.mark.asyncio
    async def test_trailing_stop_clamps_callback_rate(self):
        mock_ex = MagicMock()
        mock_ex.create_order = AsyncMock(return_value=_raw_order_response(
            id="ts_002", type="trailing_stop_market", status="open", filled=0.0,
        ))
        om = _make_connected_om(mock_ex)

        # Rate above max should be clamped to 5.0
        await om.place_trailing_stop_market(
            symbol="ETH/USDT:USDT",
            side="sell",
            amount=Decimal("0.1"),
            callback_rate=8.0,
        )

        call_kwargs = mock_ex.create_order.call_args
        params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params", {})
        assert params["callbackRate"] == 5.0

    @pytest.mark.asyncio
    async def test_trailing_stop_without_activation_price(self):
        mock_ex = MagicMock()
        mock_ex.create_order = AsyncMock(return_value=_raw_order_response(
            id="ts_003", type="trailing_stop_market", status="open", filled=0.0,
        ))
        om = _make_connected_om(mock_ex)

        await om.place_trailing_stop_market(
            symbol="SOL/USDT:USDT",
            side="buy",
            amount=Decimal("10"),
            callback_rate=1.5,
        )

        call_kwargs = mock_ex.create_order.call_args
        params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params", {})
        assert "activationPrice" not in params
        assert params["callbackRate"] == 1.5


# =========================================================================
# R-A5 — GTX (post-only) limit orders
# =========================================================================


class TestGTXPostOnly:
    """Verify GTX timeInForce on post-only limit orders."""

    @pytest.mark.asyncio
    async def test_post_only_sets_gtx(self):
        mock_ex = MagicMock()
        mock_ex.create_order = AsyncMock(return_value=_raw_order_response(
            id="lmt_001", type="limit", status="open", filled=0.0,
        ))
        om = _make_connected_om(mock_ex)

        await om.place_limit_order(
            symbol="BTC/USDT:USDT",
            side="buy",
            amount=Decimal("0.001"),
            price=Decimal("60000"),
            post_only=True,
        )

        call_kwargs = mock_ex.create_order.call_args
        params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params", {})
        assert params["timeInForce"] == "GTX"

    @pytest.mark.asyncio
    async def test_non_post_only_no_gtx(self):
        mock_ex = MagicMock()
        mock_ex.create_order = AsyncMock(return_value=_raw_order_response(
            id="lmt_002", type="limit", status="open", filled=0.0,
        ))
        om = _make_connected_om(mock_ex)

        await om.place_limit_order(
            symbol="ETH/USDT:USDT",
            side="sell",
            amount=Decimal("0.1"),
            price=Decimal("2500"),
        )

        call_kwargs = mock_ex.create_order.call_args
        params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params", {})
        assert "timeInForce" not in params
