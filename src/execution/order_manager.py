"""
Order manager for Binance Futures via ccxt.

Handles order placement, verification, cancellation, and status queries.
Every order placement is verified with a separate GET call (anti-hallucination).
All monetary values use Decimal for precision.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

import ccxt.async_support as ccxt_async
from pydantic import BaseModel, Field

logger = logging.getLogger("claude_quant.execution.order_manager")

# ---------------------------------------------------------------------------
# Retry decorator (mirrors market_data pattern)
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
                base_wait = _BACKOFF_BASE * (2 ** (attempt - 1))
                wait = base_wait + random.uniform(0, base_wait * 0.5)
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


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP_MARKET = "stop_market"
    STOP_LIMIT = "stop_limit"


class OrderState(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class OrderResult(BaseModel):
    """Result returned after placing an order."""

    order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    order_type: str
    amount: Decimal
    price: Decimal | None = None
    stop_price: Decimal | None = None
    status: str
    filled: Decimal = Decimal("0")
    average_fill_price: Decimal | None = None
    cost: Decimal = Decimal("0")
    fee: Decimal = Decimal("0")
    fee_currency: str | None = None
    timestamp: datetime
    verified: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)


class OrderStatus(BaseModel):
    """Current status of an order fetched from the exchange."""

    order_id: str
    symbol: str
    side: OrderSide
    order_type: str
    amount: Decimal
    filled: Decimal
    remaining: Decimal
    price: Decimal | None = None
    average_fill_price: Decimal | None = None
    stop_price: Decimal | None = None
    status: OrderState
    cost: Decimal = Decimal("0")
    fee: Decimal = Decimal("0")
    fee_currency: str | None = None
    timestamp: datetime
    last_trade_timestamp: datetime | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_decimal(value: Any) -> Decimal:
    """Safe conversion to Decimal."""
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _utc_from_ms(ms: int | float | None) -> datetime:
    if ms is None:
        return datetime.now(tz=timezone.utc)
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def _parse_order_state(raw_status: str | None) -> OrderState:
    """Map exchange status strings to our OrderState enum."""
    mapping: dict[str, OrderState] = {
        "open": OrderState.OPEN,
        "closed": OrderState.CLOSED,
        "canceled": OrderState.CANCELED,
        "cancelled": OrderState.CANCELED,
        "expired": OrderState.EXPIRED,
        "rejected": OrderState.REJECTED,
    }
    if raw_status is None:
        return OrderState.UNKNOWN
    return mapping.get(raw_status.lower(), OrderState.UNKNOWN)


def _generate_client_order_id() -> str:
    """Generate a unique client order ID prefixed for our system."""
    return f"cq_{uuid.uuid4().hex[:16]}"


# ---------------------------------------------------------------------------
# OrderManager
# ---------------------------------------------------------------------------


class OrderManager:
    """Manages order placement and lifecycle on Binance Futures via ccxt.

    Parameters
    ----------
    api_key : str | None
        Binance API key. Defaults to ``BINANCE_API_KEY`` env var.
    api_secret : str | None
        Binance API secret. Defaults to ``BINANCE_API_SECRET`` env var.
    testnet : bool | None
        Use Binance Futures testnet. Defaults to ``BINANCE_TESTNET`` env var.
    verify_orders : bool
        If True (default), every order placement is verified with a separate
        GET call to confirm it exists on the exchange. This is an
        anti-hallucination measure.
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        testnet: bool | None = None,
        verify_orders: bool = True,
    ) -> None:
        self._api_key = api_key or os.getenv("BINANCE_API_KEY", "")
        self._api_secret = api_secret or os.getenv("BINANCE_API_SECRET", "")

        if testnet is None:
            testnet = os.getenv("BINANCE_TESTNET", "false").lower() == "true"
        self._testnet = testnet
        self._verify_orders = verify_orders

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
            # ccxt >= 4.5.6: enable_demo_trading routes to demo-fapi.binance.com
            # The old set_sandbox_mode(True) routes to deprecated endpoints and
            # causes auth failures (see ccxt issues #26487, #27560).
            self._exchange.enable_demo_trading(True)
            logger.info("OrderManager connected to Binance Futures TESTNET (demo)")
        else:
            logger.info("OrderManager connected to Binance Futures PRODUCTION")

        await self._exchange.load_markets()

    async def close(self) -> None:
        """Shut down the exchange connection."""
        if self._exchange is not None:
            await self._exchange.close()
            self._exchange = None
            logger.info("OrderManager closed")

    async def __aenter__(self) -> OrderManager:
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    def _require_exchange(self) -> ccxt_async.binanceusdm:
        if self._exchange is None:
            raise RuntimeError("OrderManager not connected. Call connect() first.")
        return self._exchange

    # -- order verification (anti-hallucination) ----------------------------

    async def _verify_order_exists(
        self, symbol: str, order_id: str
    ) -> OrderStatus:
        """Fetch order from exchange to confirm it actually exists.

        This is a critical anti-hallucination measure: never trust the
        response from a create-order call alone. Always verify with a
        separate GET request.
        """
        exchange = self._require_exchange()
        logger.info(
            "VERIFY order_id=%s symbol=%s — fetching from exchange",
            order_id,
            symbol,
        )
        raw = await exchange.fetch_order(order_id, symbol)
        status = self._parse_order_status(raw)
        logger.info(
            "VERIFIED order_id=%s status=%s filled=%s/%s",
            order_id,
            status.status.value,
            status.filled,
            status.amount,
        )
        return status

    # -- parsing helpers -----------------------------------------------------

    def _parse_order_result(
        self,
        raw: dict[str, Any],
        client_order_id: str,
        verified: bool = False,
    ) -> OrderResult:
        """Parse a ccxt order response into an OrderResult model."""
        fee_info = raw.get("fee") or {}
        return OrderResult(
            order_id=str(raw.get("id", "")),
            client_order_id=client_order_id,
            symbol=raw.get("symbol", ""),
            side=OrderSide(raw.get("side", "buy").lower()),
            order_type=raw.get("type", "unknown"),
            amount=_to_decimal(raw.get("amount")),
            price=_to_decimal(raw.get("price")) if raw.get("price") else None,
            stop_price=_to_decimal(raw.get("stopPrice")) if raw.get("stopPrice") else None,
            status=raw.get("status", "unknown"),
            filled=_to_decimal(raw.get("filled")),
            average_fill_price=(
                _to_decimal(raw.get("average")) if raw.get("average") else None
            ),
            cost=_to_decimal(raw.get("cost")),
            fee=_to_decimal(fee_info.get("cost", 0)),
            fee_currency=fee_info.get("currency"),
            timestamp=_utc_from_ms(raw.get("timestamp")),
            verified=verified,
            raw=raw,
        )

    def _parse_order_status(self, raw: dict[str, Any]) -> OrderStatus:
        """Parse a ccxt order response into an OrderStatus model."""
        fee_info = raw.get("fee") or {}
        return OrderStatus(
            order_id=str(raw.get("id", "")),
            symbol=raw.get("symbol", ""),
            side=OrderSide(raw.get("side", "buy").lower()),
            order_type=raw.get("type", "unknown"),
            amount=_to_decimal(raw.get("amount")),
            filled=_to_decimal(raw.get("filled")),
            remaining=_to_decimal(raw.get("remaining")),
            price=_to_decimal(raw.get("price")) if raw.get("price") else None,
            average_fill_price=(
                _to_decimal(raw.get("average")) if raw.get("average") else None
            ),
            stop_price=_to_decimal(raw.get("stopPrice")) if raw.get("stopPrice") else None,
            status=_parse_order_state(raw.get("status")),
            cost=_to_decimal(raw.get("cost")),
            fee=_to_decimal(fee_info.get("cost", 0)),
            fee_currency=fee_info.get("currency"),
            timestamp=_utc_from_ms(raw.get("timestamp")),
            last_trade_timestamp=(
                _utc_from_ms(raw.get("lastTradeTimestamp"))
                if raw.get("lastTradeTimestamp")
                else None
            ),
        )

    # -- idempotent order submission -----------------------------------------

    async def _query_by_client_order_id(
        self, symbol: str, client_oid: str
    ) -> OrderStatus | None:
        """Query exchange for an order by its client order ID.

        Used after timeout/503 to check if the order was actually placed
        before deciding whether to retry. Returns None if order not found.
        """
        exchange = self._require_exchange()
        await asyncio.sleep(2)  # Brief wait for exchange-side propagation
        try:
            market = exchange.market(symbol)
            response = await exchange.fapiPrivateGetOrder(
                {
                    "symbol": market["id"],
                    "origClientOrderId": client_oid,
                }
            )
            parsed = exchange.parse_order(response, market)
            return self._parse_order_status(parsed)
        except Exception as exc:
            logger.info(
                "QUERY_CLIENT_OID client_oid=%s result=NOT_FOUND (%s)",
                client_oid,
                type(exc).__name__,
            )
            return None

    def _order_result_from_status(
        self, status: OrderStatus, client_oid: str
    ) -> OrderResult:
        """Convert an OrderStatus (from query-by-client-id) to an OrderResult."""
        return OrderResult(
            order_id=status.order_id,
            client_order_id=client_oid,
            symbol=status.symbol,
            side=status.side,
            order_type=status.order_type,
            amount=status.amount,
            price=status.price,
            stop_price=status.stop_price,
            status=status.status.value,
            filled=status.filled,
            average_fill_price=status.average_fill_price,
            cost=status.cost,
            fee=status.fee,
            fee_currency=status.fee_currency,
            timestamp=status.timestamp,
            verified=True,  # Queried exchange directly
            raw={},
        )

    async def _submit_order_idempotent(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: float | None = None,
        extra_params: dict[str, Any] | None = None,
        log_prefix: str = "ORDER",
    ) -> OrderResult | None:
        """Submit an order with idempotent retry logic.

        On timeout/503/NetworkError: queries by newClientOrderId before
        retrying to prevent duplicate orders. Each retry uses a NEW
        client order ID (the previous one was confirmed not-placed).

        Returns
        -------
        OrderResult | None
            Order result on success, None on InsufficientFunds/InvalidOrder.

        Raises
        ------
        RuntimeError
            If all retry attempts fail with transient errors.
        """
        exchange = self._require_exchange()
        last_exc: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            client_oid = _generate_client_order_id()
            params: dict[str, Any] = {
                **(extra_params or {}),
                "newClientOrderId": client_oid,
            }

            logger.info(
                "%s attempt=%d/%d symbol=%s side=%s amount=%s client_oid=%s",
                log_prefix,
                attempt,
                _MAX_RETRIES,
                symbol,
                side,
                amount,
                client_oid,
            )

            try:
                raw = await exchange.create_order(
                    symbol=symbol,
                    type=order_type,
                    side=side,
                    amount=amount,
                    price=price,
                    params=params,
                )

                # Success — verify with separate GET call
                order_id = str(raw.get("id", ""))
                logger.info(
                    "%s_PLACED order_id=%s client_oid=%s",
                    log_prefix,
                    order_id,
                    client_oid,
                )

                verified = False
                if self._verify_orders and order_id:
                    try:
                        # Conditional orders (SL/TP) need extra propagation
                        # time on testnet before they appear in GET queries.
                        # Use retry loop: 3s, then 5s, then give up.
                        is_conditional = order_type in (
                            "STOP_MARKET",
                            "TAKE_PROFIT_MARKET",
                            "STOP",
                            "TAKE_PROFIT",
                        )
                        verify_delays = [3.0, 5.0] if is_conditional else [0.5]
                        for delay in verify_delays:
                            await asyncio.sleep(delay)
                            try:
                                verification = await self._verify_order_exists(
                                    symbol, order_id
                                )
                                verified = True
                                logger.info(
                                    "%s_VERIFIED order_id=%s status=%s filled=%s",
                                    log_prefix,
                                    order_id,
                                    verification.status.value,
                                    verification.filled,
                                )
                                break
                            except Exception:
                                if delay == verify_delays[-1]:
                                    raise  # Last attempt, propagate
                                logger.info(
                                    "%s_VERIFY_RETRY order_id=%s after %.1fs",
                                    log_prefix, order_id, delay,
                                )
                    except Exception as exc:
                        logger.warning(
                            "%s_VERIFY_FAILED order_id=%s error=%s "
                            "(order was placed, verification timed out)",
                            log_prefix,
                            order_id,
                            exc,
                        )

                return self._parse_order_result(raw, client_oid, verified=verified)

            except (
                ccxt_async.NetworkError,
                ccxt_async.ExchangeNotAvailable,
                ccxt_async.RequestTimeout,
            ) as exc:
                # UNKNOWN STATE — order may or may not have been placed
                last_exc = exc
                logger.warning(
                    "%s_UNKNOWN_STATE client_oid=%s error=%s — querying exchange",
                    log_prefix,
                    client_oid,
                    exc,
                )

                # Query by client order ID to check if it was placed
                existing = await self._query_by_client_order_id(symbol, client_oid)
                if existing is not None:
                    logger.info(
                        "%s_FOUND_EXISTING client_oid=%s order_id=%s status=%s "
                        "— no retry needed",
                        log_prefix,
                        client_oid,
                        existing.order_id,
                        existing.status.value,
                    )
                    return self._order_result_from_status(existing, client_oid)

                # Confirmed NOT placed — safe to retry with NEW client order ID
                logger.info(
                    "%s_NOT_FOUND client_oid=%s — safe to retry with new ID",
                    log_prefix,
                    client_oid,
                )
                wait = _BACKOFF_BASE * (2 ** (attempt - 1))
                await asyncio.sleep(wait)

            except ccxt_async.DDoSProtection as exc:
                # DDoS protection definitely means order was NOT placed
                last_exc = exc
                wait = _BACKOFF_BASE * (2 ** (attempt - 1))
                logger.warning(
                    "%s_DDOS_PROTECTION attempt=%d/%d — waiting %.1fs",
                    log_prefix,
                    attempt,
                    _MAX_RETRIES,
                    wait,
                )
                await asyncio.sleep(wait)

            except ccxt_async.InsufficientFunds:
                logger.error(
                    "%s_INSUFFICIENT_FUNDS symbol=%s amount=%s — SKIP trade",
                    log_prefix,
                    symbol,
                    amount,
                )
                return None

            except ccxt_async.InvalidOrder as exc:
                logger.error(
                    "%s_INVALID_ORDER symbol=%s error=%s — SKIP trade",
                    log_prefix,
                    symbol,
                    exc,
                )
                return None

            except ccxt_async.ExchangeError as exc:
                logger.error(
                    "%s_EXCHANGE_ERROR symbol=%s error=%s",
                    log_prefix,
                    symbol,
                    exc,
                )
                raise

        raise RuntimeError(
            f"{log_prefix} failed after {_MAX_RETRIES} idempotent attempts"
        ) from last_exc

    # -- leverage ------------------------------------------------------------

    @_retry
    async def set_leverage(self, symbol: str, leverage: int) -> None:
        """Set leverage for a symbol on Binance Futures.

        Parameters
        ----------
        symbol : str
            Trading pair, e.g. ``"BTC/USDT:USDT"``.
        leverage : int
            Leverage multiplier (1-125 on Binance, but capped at 10 by our system).
        """
        if leverage < 1 or leverage > 10:
            raise ValueError(
                f"Leverage must be 1-10 (system maximum). Got {leverage}."
            )
        exchange = self._require_exchange()
        logger.info("SET_LEVERAGE symbol=%s leverage=%dx", symbol, leverage)
        await exchange.set_leverage(leverage, symbol)
        logger.info(
            "SET_LEVERAGE_OK symbol=%s leverage=%dx confirmed", symbol, leverage
        )

    # -- market order --------------------------------------------------------

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        amount: Decimal,
    ) -> OrderResult | None:
        """Place a market order with idempotent retry on Binance Futures.

        Parameters
        ----------
        symbol : str
            Trading pair, e.g. ``"BTC/USDT:USDT"``.
        side : str
            ``"buy"`` or ``"sell"``.
        amount : Decimal
            Order size in base currency units.

        Returns
        -------
        OrderResult | None
            Verified order result, or None if InsufficientFunds/InvalidOrder.
        """
        return await self._submit_order_idempotent(
            symbol=symbol,
            order_type="market",
            side=side.lower(),
            amount=float(amount),
            log_prefix="MARKET_ORDER",
        )

    # -- limit order ---------------------------------------------------------

    async def place_limit_order(
        self,
        symbol: str,
        side: str,
        amount: Decimal,
        price: Decimal,
    ) -> OrderResult | None:
        """Place a limit order with idempotent retry on Binance Futures.

        Parameters
        ----------
        symbol : str
            Trading pair.
        side : str
            ``"buy"`` or ``"sell"``.
        amount : Decimal
            Order size in base currency units.
        price : Decimal
            Limit price.

        Returns
        -------
        OrderResult | None
            Verified order result, or None if InsufficientFunds/InvalidOrder.
        """
        return await self._submit_order_idempotent(
            symbol=symbol,
            order_type="limit",
            side=side.lower(),
            amount=float(amount),
            price=float(price),
            log_prefix="LIMIT_ORDER",
        )

    # -- stop loss -----------------------------------------------------------

    async def place_stop_loss(
        self,
        symbol: str,
        side: str,
        amount: Decimal,
        stop_price: Decimal,
    ) -> OrderResult | None:
        """Place a stop-market (stop-loss) order with idempotent retry.

        Parameters
        ----------
        symbol : str
            Trading pair.
        side : str
            ``"buy"`` (for short stop-loss) or ``"sell"`` (for long stop-loss).
        amount : Decimal
            Order size in base currency units.
        stop_price : Decimal
            Trigger price for the stop-market order.

        Returns
        -------
        OrderResult | None
            Verified order result, or None if InsufficientFunds/InvalidOrder.
        """
        return await self._submit_order_idempotent(
            symbol=symbol,
            order_type="STOP_MARKET",
            side=side.lower(),
            amount=float(amount),
            extra_params={"stopPrice": float(stop_price), "reduceOnly": True},
            log_prefix="STOP_LOSS",
        )

    # -- take-profit order ---------------------------------------------------

    async def place_take_profit(
        self,
        symbol: str,
        side: str,
        amount: Decimal,
        stop_price: Decimal,
    ) -> OrderResult | None:
        """Place a take-profit market order with idempotent retry.

        Parameters
        ----------
        symbol : str
            Trading pair.
        side : str
            ``"buy"`` (for short TP) or ``"sell"`` (for long TP).
        amount : Decimal
            Order size in base currency units.
        stop_price : Decimal
            Trigger price for the take-profit order.

        Returns
        -------
        OrderResult | None
            Verified order result, or None if InsufficientFunds/InvalidOrder.
        """
        return await self._submit_order_idempotent(
            symbol=symbol,
            order_type="TAKE_PROFIT_MARKET",
            side=side.lower(),
            amount=float(amount),
            extra_params={"stopPrice": float(stop_price), "reduceOnly": True},
            log_prefix="TAKE_PROFIT",
        )

    # -- cancel order --------------------------------------------------------

    @_retry
    async def cancel_order(self, symbol: str, order_id: str) -> None:
        """Cancel an open order on Binance Futures.

        Parameters
        ----------
        symbol : str
            Trading pair.
        order_id : str
            Exchange order ID to cancel.
        """
        exchange = self._require_exchange()
        logger.info("CANCEL_ORDER symbol=%s order_id=%s", symbol, order_id)

        await exchange.cancel_order(order_id, symbol)
        logger.info(
            "CANCEL_ORDER_OK symbol=%s order_id=%s cancelled", symbol, order_id
        )

        # Verify cancellation
        if self._verify_orders:
            try:
                status = await self._verify_order_exists(symbol, order_id)
                if status.status != OrderState.CANCELED:
                    logger.warning(
                        "CANCEL_ORDER_VERIFY_MISMATCH order_id=%s "
                        "expected=canceled got=%s",
                        order_id,
                        status.status.value,
                    )
                else:
                    logger.info(
                        "CANCEL_ORDER_VERIFIED order_id=%s status=canceled",
                        order_id,
                    )
            except Exception as exc:
                logger.error(
                    "CANCEL_ORDER_VERIFY_FAILED order_id=%s error=%s",
                    order_id,
                    exc,
                )

    # -- order status --------------------------------------------------------

    @_retry
    async def get_order_status(
        self, symbol: str, order_id: str
    ) -> OrderStatus:
        """Fetch the current status of an order from the exchange.

        Parameters
        ----------
        symbol : str
            Trading pair.
        order_id : str
            Exchange order ID.

        Returns
        -------
        OrderStatus
            Current order status from exchange.
        """
        exchange = self._require_exchange()
        logger.info(
            "GET_ORDER_STATUS symbol=%s order_id=%s", symbol, order_id
        )
        raw = await exchange.fetch_order(order_id, symbol)
        status = self._parse_order_status(raw)
        logger.info(
            "GET_ORDER_STATUS_OK order_id=%s status=%s filled=%s/%s",
            order_id,
            status.status.value,
            status.filled,
            status.amount,
        )
        return status

    # -- open orders ---------------------------------------------------------

    @_retry
    async def get_open_orders(self, symbol: str | None = None) -> list[OrderStatus]:
        """Fetch all open orders, optionally filtered by symbol.

        Parameters
        ----------
        symbol : str | None
            If provided, only return orders for this symbol.

        Returns
        -------
        list[OrderStatus]
            List of open order statuses.
        """
        exchange = self._require_exchange()
        logger.info("GET_OPEN_ORDERS symbol=%s", symbol or "ALL")
        raw_orders = await exchange.fetch_open_orders(symbol)
        orders = [self._parse_order_status(raw) for raw in raw_orders]
        logger.info(
            "GET_OPEN_ORDERS_OK count=%d symbol=%s",
            len(orders),
            symbol or "ALL",
        )
        return orders

    async def cancel_open_orders(self, symbol: str) -> int:
        """Cancel all open orders for a symbol.

        Parameters
        ----------
        symbol : str
            Trading pair.

        Returns
        -------
        int
            Number of orders cancelled.
        """
        open_orders = await self.get_open_orders(symbol)
        cancelled = 0
        for order in open_orders:
            try:
                await self.cancel_order(symbol, order.order_id)
                cancelled += 1
            except Exception as exc:
                logger.error(
                    "Failed to cancel order %s for %s: %s",
                    order.order_id, symbol, exc,
                )
        logger.info(
            "CANCEL_OPEN_ORDERS symbol=%s cancelled=%d/%d",
            symbol, cancelled, len(open_orders),
        )
        return cancelled
