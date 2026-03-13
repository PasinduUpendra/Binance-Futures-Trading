"""
Position tracker for Binance Futures via ccxt.

Fetches real-time position data from the exchange API. All monetary
values use Decimal for precision.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import ccxt.async_support as ccxt_async
from pydantic import BaseModel, Field

logger = logging.getLogger("claude_quant.execution.position_tracker")

# ---------------------------------------------------------------------------
# Retry decorator
# ---------------------------------------------------------------------------

_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0


def _retry(func: Any) -> Any:
    """Retry an async method up to _MAX_RETRIES with exponential backoff."""

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
                logger.error("%s raised ExchangeError: %s", func.__qualname__, exc)
                raise
        raise RuntimeError(
            f"{func.__qualname__} failed after {_MAX_RETRIES} attempts"
        ) from last_exc

    wrapper.__qualname__ = func.__qualname__
    wrapper.__name__ = func.__name__
    return wrapper


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class Position(BaseModel):
    """Represents an open position on Binance Futures."""

    symbol: str
    side: str = Field(description="'long' or 'short'")
    size: Decimal = Field(description="Position size in base currency (always positive)")
    entry_price: Decimal
    current_price: Decimal
    unrealized_pnl: Decimal
    leverage: int
    liquidation_price: Decimal | None = None
    margin_type: str = Field(default="cross", description="'cross' or 'isolated'")
    notional_value: Decimal = Decimal("0")
    margin: Decimal = Decimal("0")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_decimal(value: Any) -> Decimal:
    """Safe conversion to Decimal, returning 0 for None."""
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


# ---------------------------------------------------------------------------
# PositionTracker
# ---------------------------------------------------------------------------


class PositionTracker:
    """Tracks open positions on Binance Futures via the exchange API.

    Parameters
    ----------
    api_key : str | None
        Binance API key. Defaults to ``BINANCE_API_KEY`` env var.
    api_secret : str | None
        Binance API secret. Defaults to ``BINANCE_API_SECRET`` env var.
    testnet : bool | None
        Use Binance Futures testnet. Defaults to ``BINANCE_TESTNET`` env var.
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        testnet: bool | None = None,
    ) -> None:
        self._api_key = api_key or os.getenv("BINANCE_API_KEY", "")
        self._api_secret = api_secret or os.getenv("BINANCE_API_SECRET", "")

        if testnet is None:
            testnet = os.getenv("BINANCE_TESTNET", "false").lower() == "true"
        self._testnet = testnet

        self._exchange: ccxt_async.binanceusdm | None = None

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
            self._exchange.set_sandbox_mode(True)
            logger.info("PositionTracker connected to Binance Futures TESTNET")
        else:
            logger.info("PositionTracker connected to Binance Futures PRODUCTION")

        await self._exchange.load_markets()

    async def close(self) -> None:
        """Shut down the exchange connection."""
        if self._exchange is not None:
            await self._exchange.close()
            self._exchange = None
            logger.info("PositionTracker closed")

    async def __aenter__(self) -> PositionTracker:
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    def _require_exchange(self) -> ccxt_async.binanceusdm:
        if self._exchange is None:
            raise RuntimeError(
                "PositionTracker not connected. Call connect() first."
            )
        return self._exchange

    # -- position parsing ----------------------------------------------------

    def _parse_position(self, raw: dict[str, Any]) -> Position | None:
        """Parse a ccxt position dict into a Position model.

        Returns None if the position has zero size (not actually open).
        """
        contracts = _to_decimal(raw.get("contracts", 0))
        if contracts == 0:
            return None

        side_raw = raw.get("side", "").lower()
        if side_raw not in ("long", "short"):
            # ccxt sometimes uses 'buy'/'sell' — normalize
            if side_raw == "buy":
                side_raw = "long"
            elif side_raw == "sell":
                side_raw = "short"
            else:
                logger.warning(
                    "Unknown position side '%s' for %s",
                    side_raw,
                    raw.get("symbol"),
                )
                return None

        entry_price = _to_decimal(raw.get("entryPrice", 0))
        mark_price = _to_decimal(raw.get("markPrice", 0))
        unrealized_pnl = _to_decimal(raw.get("unrealizedPnl", 0))
        liquidation_price_raw = raw.get("liquidationPrice")
        liquidation_price = (
            _to_decimal(liquidation_price_raw)
            if liquidation_price_raw and float(liquidation_price_raw) > 0
            else None
        )
        leverage_raw = raw.get("leverage", 1)
        leverage = int(float(leverage_raw)) if leverage_raw else 1
        notional = _to_decimal(raw.get("notional", 0))
        # notional may be negative for shorts; use absolute value
        if notional < 0:
            notional = abs(notional)
        margin_type = raw.get("marginMode", raw.get("marginType", "cross"))
        if margin_type is None:
            margin_type = "cross"
        collateral = _to_decimal(raw.get("collateral", raw.get("initialMargin", 0)))

        return Position(
            symbol=raw.get("symbol", ""),
            side=side_raw,
            size=abs(contracts),
            entry_price=entry_price,
            current_price=mark_price,
            unrealized_pnl=unrealized_pnl,
            leverage=leverage,
            liquidation_price=liquidation_price,
            margin_type=str(margin_type).lower(),
            notional_value=notional,
            margin=collateral,
            timestamp=datetime.now(tz=timezone.utc),
        )

    # -- public API ----------------------------------------------------------

    @_retry
    async def get_open_positions(self) -> list[Position]:
        """Fetch all open positions from Binance Futures.

        Returns
        -------
        list[Position]
            Only positions with non-zero size are returned.
        """
        exchange = self._require_exchange()
        logger.info("GET_OPEN_POSITIONS fetching from exchange")

        raw_positions = await exchange.fetch_positions()

        positions: list[Position] = []
        for raw in raw_positions:
            pos = self._parse_position(raw)
            if pos is not None:
                positions.append(pos)

        logger.info(
            "GET_OPEN_POSITIONS_OK count=%d symbols=[%s]",
            len(positions),
            ", ".join(p.symbol for p in positions),
        )
        return positions

    @_retry
    async def get_position(self, symbol: str) -> Position | None:
        """Fetch the current position for a specific symbol.

        Parameters
        ----------
        symbol : str
            Trading pair, e.g. ``"BTC/USDT:USDT"``.

        Returns
        -------
        Position | None
            The position if one is open, otherwise None.
        """
        exchange = self._require_exchange()
        logger.info("GET_POSITION symbol=%s", symbol)

        raw_positions = await exchange.fetch_positions([symbol])

        for raw in raw_positions:
            pos = self._parse_position(raw)
            if pos is not None and pos.symbol == symbol:
                logger.info(
                    "GET_POSITION_OK symbol=%s side=%s size=%s "
                    "entry=%s pnl=%s leverage=%dx",
                    pos.symbol,
                    pos.side,
                    pos.size,
                    pos.entry_price,
                    pos.unrealized_pnl,
                    pos.leverage,
                )
                return pos

        logger.info("GET_POSITION_OK symbol=%s — no open position", symbol)
        return None

    async def get_unrealized_pnl(self) -> Decimal:
        """Get total unrealized PnL across all open positions.

        Returns
        -------
        Decimal
            Sum of unrealized PnL from all positions.
        """
        positions = await self.get_open_positions()
        total_pnl = sum(
            (p.unrealized_pnl for p in positions), Decimal("0")
        )
        logger.info("GET_UNREALIZED_PNL total=%s", total_pnl)
        return total_pnl

    async def get_total_exposure(self) -> Decimal:
        """Get total notional exposure across all open positions.

        Returns
        -------
        Decimal
            Sum of absolute notional values across all positions.
        """
        positions = await self.get_open_positions()
        total_exposure = sum(
            (p.notional_value for p in positions), Decimal("0")
        )
        logger.info("GET_TOTAL_EXPOSURE total=%s", total_exposure)
        return total_exposure
