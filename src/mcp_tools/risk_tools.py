"""
MCP server for risk management.

Provides circuit breaker checks, position sizing, correlation analysis,
and portfolio risk metrics. Integrates with the hardcoded CircuitBreaker
and configurable risk parameters.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import numpy as np
import pandas as pd
import yaml
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
from pydantic import BaseModel, Field

from src.data.market_data import MarketDataClient
from src.risk.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerLevel,
    CircuitBreakerState,
    TradeResult,
)

logger = logging.getLogger("claude_quant.mcp_tools.risk")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "config")
_STATE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "user_data", "agent_state")


def _load_risk_params() -> dict[str, Any]:
    """Load risk management parameters from config."""
    path = os.path.join(_CONFIG_DIR, "risk", "risk_params.yaml")
    try:
        with open(path, "r") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning("Risk params not found at %s, using defaults", path)
        return {}


def _load_state_file(filename: str) -> dict[str, Any]:
    """Load a JSON state file from agent_state directory."""
    path = os.path.join(_STATE_DIR, filename)
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state_file(filename: str, data: dict[str, Any]) -> None:
    """Save a JSON state file to agent_state directory."""
    os.makedirs(_STATE_DIR, exist_ok=True)
    path = os.path.join(_STATE_DIR, filename)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


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


# ---------------------------------------------------------------------------
# Pydantic input schemas
# ---------------------------------------------------------------------------


class PositionSizeInput(BaseModel):
    """Input for position size calculation."""

    symbol: str = Field(description="Trading pair, e.g. 'BTC/USDT:USDT'")
    direction: str = Field(description="'long' or 'short'")
    entry_price: float = Field(gt=0, description="Planned entry price")
    stop_loss: float = Field(gt=0, description="Stop loss price")


class CheckCorrelationInput(BaseModel):
    """Input for correlation check."""

    new_symbol: str = Field(description="Symbol to check correlation against existing positions")


class GetRiskMetricsInput(BaseModel):
    """Empty input for risk metrics."""

    pass


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_market_client: MarketDataClient | None = None


async def _get_market_client() -> MarketDataClient:
    """Lazily initialise the shared market data client."""
    global _market_client
    if _market_client is None:
        _market_client = MarketDataClient()
        await _market_client.connect()
    return _market_client


async def _close_market_client() -> None:
    """Shut down the market data client."""
    global _market_client
    if _market_client is not None:
        await _market_client.close()
        _market_client = None


def _load_recent_trades() -> list[TradeResult]:
    """Load recent trade results from state for circuit breaker evaluation."""
    state = _load_state_file("trade_results.json")
    trades_raw = state.get("trades", [])
    results = []
    for t in trades_raw:
        try:
            results.append(
                TradeResult(
                    is_win=t.get("is_win", False),
                    closed_at=datetime.fromisoformat(t["closed_at"])
                    if "closed_at" in t
                    else datetime.now(timezone.utc),
                )
            )
        except (KeyError, ValueError):
            continue
    return results


def _load_start_of_day_balance() -> Decimal | None:
    """Load the start-of-day balance for daily loss tracking."""
    state = _load_state_file("daily_state.json")
    balance_str = state.get("start_of_day_balance")
    if balance_str is not None:
        return _to_decimal(balance_str)
    return None


async def _get_current_balance() -> Decimal:
    """Fetch the current account balance from Binance."""
    import ccxt.async_support as ccxt_async

    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    testnet = os.getenv("BINANCE_TESTNET", "false").lower() == "true"

    exchange = ccxt_async.binanceusdm(
        {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": "future", "adjustForTimeDifference": True},
        }
    )
    if testnet:
        # ccxt >= 4.5.6: use enable_demo_trading instead of set_sandbox_mode (ccxt #26487)
        exchange.enable_demo_trading(True)

    try:
        await exchange.load_markets()
        balance = await exchange.fetch_balance()
        usdt_total = _to_decimal(balance.get("USDT", {}).get("total", 0))
        return usdt_total
    finally:
        await exchange.close()


async def _get_open_positions() -> list[dict[str, Any]]:
    """Fetch open positions from Binance."""
    import ccxt.async_support as ccxt_async

    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    testnet = os.getenv("BINANCE_TESTNET", "false").lower() == "true"

    exchange = ccxt_async.binanceusdm(
        {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": "future", "adjustForTimeDifference": True},
        }
    )
    if testnet:
        # ccxt >= 4.5.6: use enable_demo_trading instead of set_sandbox_mode (ccxt #26487)
        exchange.enable_demo_trading(True)

    try:
        await exchange.load_markets()
        positions = await exchange.fetch_positions()
        active = []
        for pos in positions:
            contracts = float(pos.get("contracts", 0) or 0)
            if contracts > 0:
                active.append(pos)
        return active
    finally:
        await exchange.close()


# ---------------------------------------------------------------------------
# MCP tool definitions
# ---------------------------------------------------------------------------

_TOOL_DEFINITIONS: list[Tool] = [
    Tool(
        name="check_circuit_breaker",
        description=(
            "Check the current circuit breaker level (GREEN/YELLOW/RED/DEAD) and "
            "all associated constraints: max leverage, max positions, size multiplier, "
            "daily loss status, and consecutive loss pause status."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="calculate_position_size",
        description=(
            "Calculate the approved position size for a trade using half-Kelly criterion, "
            "constrained by circuit breaker level, max capital per trade (15%), and "
            "risk/reward requirements. Returns approved size or rejection reason."
        ),
        inputSchema=PositionSizeInput.model_json_schema(),
    ),
    Tool(
        name="check_correlation",
        description=(
            "Check whether adding a new position in a symbol would create excessive "
            "correlation risk with existing positions. Uses 30-day price correlation."
        ),
        inputSchema=CheckCorrelationInput.model_json_schema(),
    ),
    Tool(
        name="get_risk_metrics",
        description=(
            "Get a comprehensive view of current portfolio risk: total exposure, "
            "per-position risk, portfolio heat, drawdown, and all circuit breaker details."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
]


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------


def create_risk_server() -> Server:
    """Create and configure the Risk MCP server."""
    server = Server("risk-tools")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return _TOOL_DEFINITIONS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        try:
            result = await _dispatch_tool(name, arguments)
            return [TextContent(type="text", text=json.dumps(result, default=str))]
        except Exception as exc:
            logger.exception("Tool %s failed", name)
            error_msg = f"Error in {name}: {type(exc).__name__}: {exc}"
            return [TextContent(type="text", text=error_msg)]

    return server


async def _dispatch_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Route tool calls."""
    handlers = {
        "check_circuit_breaker": _handle_check_circuit_breaker,
        "calculate_position_size": _handle_calculate_position_size,
        "check_correlation": _handle_check_correlation,
        "get_risk_metrics": _handle_get_risk_metrics,
    }
    handler = handlers.get(name)
    if handler is None:
        raise ValueError(f"Unknown tool: {name}")
    return await handler(arguments)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


async def _handle_check_circuit_breaker(arguments: dict[str, Any]) -> dict[str, Any]:
    """Check the full circuit breaker state."""
    balance = await _get_current_balance()
    recent_trades = _load_recent_trades()
    start_of_day = _load_start_of_day_balance()

    state: CircuitBreakerState = CircuitBreaker.is_trading_allowed(
        balance=balance,
        recent_trades=recent_trades,
        start_of_day_balance=start_of_day,
    )

    result = {
        "level": state.level.value,
        "trading_allowed": state.constraints.trading_allowed,
        "balance": str(state.balance),
        "constraints": {
            "max_leverage": state.constraints.max_leverage,
            "max_positions": state.constraints.max_positions,
            "size_multiplier": str(state.constraints.size_multiplier),
            "reason": state.constraints.reason,
        },
        "daily_loss_pct": str(state.daily_loss_pct),
        "daily_loss_halt": state.daily_loss_halt,
        "consecutive_loss_pause": state.consecutive_loss_pause,
        "pause_until_utc": state.pause_until_utc.isoformat() if state.pause_until_utc else None,
        "thresholds": {
            k: str(v) if isinstance(v, Decimal) else v
            for k, v in CircuitBreaker.thresholds_summary().items()
        },
        "timestamp": _utc_now_iso(),
    }

    logger.info("Circuit breaker check: level=%s, trading_allowed=%s", state.level.value, state.constraints.trading_allowed)
    return result


async def _handle_calculate_position_size(arguments: dict[str, Any]) -> dict[str, Any]:
    """Calculate approved position size with all risk constraints."""
    params = PositionSizeInput(**arguments)
    risk_config = _load_risk_params()

    # Fetch current state
    balance = await _get_current_balance()
    recent_trades = _load_recent_trades()
    start_of_day = _load_start_of_day_balance()

    # Circuit breaker check first
    cb_state = CircuitBreaker.is_trading_allowed(
        balance=balance,
        recent_trades=recent_trades,
        start_of_day_balance=start_of_day,
    )

    if not cb_state.constraints.trading_allowed:
        return {
            "approved": False,
            "symbol": params.symbol,
            "direction": params.direction,
            "reason": f"Trading blocked by circuit breaker: {cb_state.constraints.reason}",
            "circuit_breaker_level": cb_state.level.value,
            "position_size": "0",
            "timestamp": _utc_now_iso(),
        }

    # Check open positions count
    open_positions = await _get_open_positions()
    if len(open_positions) >= cb_state.constraints.max_positions:
        return {
            "approved": False,
            "symbol": params.symbol,
            "direction": params.direction,
            "reason": (
                f"Max positions reached: {len(open_positions)}/{cb_state.constraints.max_positions} "
                f"(circuit breaker level: {cb_state.level.value})"
            ),
            "circuit_breaker_level": cb_state.level.value,
            "position_size": "0",
            "timestamp": _utc_now_iso(),
        }

    # Check if already in this symbol
    for pos in open_positions:
        if pos.get("symbol") == params.symbol:
            return {
                "approved": False,
                "symbol": params.symbol,
                "direction": params.direction,
                "reason": f"Already have an open position in {params.symbol}",
                "circuit_breaker_level": cb_state.level.value,
                "position_size": "0",
                "timestamp": _utc_now_iso(),
            }

    # Calculate risk per trade
    entry = Decimal(str(params.entry_price))
    stop = Decimal(str(params.stop_loss))
    risk_distance_pct = abs(entry - stop) / entry

    # Validate stop loss distance
    ps_config = risk_config.get("position_sizing", {})
    sl_config = risk_config.get("stop_loss", {})
    rr_config = risk_config.get("risk_reward", {})

    max_sl_pct = Decimal(str(sl_config.get("max_sl_pct", 0.03)))
    if risk_distance_pct > max_sl_pct:
        return {
            "approved": False,
            "symbol": params.symbol,
            "direction": params.direction,
            "reason": (
                f"Stop loss distance ({risk_distance_pct:.2%}) exceeds maximum "
                f"allowed ({max_sl_pct:.0%})"
            ),
            "circuit_breaker_level": cb_state.level.value,
            "position_size": "0",
            "timestamp": _utc_now_iso(),
        }

    # Validate stop loss direction
    if params.direction == "long" and stop >= entry:
        return {
            "approved": False,
            "symbol": params.symbol,
            "direction": params.direction,
            "reason": "Stop loss must be below entry price for long positions",
            "circuit_breaker_level": cb_state.level.value,
            "position_size": "0",
            "timestamp": _utc_now_iso(),
        }
    if params.direction == "short" and stop <= entry:
        return {
            "approved": False,
            "symbol": params.symbol,
            "direction": params.direction,
            "reason": "Stop loss must be above entry price for short positions",
            "circuit_breaker_level": cb_state.level.value,
            "position_size": "0",
            "timestamp": _utc_now_iso(),
        }

    # Position sizing: half-Kelly method
    max_risk_per_trade_pct = Decimal(str(rr_config.get("max_risk_per_trade_pct", 0.02)))
    max_position_pct = Decimal(str(ps_config.get("max_position_pct", 0.15)))
    kelly_fraction = Decimal(str(ps_config.get("kelly_fraction", 0.5)))
    min_position_usd = Decimal(str(ps_config.get("min_position_usd", 5.0)))

    # Risk budget = balance * max_risk_per_trade * size_multiplier
    size_multiplier = cb_state.constraints.size_multiplier
    risk_budget = balance * max_risk_per_trade_pct * size_multiplier

    # Position size from risk budget
    if risk_distance_pct > 0:
        position_notional = risk_budget / risk_distance_pct
    else:
        position_notional = Decimal("0")

    # Apply maximum position cap (15% of balance)
    max_notional = balance * max_position_pct * size_multiplier
    position_notional = min(position_notional, max_notional)

    # Apply half-Kelly
    position_notional = position_notional * kelly_fraction

    # Convert to contracts (amount in base currency)
    if entry > 0:
        position_size = position_notional / entry
    else:
        position_size = Decimal("0")

    # Check minimum
    if position_notional < min_position_usd:
        return {
            "approved": False,
            "symbol": params.symbol,
            "direction": params.direction,
            "reason": (
                f"Calculated position notional (${position_notional:.2f}) is below "
                f"minimum viable size (${min_position_usd:.2f})"
            ),
            "circuit_breaker_level": cb_state.level.value,
            "position_size": "0",
            "timestamp": _utc_now_iso(),
        }

    # Determine recommended leverage
    lev_config = risk_config.get("leverage", {})
    default_leverage = min(
        int(lev_config.get("default_leverage", 3)),
        cb_state.constraints.max_leverage,
    )

    result = {
        "approved": True,
        "symbol": params.symbol,
        "direction": params.direction,
        "position_size": str(round(position_size, 8)),
        "position_notional_usd": str(round(position_notional, 2)),
        "entry_price": str(entry),
        "stop_loss": str(stop),
        "risk_distance_pct": str(round(risk_distance_pct * 100, 2)) + "%",
        "risk_amount_usd": str(round(risk_budget, 2)),
        "recommended_leverage": default_leverage,
        "max_leverage": cb_state.constraints.max_leverage,
        "circuit_breaker_level": cb_state.level.value,
        "size_multiplier": str(size_multiplier),
        "balance": str(balance),
        "timestamp": _utc_now_iso(),
    }

    logger.info(
        "Position size approved for %s %s: size=%s, notional=$%s, leverage=%dx",
        params.direction,
        params.symbol,
        result["position_size"],
        result["position_notional_usd"],
        default_leverage,
    )
    return result


async def _handle_check_correlation(arguments: dict[str, Any]) -> dict[str, Any]:
    """Check correlation of new symbol with existing positions."""
    params = CheckCorrelationInput(**arguments)
    risk_config = _load_risk_params()
    corr_config = risk_config.get("correlation", {})
    corr_threshold = float(corr_config.get("correlation_threshold", 0.7))
    max_correlated_exposure = float(corr_config.get("max_correlated_exposure", 0.25))

    # Get open positions
    open_positions = await _get_open_positions()

    if not open_positions:
        return {
            "safe_to_add": True,
            "new_symbol": params.new_symbol,
            "correlations": [],
            "max_correlation": 0.0,
            "reason": "No existing positions, no correlation risk.",
            "timestamp": _utc_now_iso(),
        }

    # Fetch price data for correlation computation
    client = await _get_market_client()
    correlations = []

    try:
        # Fetch new symbol data
        new_candles = await client.fetch_ohlcv(params.new_symbol, timeframe="1h", limit=720)
        new_df = pd.DataFrame(new_candles)
        new_df["close"] = new_df["close"].astype(float)
        new_returns = new_df["close"].pct_change().dropna()

        for pos in open_positions:
            pos_symbol = pos.get("symbol", "")
            if not pos_symbol or pos_symbol == params.new_symbol:
                continue

            try:
                pos_candles = await client.fetch_ohlcv(pos_symbol, timeframe="1h", limit=720)
                pos_df = pd.DataFrame(pos_candles)
                pos_df["close"] = pos_df["close"].astype(float)
                pos_returns = pos_df["close"].pct_change().dropna()

                # Align lengths
                min_len = min(len(new_returns), len(pos_returns))
                if min_len < 30:
                    correlations.append(
                        {
                            "symbol": pos_symbol,
                            "correlation": None,
                            "note": "Insufficient data for correlation",
                        }
                    )
                    continue

                corr_val = float(
                    np.corrcoef(
                        new_returns.values[-min_len:],
                        pos_returns.values[-min_len:],
                    )[0, 1]
                )

                notional = abs(float(pos.get("notional", 0) or 0))
                correlations.append(
                    {
                        "symbol": pos_symbol,
                        "correlation": round(corr_val, 4),
                        "abs_correlation": round(abs(corr_val), 4),
                        "position_notional": round(notional, 2),
                        "highly_correlated": abs(corr_val) >= corr_threshold,
                    }
                )
            except Exception as exc:
                logger.warning("Failed to compute correlation for %s: %s", pos_symbol, exc)
                correlations.append(
                    {
                        "symbol": pos_symbol,
                        "correlation": None,
                        "note": f"Error: {exc}",
                    }
                )

    except Exception as exc:
        return {
            "safe_to_add": True,
            "new_symbol": params.new_symbol,
            "correlations": [],
            "max_correlation": 0.0,
            "reason": f"Could not compute correlations: {exc}. Proceeding with caution.",
            "timestamp": _utc_now_iso(),
        }

    # Evaluate safety
    max_corr = max(
        (abs(c.get("correlation", 0) or 0) for c in correlations),
        default=0.0,
    )
    highly_correlated = [c for c in correlations if c.get("highly_correlated")]

    # Check total correlated exposure
    correlated_notional = sum(
        c.get("position_notional", 0) for c in highly_correlated
    )
    balance = await _get_current_balance()
    balance_f = float(balance) if balance > 0 else 1
    correlated_exposure_pct = correlated_notional / balance_f

    safe = True
    reason = "Correlation within acceptable limits."

    if highly_correlated and correlated_exposure_pct >= max_correlated_exposure:
        safe = False
        reason = (
            f"Adding {params.new_symbol} would create excessive correlated exposure. "
            f"Correlated positions: {[c['symbol'] for c in highly_correlated]}. "
            f"Total correlated exposure: {correlated_exposure_pct:.1%} "
            f"(limit: {max_correlated_exposure:.0%})"
        )
    elif highly_correlated:
        reason = (
            f"Warning: {params.new_symbol} is correlated with "
            f"{[c['symbol'] for c in highly_correlated]}, "
            f"but exposure ({correlated_exposure_pct:.1%}) is within limits."
        )

    result = {
        "safe_to_add": safe,
        "new_symbol": params.new_symbol,
        "correlations": correlations,
        "max_correlation": round(max_corr, 4),
        "correlated_exposure_pct": round(correlated_exposure_pct * 100, 2),
        "highly_correlated_symbols": [c["symbol"] for c in highly_correlated],
        "reason": reason,
        "timestamp": _utc_now_iso(),
    }

    logger.info(
        "Correlation check for %s: safe=%s, max_corr=%.4f",
        params.new_symbol,
        safe,
        max_corr,
    )
    return result


async def _handle_get_risk_metrics(arguments: dict[str, Any]) -> dict[str, Any]:
    """Compute comprehensive portfolio risk metrics."""
    balance = await _get_current_balance()
    open_positions = await _get_open_positions()
    recent_trades = _load_recent_trades()
    start_of_day = _load_start_of_day_balance()

    # Circuit breaker state
    cb_state = CircuitBreaker.is_trading_allowed(
        balance=balance,
        recent_trades=recent_trades,
        start_of_day_balance=start_of_day,
    )

    # Position-level risk
    total_notional = Decimal("0")
    total_unrealised_pnl = Decimal("0")
    position_risks = []

    for pos in open_positions:
        notional = abs(_to_decimal(pos.get("notional", 0)))
        unrealised = _to_decimal(pos.get("unrealizedPnl", 0))
        leverage = int(pos.get("leverage", 1) or 1)
        entry = _to_decimal(pos.get("entryPrice", 0))
        mark = _to_decimal(pos.get("markPrice", 0))
        liq = _to_decimal(pos.get("liquidationPrice", 0))

        # Distance to liquidation
        liq_distance_pct = Decimal("0")
        if mark > 0 and liq > 0:
            liq_distance_pct = abs(mark - liq) / mark * 100

        total_notional += notional
        total_unrealised_pnl += unrealised

        position_risks.append(
            {
                "symbol": pos.get("symbol", ""),
                "side": pos.get("side", ""),
                "notional": str(notional),
                "unrealised_pnl": str(unrealised),
                "leverage": leverage,
                "entry_price": str(entry),
                "mark_price": str(mark),
                "liquidation_price": str(liq),
                "liq_distance_pct": str(round(liq_distance_pct, 2)),
                "pnl_pct": str(round((unrealised / notional * 100) if notional > 0 else Decimal("0"), 2)),
            }
        )

    # Portfolio-level metrics
    portfolio_exposure = total_notional / balance if balance > 0 else Decimal("0")
    portfolio_heat = portfolio_exposure * 100  # As percentage

    # Daily P&L
    daily_pnl = Decimal("0")
    daily_pnl_pct = Decimal("0")
    if start_of_day is not None and start_of_day > 0:
        daily_pnl = balance - start_of_day
        daily_pnl_pct = daily_pnl / start_of_day * 100

    # Win rate from recent trades
    if recent_trades:
        wins = sum(1 for t in recent_trades if t.is_win)
        win_rate = wins / len(recent_trades)
        last_10 = recent_trades[-10:]
        recent_wins = sum(1 for t in last_10 if t.is_win)
        recent_win_rate = recent_wins / len(last_10) if last_10 else 0
    else:
        win_rate = 0.0
        recent_win_rate = 0.0

    # Consecutive losses
    consecutive_losses = 0
    for t in reversed(recent_trades):
        if not t.is_win:
            consecutive_losses += 1
        else:
            break

    # Available capacity
    available_positions = max(0, cb_state.constraints.max_positions - len(open_positions))

    result = {
        "portfolio": {
            "balance": str(balance),
            "total_notional_exposure": str(total_notional),
            "exposure_ratio": str(round(portfolio_exposure, 4)),
            "portfolio_heat_pct": str(round(portfolio_heat, 2)),
            "unrealised_pnl": str(total_unrealised_pnl),
            "daily_pnl": str(daily_pnl),
            "daily_pnl_pct": str(round(daily_pnl_pct, 2)),
        },
        "positions": position_risks,
        "position_count": len(open_positions),
        "available_position_slots": available_positions,
        "trade_stats": {
            "total_trades": len(recent_trades),
            "overall_win_rate": round(win_rate, 4),
            "recent_win_rate_last_10": round(recent_win_rate, 4),
            "consecutive_losses": consecutive_losses,
        },
        "circuit_breaker": {
            "level": cb_state.level.value,
            "trading_allowed": cb_state.constraints.trading_allowed,
            "max_leverage": cb_state.constraints.max_leverage,
            "max_positions": cb_state.constraints.max_positions,
            "size_multiplier": str(cb_state.constraints.size_multiplier),
            "reason": cb_state.constraints.reason,
            "daily_loss_halt": cb_state.daily_loss_halt,
            "consecutive_loss_pause": cb_state.consecutive_loss_pause,
        },
        "timestamp": _utc_now_iso(),
    }

    logger.info(
        "Risk metrics: balance=%s, exposure=%s, positions=%d, CB=%s",
        balance,
        total_notional,
        len(open_positions),
        cb_state.level.value,
    )
    return result


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    """Run the Risk MCP server over stdio."""
    server = create_risk_server()
    logger.info("Starting Risk MCP server...")
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        await _close_market_client()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    asyncio.run(main())
