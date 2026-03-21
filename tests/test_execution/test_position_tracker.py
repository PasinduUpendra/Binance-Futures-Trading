"""Tests for PositionTracker — Binance Futures position fetching via ccxt."""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.execution.position_tracker import Position, PositionTracker, _to_decimal


# ---------------------------------------------------------------------------
# _to_decimal helper
# ---------------------------------------------------------------------------


class TestToDecimal:
    def test_from_float(self) -> None:
        assert _to_decimal(3.14) == Decimal("3.14")

    def test_from_int(self) -> None:
        assert _to_decimal(42) == Decimal("42")

    def test_from_str(self) -> None:
        assert _to_decimal("100.5") == Decimal("100.5")

    def test_none_returns_zero(self) -> None:
        assert _to_decimal(None) == Decimal("0")


# ---------------------------------------------------------------------------
# Position model
# ---------------------------------------------------------------------------


class TestPositionModel:
    def test_create_position(self) -> None:
        pos = Position(
            symbol="ETH/USDT:USDT",
            side="long",
            size=Decimal("0.5"),
            entry_price=Decimal("3500"),
            current_price=Decimal("3550"),
            unrealized_pnl=Decimal("25.00"),
            leverage=5,
        )
        assert pos.symbol == "ETH/USDT:USDT"
        assert pos.side == "long"
        assert pos.leverage == 5


# ---------------------------------------------------------------------------
# _parse_position
# ---------------------------------------------------------------------------


@pytest.fixture
def tracker() -> PositionTracker:
    return PositionTracker(api_key="test", api_secret="test", testnet=True)


def _raw_position(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "symbol": "ETH/USDT:USDT",
        "side": "long",
        "contracts": 0.5,
        "entryPrice": 3500.0,
        "markPrice": 3550.0,
        "unrealizedPnl": 25.0,
        "leverage": 5,
        "liquidationPrice": 2800.0,
        "notional": 1775.0,
        "marginMode": "isolated",
        "collateral": 355.0,
    }
    base.update(overrides)
    return base


class TestParsePosition:
    def test_parses_long_position(self, tracker: PositionTracker) -> None:
        pos = tracker._parse_position(_raw_position())
        assert pos is not None
        assert pos.symbol == "ETH/USDT:USDT"
        assert pos.side == "long"
        assert pos.size == Decimal("0.5")
        assert pos.entry_price == Decimal("3500.0")
        assert pos.leverage == 5
        assert pos.liquidation_price == Decimal("2800.0")

    def test_parses_short_position(self, tracker: PositionTracker) -> None:
        pos = tracker._parse_position(_raw_position(side="short", notional=-1775.0))
        assert pos is not None
        assert pos.side == "short"
        assert pos.notional_value == Decimal("1775.0")  # absolute

    def test_zero_contracts_returns_none(self, tracker: PositionTracker) -> None:
        pos = tracker._parse_position(_raw_position(contracts=0))
        assert pos is None

    def test_buy_side_normalized_to_long(self, tracker: PositionTracker) -> None:
        pos = tracker._parse_position(_raw_position(side="buy"))
        assert pos is not None
        assert pos.side == "long"

    def test_sell_side_normalized_to_short(self, tracker: PositionTracker) -> None:
        pos = tracker._parse_position(_raw_position(side="sell"))
        assert pos is not None
        assert pos.side == "short"

    def test_unknown_side_returns_none(self, tracker: PositionTracker) -> None:
        pos = tracker._parse_position(_raw_position(side="invalid"))
        assert pos is None

    def test_zero_liquidation_price_becomes_none(self, tracker: PositionTracker) -> None:
        pos = tracker._parse_position(_raw_position(liquidationPrice=0))
        assert pos is not None
        assert pos.liquidation_price is None

    def test_none_liquidation_price_becomes_none(self, tracker: PositionTracker) -> None:
        pos = tracker._parse_position(_raw_position(liquidationPrice=None))
        assert pos is not None
        assert pos.liquidation_price is None

    def test_negative_notional_abs(self, tracker: PositionTracker) -> None:
        pos = tracker._parse_position(_raw_position(notional=-5000.0))
        assert pos is not None
        assert pos.notional_value == Decimal("5000.0")

    def test_margin_from_collateral(self, tracker: PositionTracker) -> None:
        pos = tracker._parse_position(_raw_position(collateral=355.0))
        assert pos is not None
        assert pos.margin == Decimal("355.0")

    def test_margin_from_initial_margin_fallback(self, tracker: PositionTracker) -> None:
        raw = _raw_position()
        del raw["collateral"]
        raw["initialMargin"] = 350.0
        pos = tracker._parse_position(raw)
        assert pos is not None
        assert pos.margin == Decimal("350.0")

    def test_margin_type_defaults_cross(self, tracker: PositionTracker) -> None:
        raw = _raw_position()
        raw["marginMode"] = None
        raw.pop("marginMode", None)
        pos = tracker._parse_position(raw)
        assert pos is not None
        assert pos.margin_type == "cross"


# ---------------------------------------------------------------------------
# get_open_positions / get_position
# ---------------------------------------------------------------------------


class TestGetPositions:
    @pytest.mark.asyncio
    async def test_get_open_positions(self, tracker: PositionTracker) -> None:
        mock_exchange = AsyncMock()
        mock_exchange.fetch_positions.return_value = [
            _raw_position(symbol="ETH/USDT:USDT"),
            _raw_position(symbol="SOL/USDT:USDT", contracts=0),  # not open
            _raw_position(symbol="DOGE/USDT:USDT", contracts=100),
        ]
        tracker._exchange = mock_exchange

        positions = await tracker.get_open_positions()
        assert len(positions) == 2
        symbols = {p.symbol for p in positions}
        assert "ETH/USDT:USDT" in symbols
        assert "DOGE/USDT:USDT" in symbols

    @pytest.mark.asyncio
    async def test_get_position_found(self, tracker: PositionTracker) -> None:
        mock_exchange = AsyncMock()
        mock_exchange.fetch_positions.return_value = [
            _raw_position(symbol="ETH/USDT:USDT"),
        ]
        tracker._exchange = mock_exchange

        pos = await tracker.get_position("ETH/USDT:USDT")
        assert pos is not None
        assert pos.symbol == "ETH/USDT:USDT"

    @pytest.mark.asyncio
    async def test_get_position_not_found(self, tracker: PositionTracker) -> None:
        mock_exchange = AsyncMock()
        mock_exchange.fetch_positions.return_value = [
            _raw_position(symbol="ETH/USDT:USDT", contracts=0),
        ]
        tracker._exchange = mock_exchange

        pos = await tracker.get_position("ETH/USDT:USDT")
        assert pos is None

    @pytest.mark.asyncio
    async def test_not_connected_raises(self, tracker: PositionTracker) -> None:
        with pytest.raises(RuntimeError, match="not connected"):
            await tracker.get_open_positions()


# ---------------------------------------------------------------------------
# get_unrealized_pnl / get_total_exposure
# ---------------------------------------------------------------------------


class TestAggregates:
    @pytest.mark.asyncio
    async def test_unrealized_pnl(self, tracker: PositionTracker) -> None:
        mock_exchange = AsyncMock()
        mock_exchange.fetch_positions.return_value = [
            _raw_position(unrealizedPnl=10.0),
            _raw_position(unrealizedPnl=-3.0),
        ]
        tracker._exchange = mock_exchange

        pnl = await tracker.get_unrealized_pnl()
        assert pnl == Decimal("7.0")

    @pytest.mark.asyncio
    async def test_total_exposure(self, tracker: PositionTracker) -> None:
        mock_exchange = AsyncMock()
        mock_exchange.fetch_positions.return_value = [
            _raw_position(notional=1000.0),
            _raw_position(notional=-500.0),
        ]
        tracker._exchange = mock_exchange

        exposure = await tracker.get_total_exposure()
        assert exposure == Decimal("1500.0")

    @pytest.mark.asyncio
    async def test_no_positions_returns_zero(self, tracker: PositionTracker) -> None:
        mock_exchange = AsyncMock()
        mock_exchange.fetch_positions.return_value = []
        tracker._exchange = mock_exchange

        assert await tracker.get_unrealized_pnl() == Decimal("0")
        assert await tracker.get_total_exposure() == Decimal("0")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_connect_uses_demo_trading_for_testnet(
        self, tracker: PositionTracker
    ) -> None:
        with patch("src.execution.position_tracker.ccxt_async") as mock_ccxt:
            mock_exchange = AsyncMock()
            mock_ccxt.binanceusdm.return_value = mock_exchange

            await tracker.connect()

            mock_exchange.enable_demo_trading.assert_called_once_with(True)
            mock_exchange.load_markets.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_cleans_up(self, tracker: PositionTracker) -> None:
        mock_exchange = AsyncMock()
        tracker._exchange = mock_exchange

        await tracker.close()
        mock_exchange.close.assert_awaited_once()
        assert tracker._exchange is None

    @pytest.mark.asyncio
    async def test_context_manager(self) -> None:
        with patch("src.execution.position_tracker.ccxt_async") as mock_ccxt:
            mock_exchange = AsyncMock()
            mock_ccxt.binanceusdm.return_value = mock_exchange

            t = PositionTracker(api_key="k", api_secret="s", testnet=True)
            async with t:
                assert t._exchange is not None
            mock_exchange.close.assert_awaited_once()
