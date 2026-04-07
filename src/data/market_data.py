"""
Market data client for Binance Futures via ccxt.

Provides async OHLCV, ticker, orderbook fetching and real-time WebSocket
price subscriptions. All prices use Decimal for precision and timestamps
are always UTC.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Coroutine

import ccxt.async_support as ccxt_async
import websockets
from pydantic import BaseModel, Field

logger = logging.getLogger("claude_quant.data.market_data")

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class TickerData(BaseModel):
    """Snapshot of a single symbol's ticker."""

    symbol: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    high: Decimal
    low: Decimal
    volume: Decimal
    timestamp: datetime


class OrderBookEntry(BaseModel):
    price: Decimal
    amount: Decimal


class OrderBookData(BaseModel):
    symbol: str
    bids: list[OrderBookEntry]
    asks: list[OrderBookEntry]
    timestamp: datetime


class AssetBalance(BaseModel, frozen=True):
    """Balance details for a single asset on the Futures account."""

    asset: str
    wallet_balance: Decimal
    unrealized_pnl: Decimal
    margin_balance: Decimal
    available_balance: Decimal


# ---------------------------------------------------------------------------
# Retry decorator
# ---------------------------------------------------------------------------

_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0  # seconds


def _retry(func: Callable[..., Coroutine[Any, Any, Any]]) -> Callable[..., Coroutine[Any, Any, Any]]:
    """Decorator: retry an async method up to _MAX_RETRIES with exponential backoff."""

    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                return await func(*args, **kwargs)
            except (
                ccxt_async.NetworkError,
                ccxt_async.ExchangeNotAvailable,
                ccxt_async.RequestTimeout,
                ccxt_async.DDoSProtection,
            ) as exc:
                last_exc = exc
                wait = _BACKOFF_BASE * (2 ** (attempt - 1))
                logger.warning(
                    "Attempt %d/%d for %s failed (%s). Retrying in %.1fs ...",
                    attempt,
                    _MAX_RETRIES,
                    func.__qualname__,
                    exc,
                    wait,
                )
                await asyncio.sleep(wait)
            except ccxt_async.ExchangeError as exc:
                # Non-transient exchange errors should not be retried.
                logger.error("%s raised ExchangeError: %s", func.__qualname__, exc)
                raise
        # All retries exhausted
        raise RuntimeError(
            f"{func.__qualname__} failed after {_MAX_RETRIES} attempts"
        ) from last_exc

    wrapper.__qualname__ = func.__qualname__
    wrapper.__name__ = func.__name__
    return wrapper


# ---------------------------------------------------------------------------
# MarketDataClient
# ---------------------------------------------------------------------------


class MarketDataClient:
    """Async Binance Futures market-data client backed by ccxt.

    Parameters
    ----------
    api_key : str | None
        Binance API key.  Defaults to ``BINANCE_API_KEY`` env var.
    api_secret : str | None
        Binance API secret.  Defaults to ``BINANCE_API_SECRET`` env var.
    testnet : bool | None
        If *True* use Binance Futures testnet.  Defaults to ``BINANCE_TESTNET``
        env var (``"true"`` means testnet, anything else means production).
    """

    TESTNET_WS_URL = "wss://fstream.binancefuture.com/ws"
    PRODUCTION_WS_URL = "wss://fstream.binance.com/ws"

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        testnet: bool | None = None,
    ) -> None:
        if testnet is None:
            testnet = os.getenv("BINANCE_TESTNET", "false").lower() == "true"
        self._testnet = testnet

        if api_key:
            self._api_key = api_key
            self._api_secret = api_secret or ""
        elif testnet:
            self._api_key = os.getenv("BINANCE_API_KEY", "")
            self._api_secret = os.getenv("BINANCE_API_SECRET", "")
        else:
            self._api_key = os.getenv("BINANCE_API_KEY_PROD", "")
            self._api_secret = os.getenv("BINANCE_API_SECRET_PROD", "")

        self._exchange: ccxt_async.binanceusdm | None = None
        self._ws_connections: dict[str, asyncio.Task[None]] = {}
        self._ws_stop_events: dict[str, asyncio.Event] = {}

    # -- lifecycle -----------------------------------------------------------

    async def connect(self) -> None:
        """Initialise the ccxt exchange instance and load markets."""
        if self._exchange is not None:
            return

        self._exchange = ccxt_async.binanceusdm(
            {
                "apiKey": self._api_key,
                "secret": self._api_secret,
                "enableRateLimit": True,
                "options": {
                    "defaultType": "future",
                    "adjustForTimeDifference": True,
                },
            }
        )

        if self._testnet:
            # ccxt >= 4.5.6: enable_demo_trading routes to demo-fapi.binance.com
            # The old set_sandbox_mode(True) routes to deprecated endpoints and
            # causes auth failures (see ccxt issues #26487, #27560).
            self._exchange.enable_demo_trading(True)
            logger.info("MarketDataClient connected to Binance Futures TESTNET (demo)")
        else:
            logger.info("MarketDataClient connected to Binance Futures PRODUCTION")

        await self._exchange.load_markets()

    async def close(self) -> None:
        """Shut down the exchange connection and all WebSocket subscriptions."""
        # Cancel WebSocket tasks
        for symbol, stop_event in list(self._ws_stop_events.items()):
            stop_event.set()
        for symbol, task in list(self._ws_connections.items()):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._ws_connections.clear()
        self._ws_stop_events.clear()

        if self._exchange is not None:
            await self._exchange.close()
            self._exchange = None
            logger.info("MarketDataClient closed")

    async def __aenter__(self) -> MarketDataClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # -- helpers -------------------------------------------------------------

    def _require_exchange(self) -> ccxt_async.binanceusdm:
        if self._exchange is None:
            raise RuntimeError("MarketDataClient not connected. Call connect() first.")
        return self._exchange

    @staticmethod
    def _to_decimal(value: Any) -> Decimal:
        """Convert a float/int/str to Decimal, raising on NaN/None."""
        if value is None:
            raise ValueError("Cannot convert None to Decimal")
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"Cannot convert {value!r} to Decimal") from exc

    @staticmethod
    def _utc_from_ms(ms: int | float | None) -> datetime:
        if ms is None:
            return datetime.now(tz=timezone.utc)
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)

    # -- public API ----------------------------------------------------------

    @_retry
    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "5m",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Fetch OHLCV candles.

        Returns a list of dicts with keys:
        ``timestamp``, ``open``, ``high``, ``low``, ``close``, ``volume``.
        All price fields are ``Decimal``.
        """
        exchange = self._require_exchange()
        raw: list[list[Any]] = await exchange.fetch_ohlcv(
            symbol, timeframe=timeframe, limit=limit
        )

        candles: list[dict[str, Any]] = []
        for row in raw:
            ts_ms, o, h, l, c, v = row[:6]
            candles.append(
                {
                    "timestamp": self._utc_from_ms(ts_ms),
                    "open": self._to_decimal(o),
                    "high": self._to_decimal(h),
                    "low": self._to_decimal(l),
                    "close": self._to_decimal(c),
                    "volume": self._to_decimal(v),
                }
            )
        logger.debug(
            "Fetched %d candles for %s (%s)", len(candles), symbol, timeframe
        )
        return candles

    @_retry
    async def fetch_ticker(self, symbol: str) -> TickerData:
        """Fetch the latest 24-h ticker for *symbol*."""
        exchange = self._require_exchange()
        raw: dict[str, Any] = await exchange.fetch_ticker(symbol)
        # Guard against None values: .get("key", 0) only uses the default
        # when the key is ABSENT.  Binance testnet sometimes returns
        # {"last": None, ...} — `or 0` handles both missing and None.
        return TickerData(
            symbol=symbol,
            bid=self._to_decimal(raw.get("bid") or 0),
            ask=self._to_decimal(raw.get("ask") or 0),
            last=self._to_decimal(raw.get("last") or 0),
            high=self._to_decimal(raw.get("high") or 0),
            low=self._to_decimal(raw.get("low") or 0),
            volume=self._to_decimal(raw.get("quoteVolume") or raw.get("baseVolume") or 0),
            timestamp=self._utc_from_ms(raw.get("timestamp")),
        )

    @_retry
    async def fetch_orderbook(
        self, symbol: str, limit: int = 20
    ) -> OrderBookData:
        """Fetch the current order book for *symbol*."""
        exchange = self._require_exchange()
        raw: dict[str, Any] = await exchange.fetch_order_book(symbol, limit=limit)

        bids = [
            OrderBookEntry(price=self._to_decimal(p), amount=self._to_decimal(a))
            for p, a in raw.get("bids", [])
        ]
        asks = [
            OrderBookEntry(price=self._to_decimal(p), amount=self._to_decimal(a))
            for p, a in raw.get("asks", [])
        ]
        return OrderBookData(
            symbol=symbol,
            bids=bids,
            asks=asks,
            timestamp=self._utc_from_ms(raw.get("timestamp")),
        )

    @_retry
    async def get_account_balance(self) -> Decimal:
        """Fetch the total USDT balance from the Binance Futures account.

        Returns the total USDT balance (available + in positions).
        """
        exchange = self._require_exchange()
        raw = await exchange.fetch_balance()
        # ccxt unified balance: raw['USDT']['total'] for Binance Futures
        usdt_info = raw.get("USDT", {})
        total = usdt_info.get("total", 0)
        balance = self._to_decimal(total)
        logger.info("Account balance: %s USDT", balance)
        return balance

    @_retry
    async def get_margin_balance(self) -> Decimal:
        """Fetch the total margin balance (equity) including unrealized PnL.

        For Binance USDM Futures, ``totalMarginBalance`` = wallet balance
        + unrealized PnL.  This reflects true account equity and should be
        used for circuit-breaker checks.

        Falls back to wallet balance if the raw field is unavailable.
        """
        exchange = self._require_exchange()
        raw = await exchange.fetch_balance()
        info = raw.get("info", {})
        margin_bal = info.get("totalMarginBalance")
        if margin_bal is not None:
            balance = self._to_decimal(margin_bal)
        else:
            # Fallback: use wallet balance
            usdt_info = raw.get("USDT", {})
            balance = self._to_decimal(usdt_info.get("total", 0))
            logger.warning(
                "totalMarginBalance not available; falling back to wallet balance"
            )
        logger.info("Margin balance (equity): %s USDT", balance)
        return balance

    @_retry
    async def get_all_assets(self) -> list[AssetBalance]:
        """Fetch ALL non-zero asset balances from the Futures account.

        Reads the ``assets`` array from the raw Binance response which
        includes every currency held (USDT, USDC, BTC, etc.) regardless
        of whether Multi-Asset Mode is enabled.

        Returns a list of :class:`AssetBalance` for every asset with a
        non-zero wallet balance or margin balance.
        """
        exchange = self._require_exchange()
        raw = await exchange.fetch_balance()
        info = raw.get("info", {})
        assets_raw: list[dict[str, Any]] = info.get("assets", [])

        assets: list[AssetBalance] = []
        for item in assets_raw:
            wallet = self._to_decimal(item.get("walletBalance", "0"))
            margin = self._to_decimal(item.get("marginBalance", "0"))
            if wallet == Decimal("0") and margin == Decimal("0"):
                continue
            assets.append(
                AssetBalance(
                    asset=item.get("asset", "UNKNOWN"),
                    wallet_balance=wallet,
                    unrealized_pnl=self._to_decimal(
                        item.get("unrealizedProfit", "0"),
                    ),
                    margin_balance=margin,
                    available_balance=self._to_decimal(
                        item.get("availableBalance", "0"),
                    ),
                )
            )

        for a in assets:
            logger.info(
                "Asset %s: wallet=%s  upnl=%s  margin=%s  available=%s",
                a.asset,
                a.wallet_balance,
                a.unrealized_pnl,
                a.margin_balance,
                a.available_balance,
            )
        return assets

    @_retry
    async def get_current_price(self, symbol: str) -> Decimal:
        """Return the last traded price for *symbol* as a Decimal."""
        ticker = await self.fetch_ticker(symbol)
        return ticker.last

    @_retry
    async def fetch_funding_rate(self, symbol: str) -> Decimal:
        """Fetch the current funding rate for *symbol*.

        Returns the funding rate as a Decimal (e.g. 0.0001 for 0.01%).
        Positive = longs pay shorts.  Negative = shorts pay longs.
        """
        exchange = self._require_exchange()
        raw = await exchange.fetch_funding_rate(symbol)
        rate = self._to_decimal(raw.get("fundingRate", 0))
        logger.info("Funding rate for %s: %s", symbol, rate)
        return rate

    # -- Sentiment / aggregate data (infrastructure-only, NOT for trading) ---

    @_retry
    async def fetch_top_position_ratio(
        self, symbol: str, period: str = "5m", limit: int = 30,
    ) -> list[dict[str, Any]]:
        """Fetch top trader long/short position ratio.

        Uses ``/fapi/v1/topLongShortPositionRatio``.
        Returns list of dicts with ``timestamp``, ``long_account``, ``short_account``,
        ``long_short_ratio``.
        """
        exchange = self._require_exchange()
        binance_symbol = symbol.replace("/", "").replace(":USDT", "")
        response = await exchange.fapiPublicGetTopLongShortPositionRatio({
            "symbol": binance_symbol,
            "period": period,
            "limit": limit,
        })
        result = []
        for entry in response:
            result.append({
                "timestamp": self._utc_from_ms(int(entry.get("timestamp", 0))),
                "long_account": float(entry.get("longAccount", 0)),
                "short_account": float(entry.get("shortAccount", 0)),
                "long_short_ratio": float(entry.get("longShortRatio", 0)),
            })
        return result

    @_retry
    async def fetch_taker_buy_sell_ratio(
        self, symbol: str, period: str = "5m", limit: int = 30,
    ) -> list[dict[str, Any]]:
        """Fetch taker buy/sell volume ratio.

        Uses ``/fapi/v1/takerlongshortRatio``.
        Returns list of dicts with ``timestamp``, ``buy_sell_ratio``,
        ``buy_vol``, ``sell_vol``.
        """
        exchange = self._require_exchange()
        binance_symbol = symbol.replace("/", "").replace(":USDT", "")
        response = await exchange.fapiPublicGetTakerlongshortRatio({
            "symbol": binance_symbol,
            "period": period,
            "limit": limit,
        })
        result = []
        for entry in response:
            result.append({
                "timestamp": self._utc_from_ms(int(entry.get("timestamp", 0))),
                "buy_sell_ratio": float(entry.get("buySellRatio", 0)),
                "buy_vol": float(entry.get("buyVol", 0)),
                "sell_vol": float(entry.get("sellVol", 0)),
            })
        return result

    # -- WebSocket -----------------------------------------------------------

    async def subscribe_ticker(
        self,
        symbol: str,
        callback: Callable[[TickerData], Coroutine[Any, Any, None] | None],
    ) -> None:
        """Subscribe to real-time mini-ticker updates via Binance WebSocket.

        *callback* is invoked with a ``TickerData`` for every incoming message.
        The callback can be either sync or async.

        To unsubscribe later, call ``unsubscribe_ticker(symbol)``.
        """
        if symbol in self._ws_connections:
            logger.warning("Already subscribed to %s ticker", symbol)
            return

        stop_event = asyncio.Event()
        self._ws_stop_events[symbol] = stop_event

        task = asyncio.create_task(
            self._ws_ticker_loop(symbol, callback, stop_event),
            name=f"ws-ticker-{symbol}",
        )
        self._ws_connections[symbol] = task
        logger.info("Subscribed to real-time ticker for %s", symbol)

    async def unsubscribe_ticker(self, symbol: str) -> None:
        """Stop the WebSocket ticker subscription for *symbol*."""
        stop_event = self._ws_stop_events.pop(symbol, None)
        if stop_event is not None:
            stop_event.set()
        task = self._ws_connections.pop(symbol, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("Unsubscribed from ticker for %s", symbol)

    async def _ws_ticker_loop(
        self,
        symbol: str,
        callback: Callable[[TickerData], Coroutine[Any, Any, None] | None],
        stop_event: asyncio.Event,
    ) -> None:
        """Internal loop that maintains a WebSocket connection and dispatches updates."""
        # Binance stream name: e.g. "btcusdt@miniTicker"
        stream_symbol = symbol.replace("/", "").replace(":", "").lower()
        stream_name = f"{stream_symbol}@miniTicker"
        ws_base = self.TESTNET_WS_URL if self._testnet else self.PRODUCTION_WS_URL
        url = f"{ws_base}/{stream_name}"

        while not stop_event.is_set():
            try:
                async with websockets.connect(url) as ws:  # type: ignore[arg-type]
                    logger.info("WebSocket connected for %s at %s", symbol, url)
                    while not stop_event.is_set():
                        try:
                            raw_msg = await asyncio.wait_for(ws.recv(), timeout=30.0)
                        except asyncio.TimeoutError:
                            # Send pong to keep alive
                            continue

                        data = json.loads(raw_msg)
                        try:
                            ticker = TickerData(
                                symbol=symbol,
                                bid=self._to_decimal(data.get("b", data.get("c", 0))),
                                ask=self._to_decimal(data.get("a", data.get("c", 0))),
                                last=self._to_decimal(data.get("c", 0)),
                                high=self._to_decimal(data.get("h", 0)),
                                low=self._to_decimal(data.get("l", 0)),
                                volume=self._to_decimal(data.get("q", data.get("v", 0))),
                                timestamp=self._utc_from_ms(data.get("E")),
                            )
                        except (ValueError, KeyError) as exc:
                            logger.warning("Failed to parse WS ticker message: %s", exc)
                            continue

                        try:
                            result = callback(ticker)
                            if asyncio.iscoroutine(result):
                                await result
                        except Exception:
                            logger.exception("Ticker callback raised for %s", symbol)

            except asyncio.CancelledError:
                logger.info("WebSocket task cancelled for %s", symbol)
                return
            except Exception:
                if stop_event.is_set():
                    return
                logger.exception(
                    "WebSocket error for %s. Reconnecting in 5s ...", symbol
                )
                await asyncio.sleep(5.0)

    # -- Kline (candle close) WebSocket --------------------------------------

    async def subscribe_kline_close(
        self,
        symbol: str,
        timeframe: str,
        callback: Callable[[dict[str, Any]], Coroutine[Any, Any, None] | None],
    ) -> None:
        """Subscribe to kline WebSocket and invoke *callback* on candle close.

        *callback* receives a dict with keys:
        ``symbol``, ``timeframe``, ``open``, ``high``, ``low``, ``close``,
        ``volume``, ``timestamp`` (datetime, UTC).

        Only fires when ``k.x == True`` (candle is closed / final).
        Automatically reconnects on disconnect (handles 24h WS limit).
        """
        key = f"{symbol}@kline_{timeframe}"
        if key in self._ws_connections:
            logger.warning("Already subscribed to %s", key)
            return

        stop_event = asyncio.Event()
        self._ws_stop_events[key] = stop_event

        task = asyncio.create_task(
            self._ws_kline_loop(symbol, timeframe, callback, stop_event),
            name=f"ws-kline-{symbol}-{timeframe}",
        )
        self._ws_connections[key] = task
        logger.info("Subscribed to kline close for %s %s", symbol, timeframe)

    async def unsubscribe_kline(self, symbol: str, timeframe: str) -> None:
        """Stop the kline WebSocket subscription."""
        key = f"{symbol}@kline_{timeframe}"
        stop_event = self._ws_stop_events.pop(key, None)
        if stop_event is not None:
            stop_event.set()
        task = self._ws_connections.pop(key, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("Unsubscribed from kline %s %s", symbol, timeframe)

    async def _ws_kline_loop(
        self,
        symbol: str,
        timeframe: str,
        callback: Callable[[dict[str, Any]], Coroutine[Any, Any, None] | None],
        stop_event: asyncio.Event,
    ) -> None:
        """Internal loop: maintain kline WS and dispatch on candle close."""
        # Binance stream: e.g. "ethusdt@kline_4h"
        # ETH/USDT:USDT → ethusdt (strip settlement part after ':')
        base_symbol = symbol.split(":")[0]  # ETH/USDT:USDT → ETH/USDT
        stream_symbol = base_symbol.replace("/", "").lower()  # ETH/USDT → ethusdt
        stream_name = f"{stream_symbol}@kline_{timeframe}"
        ws_base = self.TESTNET_WS_URL if self._testnet else self.PRODUCTION_WS_URL
        url = f"{ws_base}/{stream_name}"

        last_closed_ts: datetime | None = None
        reconnect_delay = 5.0

        while not stop_event.is_set():
            try:
                async with websockets.connect(url) as ws:  # type: ignore[arg-type]
                    logger.info("Kline WS connected for %s %s at %s", symbol, timeframe, url)
                    reconnect_delay = 5.0  # Reset on successful connect

                    # REST fallback: check if candle closes were missed during disconnect
                    if last_closed_ts is not None:
                        try:
                            recent = await self.fetch_ohlcv(symbol, timeframe, limit=10)
                            if recent and len(recent) >= 2:
                                # Iterate all fully closed candles since last_closed_ts
                                # (exclude last row — it's the currently-forming candle)
                                for row in recent[:-1]:
                                    closed_ts = row["timestamp"]
                                    if closed_ts > last_closed_ts:
                                        logger.warning(
                                            "Missed %s close during WS disconnect for %s: "
                                            "last_seen=%s, firing for %s",
                                            timeframe, symbol, last_closed_ts, closed_ts,
                                        )
                                        candle = {
                                            "symbol": symbol,
                                            "timeframe": timeframe,
                                            "open": float(row["open"]),
                                            "high": float(row["high"]),
                                            "low": float(row["low"]),
                                            "close": float(row["close"]),
                                            "volume": float(row["volume"]),
                                            "timestamp": closed_ts,
                                        }
                                        try:
                                            result = callback(candle)
                                            if asyncio.iscoroutine(result):
                                                await result
                                        except Exception:
                                            logger.exception(
                                                "Missed-candle callback raised for %s %s",
                                                symbol, timeframe,
                                            )
                                        last_closed_ts = closed_ts
                        except Exception:
                            logger.exception(
                                "REST fallback candle check failed for %s %s",
                                symbol, timeframe,
                            )

                    while not stop_event.is_set():
                        try:
                            raw_msg = await asyncio.wait_for(ws.recv(), timeout=30.0)
                        except asyncio.TimeoutError:
                            continue

                        data = json.loads(raw_msg)
                        kline = data.get("k", {})

                        # Only fire on candle close (k.x == True)
                        if not kline.get("x", False):
                            continue

                        try:
                            closed_ts = self._utc_from_ms(kline.get("T"))
                            candle = {
                                "symbol": symbol,
                                "timeframe": timeframe,
                                "open": float(kline.get("o", 0)),
                                "high": float(kline.get("h", 0)),
                                "low": float(kline.get("l", 0)),
                                "close": float(kline.get("c", 0)),
                                "volume": float(kline.get("v", 0)),
                                "timestamp": closed_ts,
                            }
                            last_closed_ts = closed_ts
                        except (ValueError, KeyError) as exc:
                            logger.warning("Failed to parse kline message: %s", exc)
                            continue

                        logger.info(
                            "4H candle closed: %s %s close=%.4f",
                            symbol, timeframe, candle["close"],
                        )

                        try:
                            result = callback(candle)
                            if asyncio.iscoroutine(result):
                                await result
                        except Exception:
                            logger.exception("Kline callback raised for %s %s", symbol, timeframe)

            except asyncio.CancelledError:
                logger.info("Kline WS task cancelled for %s %s", symbol, timeframe)
                return
            except Exception:
                if stop_event.is_set():
                    return
                logger.exception(
                    "Kline WS error for %s %s. Reconnecting in %.0fs ...",
                    symbol, timeframe, reconnect_delay,
                )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60.0)  # Exponential backoff, cap 60s
