"""Tests for MarketDataClient — OHLCV, ticker, orderbook, and WebSocket."""

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.data.market_data import (
    AssetBalance,
    MarketDataClient,
    OrderBookData,
    TickerData,
)


@pytest.fixture
def client() -> MarketDataClient:
    return MarketDataClient(api_key="test", api_secret="test", testnet=True)


# ---------------------------------------------------------------------------
# Helpers / _to_decimal / _utc_from_ms
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_to_decimal_from_float(self) -> None:
        assert MarketDataClient._to_decimal(3.14) == Decimal("3.14")

    def test_to_decimal_from_int(self) -> None:
        assert MarketDataClient._to_decimal(42) == Decimal("42")

    def test_to_decimal_from_str(self) -> None:
        assert MarketDataClient._to_decimal("3500.00") == Decimal("3500.00")

    def test_to_decimal_none_raises(self) -> None:
        with pytest.raises(ValueError, match="None"):
            MarketDataClient._to_decimal(None)

    def test_utc_from_ms(self) -> None:
        # 2026-03-15 12:00:00 UTC in ms
        ms = 1773748800000
        dt = MarketDataClient._utc_from_ms(ms)
        assert dt.tzinfo is not None
        assert dt.year >= 2026

    def test_utc_from_ms_none(self) -> None:
        dt = MarketDataClient._utc_from_ms(None)
        assert dt.tzinfo is not None  # should still be UTC


# ---------------------------------------------------------------------------
# Stream name construction (critical — the bug that was fixed)
# ---------------------------------------------------------------------------


class TestStreamNameConstruction:
    """Verify that ccxt symbols are converted to the correct Binance WS stream
    names. This tests the fix for the bug where ETH/USDT:USDT → ethusdtusdt
    instead of ethusdt."""

    @pytest.mark.asyncio
    async def test_kline_stream_ethusdt(self, client: MarketDataClient) -> None:
        """ETH/USDT:USDT should produce stream 'ethusdt@kline_4h'."""
        with patch(
            "src.data.market_data.websockets.connect",
            side_effect=asyncio.CancelledError,
        ) as mock_connect:
            stop = asyncio.Event()
            await client._ws_kline_loop("ETH/USDT:USDT", "4h", AsyncMock(), stop)

        url = mock_connect.call_args[0][0]
        assert "ethusdt@kline_4h" in url
        assert "ethusdtusdt" not in url

    @pytest.mark.asyncio
    async def test_kline_stream_solusdt(self, client: MarketDataClient) -> None:
        """SOL/USDT:USDT should produce stream 'solusdt@kline_4h'."""
        with patch(
            "src.data.market_data.websockets.connect",
            side_effect=asyncio.CancelledError,
        ) as mock_connect:
            stop = asyncio.Event()
            await client._ws_kline_loop("SOL/USDT:USDT", "4h", AsyncMock(), stop)

        assert "solusdt@kline_4h" in mock_connect.call_args[0][0]

    @pytest.mark.asyncio
    async def test_kline_stream_dogeusdt(self, client: MarketDataClient) -> None:
        """DOGE/USDT:USDT should produce stream 'dogeusdt@kline_4h'."""
        with patch(
            "src.data.market_data.websockets.connect",
            side_effect=asyncio.CancelledError,
        ) as mock_connect:
            stop = asyncio.Event()
            await client._ws_kline_loop("DOGE/USDT:USDT", "4h", AsyncMock(), stop)

        assert "dogeusdt@kline_4h" in mock_connect.call_args[0][0]

    @pytest.mark.asyncio
    async def test_kline_stream_btcusdt(self, client: MarketDataClient) -> None:
        """BTC/USDT:USDT should produce stream 'btcusdt@kline_4h'."""
        with patch(
            "src.data.market_data.websockets.connect",
            side_effect=asyncio.CancelledError,
        ) as mock_connect:
            stop = asyncio.Event()
            await client._ws_kline_loop("BTC/USDT:USDT", "4h", AsyncMock(), stop)

        assert "btcusdt@kline_4h" in mock_connect.call_args[0][0]

    @pytest.mark.asyncio
    async def test_testnet_ws_url(self, client: MarketDataClient) -> None:
        """Testnet client should use the testnet WS base URL."""
        with patch(
            "src.data.market_data.websockets.connect",
            side_effect=asyncio.CancelledError,
        ) as mock_connect:
            stop = asyncio.Event()
            await client._ws_kline_loop("ETH/USDT:USDT", "4h", AsyncMock(), stop)

        assert mock_connect.call_args[0][0].startswith(MarketDataClient.TESTNET_WS_URL)

    @pytest.mark.asyncio
    async def test_production_ws_url(self) -> None:
        """Production client should use the production WS base URL."""
        prod_client = MarketDataClient(api_key="k", api_secret="s", testnet=False)
        with patch(
            "src.data.market_data.websockets.connect",
            side_effect=asyncio.CancelledError,
        ) as mock_connect:
            stop = asyncio.Event()
            await prod_client._ws_kline_loop("ETH/USDT:USDT", "4h", AsyncMock(), stop)

        assert mock_connect.call_args[0][0].startswith(MarketDataClient.PRODUCTION_WS_URL)


# ---------------------------------------------------------------------------
# fetch_ohlcv
# ---------------------------------------------------------------------------


class TestFetchOHLCV:
    @pytest.mark.asyncio
    async def test_fetch_ohlcv_parses_candles(self, client: MarketDataClient) -> None:
        mock_exchange = AsyncMock()
        mock_exchange.fetch_ohlcv.return_value = [
            [1710547200000, 3500.0, 3550.0, 3490.0, 3540.0, 1000.0],
            [1710550800000, 3540.0, 3560.0, 3530.0, 3555.0, 800.0],
        ]
        client._exchange = mock_exchange

        candles = await client.fetch_ohlcv("ETH/USDT:USDT", "4h", limit=2)
        assert len(candles) == 2
        assert candles[0]["open"] == Decimal("3500.0")
        assert candles[0]["close"] == Decimal("3540.0")
        assert isinstance(candles[0]["timestamp"], datetime)

    @pytest.mark.asyncio
    async def test_fetch_ohlcv_not_connected_raises(self, client: MarketDataClient) -> None:
        with pytest.raises(RuntimeError, match="not connected"):
            await client.fetch_ohlcv("ETH/USDT:USDT")


# ---------------------------------------------------------------------------
# fetch_ticker
# ---------------------------------------------------------------------------


class TestFetchTicker:
    @pytest.mark.asyncio
    async def test_fetch_ticker_returns_ticker_data(self, client: MarketDataClient) -> None:
        mock_exchange = AsyncMock()
        mock_exchange.fetch_ticker.return_value = {
            "bid": 3500.0,
            "ask": 3500.5,
            "last": 3500.2,
            "high": 3600.0,
            "low": 3400.0,
            "quoteVolume": 5000000.0,
            "timestamp": 1710547200000,
        }
        client._exchange = mock_exchange

        ticker = await client.fetch_ticker("ETH/USDT:USDT")
        assert isinstance(ticker, TickerData)
        assert ticker.last == Decimal("3500.2")
        assert ticker.symbol == "ETH/USDT:USDT"

    @pytest.mark.asyncio
    async def test_fetch_ticker_handles_none_values(self, client: MarketDataClient) -> None:
        """Binance testnet sometimes returns None for ticker fields.

        raw.get('last', 0) returns None when key exists with None value.
        The fix uses `or 0` to handle both missing and None.
        """
        mock_exchange = AsyncMock()
        mock_exchange.fetch_ticker.return_value = {
            "bid": None,
            "ask": None,
            "last": None,
            "high": None,
            "low": None,
            "quoteVolume": None,
            "timestamp": 1710547200000,
        }
        client._exchange = mock_exchange

        ticker = await client.fetch_ticker("ETH/USDT:USDT")
        assert isinstance(ticker, TickerData)
        assert ticker.last == Decimal("0")
        assert ticker.bid == Decimal("0")
        assert ticker.ask == Decimal("0")

    @pytest.mark.asyncio
    async def test_fetch_ticker_handles_missing_keys(self, client: MarketDataClient) -> None:
        """Missing keys should default to 0."""
        mock_exchange = AsyncMock()
        mock_exchange.fetch_ticker.return_value = {
            "timestamp": 1710547200000,
        }
        client._exchange = mock_exchange

        ticker = await client.fetch_ticker("ETH/USDT:USDT")
        assert ticker.last == Decimal("0")
        assert ticker.volume == Decimal("0")


# ---------------------------------------------------------------------------
# fetch_orderbook
# ---------------------------------------------------------------------------


class TestFetchOrderbook:
    @pytest.mark.asyncio
    async def test_fetch_orderbook(self, client: MarketDataClient) -> None:
        mock_exchange = AsyncMock()
        mock_exchange.fetch_order_book.return_value = {
            "bids": [[3500.0, 10.0], [3499.0, 5.0]],
            "asks": [[3501.0, 8.0], [3502.0, 3.0]],
            "timestamp": 1710547200000,
        }
        client._exchange = mock_exchange

        ob = await client.fetch_orderbook("ETH/USDT:USDT", limit=2)
        assert isinstance(ob, OrderBookData)
        assert len(ob.bids) == 2
        assert ob.bids[0].price == Decimal("3500.0")
        assert len(ob.asks) == 2


# ---------------------------------------------------------------------------
# get_account_balance / get_current_price
# ---------------------------------------------------------------------------


class TestBalance:
    @pytest.mark.asyncio
    async def test_get_account_balance(self, client: MarketDataClient) -> None:
        mock_exchange = AsyncMock()
        mock_exchange.fetch_balance.return_value = {
            "USDT": {"total": 5000.0, "free": 4800.0, "used": 200.0}
        }
        client._exchange = mock_exchange

        balance = await client.get_account_balance()
        assert balance == Decimal("5000.0")

    @pytest.mark.asyncio
    async def test_get_current_price(self, client: MarketDataClient) -> None:
        mock_exchange = AsyncMock()
        mock_exchange.fetch_ticker.return_value = {
            "bid": 3500, "ask": 3501, "last": 3500.5,
            "high": 3600, "low": 3400, "quoteVolume": 1000, "timestamp": None,
        }
        client._exchange = mock_exchange

        price = await client.get_current_price("ETH/USDT:USDT")
        assert price == Decimal("3500.5")


# ---------------------------------------------------------------------------
# WebSocket kline callback dispatch
# ---------------------------------------------------------------------------


class TestKlineCallbackDispatch:
    @pytest.mark.asyncio
    async def test_only_fires_on_candle_close(self, client: MarketDataClient) -> None:
        """Callback should fire only when k.x == True (closed candle)."""
        callback = AsyncMock()
        messages = [
            # Not closed
            json.dumps({"k": {"x": False, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "10", "T": 1710547200000}}),
            # Closed
            json.dumps({"k": {"x": True, "o": "1", "h": "2", "l": "0.5", "c": "1.8", "v": "20", "T": 1710547200000}}),
        ]

        mock_ws = AsyncMock()
        msg_iter = iter(messages)
        call_count = 0

        async def recv() -> str:
            nonlocal call_count
            call_count += 1
            try:
                return next(msg_iter)
            except StopIteration:
                # Signal loop to stop via CancelledError on the task
                raise asyncio.CancelledError

        mock_ws.recv = recv

        with patch("src.data.market_data.websockets.connect") as mock_connect:
            mock_connect.return_value.__aenter__ = AsyncMock(return_value=mock_ws)
            mock_connect.return_value.__aexit__ = AsyncMock(return_value=False)

            stop = asyncio.Event()
            await client._ws_kline_loop("ETH/USDT:USDT", "4h", callback, stop)

        callback.assert_called_once()
        candle = callback.call_args[0][0]
        assert candle["close"] == 1.8
        assert candle["symbol"] == "ETH/USDT:USDT"

    @pytest.mark.asyncio
    async def test_subscribe_and_unsubscribe(self, client: MarketDataClient) -> None:
        """subscribe_kline_close / unsubscribe_kline lifecycle."""
        callback = AsyncMock()
        key = "ETH/USDT:USDT@kline_4h"

        with patch(
            "src.data.market_data.websockets.connect",
            side_effect=asyncio.CancelledError,
        ):
            await client.subscribe_kline_close("ETH/USDT:USDT", "4h", callback)
            assert key in client._ws_connections

            # Give the task a moment to start and finish
            await asyncio.sleep(0.05)

            await client.unsubscribe_kline("ETH/USDT:USDT", "4h")
            assert key not in client._ws_connections
            assert key not in client._ws_stop_events


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_require_exchange_raises_when_not_connected(
        self, client: MarketDataClient
    ) -> None:
        with pytest.raises(RuntimeError, match="not connected"):
            client._require_exchange()


# ---------------------------------------------------------------------------
# get_all_assets
# ---------------------------------------------------------------------------


class TestGetAllAssets:
    """Tests for the multi-asset balance method."""

    @pytest.mark.asyncio
    async def test_returns_all_nonzero_assets(
        self, client: MarketDataClient,
    ) -> None:
        mock_exchange = AsyncMock()
        mock_exchange.fetch_balance = AsyncMock(return_value={
            "info": {
                "assets": [
                    {
                        "asset": "USDT",
                        "walletBalance": "5099.92",
                        "unrealizedProfit": "45.72",
                        "marginBalance": "5145.64",
                        "availableBalance": "4800.00",
                    },
                    {
                        "asset": "USDC",
                        "walletBalance": "5000.00",
                        "unrealizedProfit": "0.00",
                        "marginBalance": "5000.00",
                        "availableBalance": "5000.00",
                    },
                    {
                        "asset": "BTC",
                        "walletBalance": "0.01",
                        "unrealizedProfit": "0.00",
                        "marginBalance": "0.01",
                        "availableBalance": "0.01",
                    },
                    {
                        "asset": "ETH",
                        "walletBalance": "0",
                        "unrealizedProfit": "0",
                        "marginBalance": "0",
                        "availableBalance": "0",
                    },
                ],
            },
        })
        client._exchange = mock_exchange

        assets = await client.get_all_assets()

        assert len(assets) == 3
        names = [a.asset for a in assets]
        assert "USDT" in names
        assert "USDC" in names
        assert "BTC" in names
        assert "ETH" not in names  # zero balance filtered out

    @pytest.mark.asyncio
    async def test_usdt_values_correct(
        self, client: MarketDataClient,
    ) -> None:
        mock_exchange = AsyncMock()
        mock_exchange.fetch_balance = AsyncMock(return_value={
            "info": {
                "assets": [
                    {
                        "asset": "USDT",
                        "walletBalance": "5099.92",
                        "unrealizedProfit": "45.72",
                        "marginBalance": "5145.64",
                        "availableBalance": "4800.00",
                    },
                ],
            },
        })
        client._exchange = mock_exchange

        assets = await client.get_all_assets()

        assert len(assets) == 1
        usdt = assets[0]
        assert usdt.asset == "USDT"
        assert usdt.wallet_balance == Decimal("5099.92")
        assert usdt.unrealized_pnl == Decimal("45.72")
        assert usdt.margin_balance == Decimal("5145.64")
        assert usdt.available_balance == Decimal("4800.00")

    @pytest.mark.asyncio
    async def test_empty_assets_returns_empty(
        self, client: MarketDataClient,
    ) -> None:
        mock_exchange = AsyncMock()
        mock_exchange.fetch_balance = AsyncMock(return_value={
            "info": {"assets": []},
        })
        client._exchange = mock_exchange

        assets = await client.get_all_assets()
        assert assets == []

    @pytest.mark.asyncio
    async def test_missing_assets_key_returns_empty(
        self, client: MarketDataClient,
    ) -> None:
        mock_exchange = AsyncMock()
        mock_exchange.fetch_balance = AsyncMock(return_value={
            "info": {},
        })
        client._exchange = mock_exchange

        assets = await client.get_all_assets()
        assert assets == []

    @pytest.mark.asyncio
    async def test_asset_balance_is_frozen(self) -> None:
        ab = AssetBalance(
            asset="USDT",
            wallet_balance=Decimal("100"),
            unrealized_pnl=Decimal("0"),
            margin_balance=Decimal("100"),
            available_balance=Decimal("100"),
        )
        with pytest.raises(Exception):
            ab.asset = "BTC"  # type: ignore[misc]
