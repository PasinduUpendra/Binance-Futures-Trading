"""Tests for MarketDataClient.subscribe_kline_close WebSocket integration."""

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.data.market_data import MarketDataClient


@pytest.fixture
def client() -> MarketDataClient:
    return MarketDataClient(api_key="test", api_secret="test", testnet=True)


def _make_kline_msg(*, closed: bool, close: str = "3540.00") -> str:
    """Helper to build a kline WS message."""
    return json.dumps({
        "e": "kline",
        "E": 1710547200000,
        "s": "ETHUSDT",
        "k": {
            "t": 1710532800000,
            "T": 1710547199999,
            "s": "ETHUSDT",
            "i": "4h",
            "o": "3500.00",
            "h": "3550.00",
            "l": "3490.00",
            "c": close,
            "v": "1000.00",
            "x": closed,
        },
    })


class TestSubscribeKlineClose:
    """Tests for the kline close WebSocket subscription."""

    @pytest.mark.asyncio
    async def test_subscribe_creates_task(self, client: MarketDataClient) -> None:
        """subscribe_kline_close should register a WS task for the symbol+timeframe."""
        callback = AsyncMock()
        with patch("src.data.market_data.websockets") as mock_ws:
            mock_ws.connect.return_value.__aenter__ = AsyncMock(
                side_effect=asyncio.CancelledError
            )
            await client.subscribe_kline_close("ETH/USDT:USDT", "4h", callback)

            key = "ETH/USDT:USDT@kline_4h"
            assert key in client._ws_connections
            assert key in client._ws_stop_events

            await client.unsubscribe_kline("ETH/USDT:USDT", "4h")

    @pytest.mark.asyncio
    async def test_duplicate_subscribe_warns(self, client: MarketDataClient) -> None:
        """Subscribing twice to the same symbol+timeframe should not overwrite."""
        callback = AsyncMock()
        key = "ETH/USDT:USDT@kline_4h"

        client._ws_connections[key] = MagicMock()
        client._ws_stop_events[key] = asyncio.Event()

        await client.subscribe_kline_close("ETH/USDT:USDT", "4h", callback)
        callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_unsubscribe_cleans_up(self, client: MarketDataClient) -> None:
        """unsubscribe_kline should remove the task and stop event."""
        key = "ETH/USDT:USDT@kline_4h"
        stop_event = asyncio.Event()
        mock_task = AsyncMock()
        client._ws_connections[key] = mock_task
        client._ws_stop_events[key] = stop_event

        await client.unsubscribe_kline("ETH/USDT:USDT", "4h")

        assert key not in client._ws_connections
        assert key not in client._ws_stop_events
        assert stop_event.is_set()

    @pytest.mark.asyncio
    async def test_callback_fires_on_closed_candle(self, client: MarketDataClient) -> None:
        """Callback should fire when kline message has k.x == True."""
        received: list[dict[str, Any]] = []

        async def on_close(candle: dict[str, Any]) -> None:
            received.append(candle)

        closed_msg = _make_kline_msg(closed=True)
        open_msg = _make_kline_msg(closed=False)

        stop_event = asyncio.Event()

        async def _fake_recv() -> str:
            if _fake_recv.call_count == 0:
                _fake_recv.call_count += 1
                return open_msg
            elif _fake_recv.call_count == 1:
                _fake_recv.call_count += 1
                return closed_msg
            else:
                stop_event.set()
                await asyncio.sleep(10)
                return ""

        _fake_recv.call_count = 0  # type: ignore[attr-defined]

        mock_ws_inner = AsyncMock()
        mock_ws_inner.recv = _fake_recv

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ws_inner)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("src.data.market_data.websockets") as ws_mod:
            ws_mod.connect.return_value = mock_ctx

            task = asyncio.create_task(
                client._ws_kline_loop("ETH/USDT:USDT", "4h", on_close, stop_event)
            )
            await asyncio.wait_for(stop_event.wait(), timeout=5.0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert len(received) == 1
        assert received[0]["symbol"] == "ETH/USDT:USDT"
        assert received[0]["timeframe"] == "4h"
        assert received[0]["close"] == 3540.00
        assert received[0]["high"] == 3550.00

    @pytest.mark.asyncio
    async def test_callback_ignores_non_close(self, client: MarketDataClient) -> None:
        """Callback should NOT fire when kline message has k.x == False."""
        received: list[dict[str, Any]] = []

        async def on_close(candle: dict[str, Any]) -> None:
            received.append(candle)

        open_msg = _make_kline_msg(closed=False)
        stop_event = asyncio.Event()

        async def _fake_recv() -> str:
            if _fake_recv.call_count == 0:
                _fake_recv.call_count += 1
                return open_msg
            else:
                stop_event.set()
                await asyncio.sleep(10)
                return ""

        _fake_recv.call_count = 0  # type: ignore[attr-defined]

        mock_ws_inner = AsyncMock()
        mock_ws_inner.recv = _fake_recv

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ws_inner)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("src.data.market_data.websockets") as ws_mod:
            ws_mod.connect.return_value = mock_ctx

            task = asyncio.create_task(
                client._ws_kline_loop("ETH/USDT:USDT", "4h", on_close, stop_event)
            )
            await asyncio.wait_for(stop_event.wait(), timeout=5.0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_callback_receives_correct_data_types(self, client: MarketDataClient) -> None:
        """Candle data passed to callback should have correct types."""
        received: list[dict[str, Any]] = []

        async def on_close(candle: dict[str, Any]) -> None:
            received.append(candle)

        closed_msg = json.dumps({
            "e": "kline", "E": 1710547200000, "s": "SOLUSDT",
            "k": {
                "o": "150.50", "h": "155.00", "l": "149.00",
                "c": "153.25", "v": "50000.00", "x": True,
                "t": 1710532800000, "T": 1710547199999,
            },
        })

        stop_event = asyncio.Event()

        async def _fake_recv() -> str:
            if _fake_recv.call_count == 0:
                _fake_recv.call_count += 1
                return closed_msg
            else:
                stop_event.set()
                await asyncio.sleep(10)
                return ""

        _fake_recv.call_count = 0  # type: ignore[attr-defined]

        mock_ws_inner = AsyncMock()
        mock_ws_inner.recv = _fake_recv

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_ws_inner)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("src.data.market_data.websockets") as ws_mod:
            ws_mod.connect.return_value = mock_ctx

            task = asyncio.create_task(
                client._ws_kline_loop("SOL/USDT:USDT", "4h", on_close, stop_event)
            )
            await asyncio.wait_for(stop_event.wait(), timeout=5.0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert len(received) == 1
        c = received[0]
        assert isinstance(c["open"], float)
        assert isinstance(c["high"], float)
        assert isinstance(c["low"], float)
        assert isinstance(c["close"], float)
        assert isinstance(c["volume"], float)
        assert isinstance(c["timestamp"], datetime)
        assert c["symbol"] == "SOL/USDT:USDT"
        assert c["timeframe"] == "4h"

    @pytest.mark.asyncio
    async def test_reconnects_on_disconnect(self, client: MarketDataClient) -> None:
        """Loop should reconnect after a WebSocket disconnect and deliver candle."""
        received: list[dict[str, Any]] = []

        async def on_close(candle: dict[str, Any]) -> None:
            received.append(candle)

        closed_msg = _make_kline_msg(closed=True, close="3600.00")
        stop_event = asyncio.Event()

        async def _fake_recv() -> str:
            if _fake_recv.call_count == 0:
                _fake_recv.call_count += 1
                return closed_msg
            else:
                stop_event.set()
                await asyncio.sleep(10)
                return ""

        _fake_recv.call_count = 0  # type: ignore[attr-defined]

        mock_ws_inner = AsyncMock()
        mock_ws_inner.recv = _fake_recv

        mock_ctx_ok = AsyncMock()
        mock_ctx_ok.__aenter__ = AsyncMock(return_value=mock_ws_inner)
        mock_ctx_ok.__aexit__ = AsyncMock(return_value=False)

        connect_count = 0

        def _connect_side_effect(*args: Any, **kwargs: Any) -> Any:
            nonlocal connect_count
            connect_count += 1
            if connect_count == 1:
                raise ConnectionError("WS died")
            return mock_ctx_ok

        with patch("src.data.market_data.websockets") as ws_mod, \
             patch("src.data.market_data.asyncio.sleep", new_callable=AsyncMock):
            ws_mod.connect.side_effect = _connect_side_effect

            task = asyncio.create_task(
                client._ws_kline_loop("ETH/USDT:USDT", "4h", on_close, stop_event)
            )
            await asyncio.wait_for(stop_event.wait(), timeout=5.0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert len(received) == 1
        assert received[0]["close"] == 3600.00
