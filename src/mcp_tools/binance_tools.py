"""
MCP server exposing Binance Futures operations as tools.

Provides account, market data, order management, and position tools
via the Model Context Protocol. All monetary values use Decimal for
precision and timestamps are always UTC.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import ccxt.async_support as ccxt_async
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
from pydantic import BaseModel, Field

logger = logging.getLogger("claude_quant.mcp_tools.binance")

# ---------------------------------------------------------------------------
# Pydantic schemas for tool inputs / outputs
# ---------------------------------------------------------------------------


class OHLCVInput(BaseModel):
    """Input schema for get_ohlcv."""

    symbol: str = Field(description="Trading pair, e.g. 'BTC/USDT:USDT'")
    timeframe: str = Field(default="5m", description="Candle timeframe, e.g. '1m', '5m', '1h'")
    limit: int = Field(default=200, ge=1, le=1500, description="Number of candles to fetch")


class TickerInput(BaseModel):
    """Input schema for get_ticker."""

    symbol: str = Field(description="Trading pair, e.g. 'BTC/USDT:USDT'")


class OrderBookInput(BaseModel):
    """Input schema for get_orderbook."""

    symbol: str = Field(description="Trading pair, e.g. 'BTC/USDT:USDT'")
    limit: int = Field(default=20, ge=1, le=100, description="Number of levels per side")


class PlaceOrderInput(BaseModel):
    """Input schema for place_order."""

    symbol: str = Field(description="Trading pair, e.g. 'BTC/USDT:USDT'")
    side: str = Field(description="'buy' or 'sell'")
    type: str = Field(description="Order type: 'market', 'limit', 'stop_market', 'take_profit_market'")
    amount: float = Field(gt=0, description="Order quantity in base currency")
    price: float | None = Field(default=None, description="Limit price (required for limit orders)")
    stop_price: float | None = Field(
        default=None, description="Stop/trigger price (for stop_market / take_profit_market)"
    )


class CancelOrderInput(BaseModel):
    """Input schema for cancel_order."""

    symbol: str = Field(description="Trading pair, e.g. 'BTC/USDT:USDT'")
    order_id: str = Field(description="Exchange order ID to cancel")


class SetLeverageInput(BaseModel):
    """Input schema for set_leverage."""

    symbol: str = Field(description="Trading pair, e.g. 'BTC/USDT:USDT'")
    leverage: int = Field(ge=1, le=125, description="Leverage multiplier")


class TradeHistoryInput(BaseModel):
    """Input schema for get_trade_history."""

    symbol: str = Field(description="Trading pair, e.g. 'BTC/USDT:USDT'")
    limit: int = Field(default=50, ge=1, le=1000, description="Number of recent fills to return")


# ---------------------------------------------------------------------------
# Exchange client singleton
# ---------------------------------------------------------------------------

_exchange: ccxt_async.binanceusdm | None = None


async def _get_exchange() -> ccxt_async.binanceusdm:
    """Lazily initialise and return the shared ccxt exchange instance."""
    global _exchange
    if _exchange is not None:
        return _exchange

    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    testnet = os.getenv("BINANCE_TESTNET", "false").lower() == "true"

    _exchange = ccxt_async.binanceusdm(
        {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": "future",
                "adjustForTimeDifference": True,
            },
        }
    )

    if testnet:
        # ccxt >= 4.5.6: use enable_demo_trading instead of set_sandbox_mode (ccxt #26487)
        _exchange.enable_demo_trading(True)
        logger.info("Binance MCP tools connected to TESTNET")
    else:
        logger.info("Binance MCP tools connected to PRODUCTION")

    await _exchange.load_markets()
    return _exchange


async def _close_exchange() -> None:
    """Gracefully shut down the exchange connection."""
    global _exchange
    if _exchange is not None:
        await _exchange.close()
        _exchange = None
        logger.info("Binance exchange connection closed")


def _to_decimal(value: Any) -> Decimal:
    """Safe conversion to Decimal."""
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialize_decimal(obj: Any) -> Any:
    """Recursively convert Decimal values to strings for JSON serialization."""
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _serialize_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize_decimal(item) for item in obj]
    return obj


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

_TOOL_DEFINITIONS: list[Tool] = [
    Tool(
        name="get_account_balance",
        description=(
            "Fetch the current Binance Futures account balance. Returns total balance, "
            "available balance, unrealised PnL, and margin details in USDT."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_ohlcv",
        description=(
            "Fetch OHLCV (candlestick) data for a symbol. Returns a list of candles "
            "with timestamp, open, high, low, close, volume."
        ),
        inputSchema=OHLCVInput.model_json_schema(),
    ),
    Tool(
        name="get_ticker",
        description=(
            "Fetch the current ticker for a symbol including last price, bid, ask, "
            "24h high/low, and 24h volume."
        ),
        inputSchema=TickerInput.model_json_schema(),
    ),
    Tool(
        name="get_orderbook",
        description="Fetch the order book (bids and asks) for a symbol.",
        inputSchema=OrderBookInput.model_json_schema(),
    ),
    Tool(
        name="get_open_positions",
        description=(
            "Fetch all currently open futures positions with entry price, size, "
            "unrealised PnL, leverage, and liquidation price."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="place_order",
        description=(
            "Place a futures order. Supports market, limit, stop_market, and "
            "take_profit_market types. Returns the order result with order ID."
        ),
        inputSchema=PlaceOrderInput.model_json_schema(),
    ),
    Tool(
        name="cancel_order",
        description="Cancel an open order by its exchange order ID.",
        inputSchema=CancelOrderInput.model_json_schema(),
    ),
    Tool(
        name="set_leverage",
        description="Set the leverage multiplier for a trading pair.",
        inputSchema=SetLeverageInput.model_json_schema(),
    ),
    Tool(
        name="get_trade_history",
        description="Fetch recent fills (executed trades) for a symbol.",
        inputSchema=TradeHistoryInput.model_json_schema(),
    ),
]


def create_binance_server() -> Server:
    """Create and configure the Binance MCP server."""
    server = Server("binance-tools")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return _TOOL_DEFINITIONS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        import json as _json

        try:
            result = await _dispatch_tool(name, arguments)
            return [TextContent(type="text", text=_json.dumps(result, default=str))]
        except Exception as exc:
            logger.exception("Tool %s failed", name)
            error_msg = f"Error in {name}: {type(exc).__name__}: {exc}"
            return [TextContent(type="text", text=error_msg)]

    return server


# ---------------------------------------------------------------------------
# Tool dispatch and implementations
# ---------------------------------------------------------------------------


async def _dispatch_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Route tool calls to their implementations."""
    handlers = {
        "get_account_balance": _handle_get_account_balance,
        "get_ohlcv": _handle_get_ohlcv,
        "get_ticker": _handle_get_ticker,
        "get_orderbook": _handle_get_orderbook,
        "get_open_positions": _handle_get_open_positions,
        "place_order": _handle_place_order,
        "cancel_order": _handle_cancel_order,
        "set_leverage": _handle_set_leverage,
        "get_trade_history": _handle_get_trade_history,
    }
    handler = handlers.get(name)
    if handler is None:
        raise ValueError(f"Unknown tool: {name}")
    return await handler(arguments)


async def _handle_get_account_balance(arguments: dict[str, Any]) -> dict[str, Any]:
    """Fetch account balance from Binance Futures."""
    exchange = await _get_exchange()
    balance_raw = await exchange.fetch_balance()

    usdt_info = balance_raw.get("USDT", {})
    total = _to_decimal(usdt_info.get("total", 0))
    free = _to_decimal(usdt_info.get("free", 0))
    used = _to_decimal(usdt_info.get("used", 0))

    # Futures-specific info from the raw response
    info = balance_raw.get("info", {})
    assets = info.get("assets", [])
    usdt_asset = next((a for a in assets if a.get("asset") == "USDT"), {}) if assets else {}

    unrealised_pnl = _to_decimal(usdt_asset.get("unrealizedProfit", 0))
    margin_balance = _to_decimal(usdt_asset.get("marginBalance", total))
    wallet_balance = _to_decimal(usdt_asset.get("walletBalance", total))

    result = {
        "total_balance": str(total),
        "available_balance": str(free),
        "used_margin": str(used),
        "unrealised_pnl": str(unrealised_pnl),
        "margin_balance": str(margin_balance),
        "wallet_balance": str(wallet_balance),
        "currency": "USDT",
        "timestamp": _utc_now_iso(),
    }
    logger.info("Account balance fetched: total=%s, available=%s", total, free)
    return result


async def _handle_get_ohlcv(arguments: dict[str, Any]) -> dict[str, Any]:
    """Fetch OHLCV candlestick data."""
    params = OHLCVInput(**arguments)
    exchange = await _get_exchange()

    raw_candles = await exchange.fetch_ohlcv(
        params.symbol, timeframe=params.timeframe, limit=params.limit
    )

    candles = []
    for row in raw_candles:
        ts_ms, o, h, l, c, v = row[:6]
        candles.append(
            {
                "timestamp": datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).isoformat(),
                "open": str(_to_decimal(o)),
                "high": str(_to_decimal(h)),
                "low": str(_to_decimal(l)),
                "close": str(_to_decimal(c)),
                "volume": str(_to_decimal(v)),
            }
        )

    result = {
        "symbol": params.symbol,
        "timeframe": params.timeframe,
        "count": len(candles),
        "candles": candles,
    }
    logger.info("Fetched %d candles for %s (%s)", len(candles), params.symbol, params.timeframe)
    return result


async def _handle_get_ticker(arguments: dict[str, Any]) -> dict[str, Any]:
    """Fetch current ticker data."""
    params = TickerInput(**arguments)
    exchange = await _get_exchange()

    raw = await exchange.fetch_ticker(params.symbol)

    result = {
        "symbol": params.symbol,
        "last": str(_to_decimal(raw.get("last", 0))),
        "bid": str(_to_decimal(raw.get("bid", 0))),
        "ask": str(_to_decimal(raw.get("ask", 0))),
        "high_24h": str(_to_decimal(raw.get("high", 0))),
        "low_24h": str(_to_decimal(raw.get("low", 0))),
        "volume_24h": str(_to_decimal(raw.get("quoteVolume", raw.get("baseVolume", 0)))),
        "change_pct_24h": str(_to_decimal(raw.get("percentage", 0))),
        "vwap": str(_to_decimal(raw.get("vwap", 0))),
        "timestamp": _utc_now_iso(),
    }
    logger.info("Ticker for %s: last=%s", params.symbol, result["last"])
    return result


async def _handle_get_orderbook(arguments: dict[str, Any]) -> dict[str, Any]:
    """Fetch order book data."""
    params = OrderBookInput(**arguments)
    exchange = await _get_exchange()

    raw = await exchange.fetch_order_book(params.symbol, limit=params.limit)

    bids = [
        {"price": str(_to_decimal(p)), "amount": str(_to_decimal(a))}
        for p, a in raw.get("bids", [])
    ]
    asks = [
        {"price": str(_to_decimal(p)), "amount": str(_to_decimal(a))}
        for p, a in raw.get("asks", [])
    ]

    result = {
        "symbol": params.symbol,
        "bids": bids,
        "asks": asks,
        "bid_count": len(bids),
        "ask_count": len(asks),
        "spread": str(_to_decimal(asks[0]["price"]) - _to_decimal(bids[0]["price"]))
        if bids and asks
        else "0",
        "timestamp": _utc_now_iso(),
    }
    logger.info("Orderbook for %s: %d bids, %d asks", params.symbol, len(bids), len(asks))
    return result


async def _handle_get_open_positions(arguments: dict[str, Any]) -> dict[str, Any]:
    """Fetch all open futures positions."""
    exchange = await _get_exchange()

    positions_raw = await exchange.fetch_positions()

    positions = []
    for pos in positions_raw:
        contracts = float(pos.get("contracts", 0) or 0)
        if contracts == 0:
            continue  # Skip empty positions

        positions.append(
            {
                "symbol": pos.get("symbol", ""),
                "side": pos.get("side", ""),
                "contracts": str(_to_decimal(pos.get("contracts", 0))),
                "notional": str(_to_decimal(pos.get("notional", 0))),
                "entry_price": str(_to_decimal(pos.get("entryPrice", 0))),
                "mark_price": str(_to_decimal(pos.get("markPrice", 0))),
                "unrealised_pnl": str(_to_decimal(pos.get("unrealizedPnl", 0))),
                "leverage": str(pos.get("leverage", 1)),
                "margin_mode": pos.get("marginMode", ""),
                "liquidation_price": str(_to_decimal(pos.get("liquidationPrice", 0))),
                "percentage": str(_to_decimal(pos.get("percentage", 0))),
            }
        )

    result = {
        "positions": positions,
        "count": len(positions),
        "timestamp": _utc_now_iso(),
    }
    logger.info("Open positions: %d active", len(positions))
    return result


async def _handle_place_order(arguments: dict[str, Any]) -> dict[str, Any]:
    """Place a futures order on Binance."""
    params = PlaceOrderInput(**arguments)
    exchange = await _get_exchange()

    # Validate side
    side = params.side.lower()
    if side not in ("buy", "sell"):
        raise ValueError(f"Invalid side '{params.side}'. Must be 'buy' or 'sell'.")

    # Validate order type
    order_type = params.type.lower()
    valid_types = ("market", "limit", "stop_market", "take_profit_market")
    if order_type not in valid_types:
        raise ValueError(f"Invalid order type '{params.type}'. Must be one of {valid_types}.")

    # Build extra params for stop/TP orders
    extra_params: dict[str, Any] = {}
    if order_type in ("stop_market", "take_profit_market"):
        if params.stop_price is None:
            raise ValueError(f"stop_price is required for {order_type} orders.")
        extra_params["stopPrice"] = params.stop_price

    # Place the order
    logger.info(
        "Placing %s %s order: %s %.6f @ %s (stop=%s)",
        side,
        order_type,
        params.symbol,
        params.amount,
        params.price,
        params.stop_price,
    )

    order = await exchange.create_order(
        symbol=params.symbol,
        type=order_type,
        side=side,
        amount=params.amount,
        price=params.price,
        params=extra_params,
    )

    result = {
        "order_id": str(order.get("id", "")),
        "client_order_id": str(order.get("clientOrderId", "")),
        "symbol": order.get("symbol", params.symbol),
        "side": order.get("side", side),
        "type": order.get("type", order_type),
        "amount": str(_to_decimal(order.get("amount", params.amount))),
        "price": str(_to_decimal(order.get("price", params.price or 0))),
        "cost": str(_to_decimal(order.get("cost", 0))),
        "status": order.get("status", "unknown"),
        "filled": str(_to_decimal(order.get("filled", 0))),
        "remaining": str(_to_decimal(order.get("remaining", 0))),
        "average": str(_to_decimal(order.get("average", 0))),
        "timestamp": _utc_now_iso(),
    }
    logger.info("Order placed: id=%s status=%s", result["order_id"], result["status"])
    return result


async def _handle_cancel_order(arguments: dict[str, Any]) -> dict[str, Any]:
    """Cancel an open order."""
    params = CancelOrderInput(**arguments)
    exchange = await _get_exchange()

    logger.info("Cancelling order %s on %s", params.order_id, params.symbol)

    cancelled = await exchange.cancel_order(params.order_id, params.symbol)

    result = {
        "order_id": str(cancelled.get("id", params.order_id)),
        "symbol": cancelled.get("symbol", params.symbol),
        "status": cancelled.get("status", "cancelled"),
        "timestamp": _utc_now_iso(),
    }
    logger.info("Order %s cancelled successfully", params.order_id)
    return result


async def _handle_set_leverage(arguments: dict[str, Any]) -> dict[str, Any]:
    """Set leverage for a trading pair."""
    params = SetLeverageInput(**arguments)
    exchange = await _get_exchange()

    logger.info("Setting leverage for %s to %dx", params.symbol, params.leverage)

    response = await exchange.set_leverage(params.leverage, params.symbol)

    result = {
        "symbol": params.symbol,
        "leverage": params.leverage,
        "success": True,
        "raw_response": str(response) if response else "OK",
        "timestamp": _utc_now_iso(),
    }
    logger.info("Leverage set: %s = %dx", params.symbol, params.leverage)
    return result


async def _handle_get_trade_history(arguments: dict[str, Any]) -> dict[str, Any]:
    """Fetch recent trade fills for a symbol."""
    params = TradeHistoryInput(**arguments)
    exchange = await _get_exchange()

    raw_trades = await exchange.fetch_my_trades(params.symbol, limit=params.limit)

    trades = []
    for t in raw_trades:
        trades.append(
            {
                "id": str(t.get("id", "")),
                "order_id": str(t.get("order", "")),
                "symbol": t.get("symbol", params.symbol),
                "side": t.get("side", ""),
                "price": str(_to_decimal(t.get("price", 0))),
                "amount": str(_to_decimal(t.get("amount", 0))),
                "cost": str(_to_decimal(t.get("cost", 0))),
                "fee_cost": str(_to_decimal((t.get("fee") or {}).get("cost", 0))),
                "fee_currency": (t.get("fee") or {}).get("currency", "USDT"),
                "timestamp": datetime.fromtimestamp(
                    (t.get("timestamp", 0) or 0) / 1000.0, tz=timezone.utc
                ).isoformat(),
                "maker": t.get("takerOrMaker", "") == "maker",
            }
        )

    result = {
        "symbol": params.symbol,
        "trades": trades,
        "count": len(trades),
        "timestamp": _utc_now_iso(),
    }
    logger.info("Trade history for %s: %d fills", params.symbol, len(trades))
    return result


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    """Run the Binance MCP server over stdio."""
    server = create_binance_server()
    logger.info("Starting Binance MCP server...")
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        await _close_exchange()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    asyncio.run(main())
