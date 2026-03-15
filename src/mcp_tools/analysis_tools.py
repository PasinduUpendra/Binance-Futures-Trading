"""
MCP server for market analysis.

Provides technical analysis, regime detection, strategy signals, and
support/resistance level computation. Uses the existing IndicatorEngine
and MarketDataClient from the Claude Quant data layer.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
import yaml
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
from pydantic import BaseModel, Field

from src.data.indicator_engine import IndicatorEngine
from src.data.market_data import MarketDataClient

logger = logging.getLogger("claude_quant.mcp_tools.analysis")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "config")


def _load_regime_params() -> dict[str, Any]:
    """Load regime detection parameters from config file."""
    path = os.path.join(_CONFIG_DIR, "regime", "regime_params.yaml")
    try:
        with open(path, "r") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning("Regime params file not found at %s, using defaults", path)
        return {}


# ---------------------------------------------------------------------------
# Pydantic input schemas
# ---------------------------------------------------------------------------


class AnalyzeMarketInput(BaseModel):
    """Input for full market analysis."""

    symbol: str = Field(description="Trading pair, e.g. 'BTC/USDT:USDT'")
    timeframe: str = Field(default="5m", description="Candle timeframe, e.g. '1m', '5m', '1h'")


class GetRegimeInput(BaseModel):
    """Input for market regime detection."""

    symbol: str = Field(description="Trading pair, e.g. 'BTC/USDT:USDT'")
    timeframe: str = Field(default="5m", description="Candle timeframe")


class GetSignalsInput(BaseModel):
    """Input for strategy signal generation."""

    symbol: str = Field(description="Trading pair, e.g. 'BTC/USDT:USDT'")
    timeframe: str = Field(default="5m", description="Candle timeframe")


class GetSupportResistanceInput(BaseModel):
    """Input for support/resistance level detection."""

    symbol: str = Field(description="Trading pair, e.g. 'BTC/USDT:USDT'")
    timeframe: str = Field(default="1h", description="Candle timeframe (higher TF recommended)")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_market_client: MarketDataClient | None = None
_indicator_engine = IndicatorEngine()


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


async def _fetch_enriched_df(
    symbol: str, timeframe: str, limit: int = 300
) -> pd.DataFrame:
    """Fetch OHLCV data and compute all technical indicators."""
    client = await _get_market_client()
    candles = await client.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    df = pd.DataFrame(candles)
    # Convert Decimal columns to float for indicator computation
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = df[col].astype(float)

    df = _indicator_engine.calculate_all(df)
    return df


def _safe_float(val: Any) -> float | None:
    """Safely convert a value to float, returning None for NaN/inf."""
    if val is None:
        return None
    try:
        f = float(val)
        if np.isnan(f) or np.isinf(f):
            return None
        return round(f, 8)
    except (ValueError, TypeError):
        return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Analysis logic
# ---------------------------------------------------------------------------


def _detect_regime(df: pd.DataFrame) -> dict[str, Any]:
    """
    Classify the current market regime based on indicator values.

    Regimes:
    - strong_trend: High ADX, clear directional movement
    - moderate_trend: Moderate ADX, some trend
    - ranging: Low ADX, narrow bands, consolidation
    - volatile: Wide BB, high ATR, no clear direction
    - quiet: Very low activity
    """
    if df.empty or len(df) < 50:
        return {"regime": "unknown", "confidence": 0, "details": "Insufficient data"}

    last = df.iloc[-1]
    recent = df.tail(50)

    adx = _safe_float(last.get("adx")) or 0
    rsi = _safe_float(last.get("rsi")) or 50
    bb_width = _safe_float(last.get("bb_width")) or 0
    atr = _safe_float(last.get("atr")) or 0
    supertrend_dir = _safe_float(last.get("supertrend_direction")) or 0

    # Compute averages for relative comparisons
    avg_bb_width = recent["bb_width"].dropna().mean() if "bb_width" in recent.columns else bb_width
    avg_atr = recent["atr"].dropna().mean() if "atr" in recent.columns else atr
    vol_sma = _safe_float(last.get("volume_sma")) or 1
    curr_vol = _safe_float(last.get("volume")) or 0
    vol_ratio = curr_vol / vol_sma if vol_sma > 0 else 1

    # BB width relative to average
    bb_ratio = bb_width / avg_bb_width if avg_bb_width > 0 else 1
    atr_ratio = atr / avg_atr if avg_atr > 0 else 1

    # EMA alignment check
    ema_9 = _safe_float(last.get("ema_9")) or 0
    ema_21 = _safe_float(last.get("ema_21")) or 0
    ema_50 = _safe_float(last.get("ema_50")) or 0
    emas_aligned_bull = ema_9 > ema_21 > ema_50 > 0
    emas_aligned_bear = ema_9 < ema_21 < ema_50 > 0

    # Classify regime
    regime = "unknown"
    confidence = 0
    sub_type = ""

    if adx >= 30 and (emas_aligned_bull or emas_aligned_bear):
        regime = "strong_trend"
        sub_type = "bullish" if emas_aligned_bull else "bearish"
        confidence = min(95, int(50 + adx))
    elif adx >= 20:
        regime = "moderate_trend"
        if supertrend_dir > 0:
            sub_type = "bullish"
        elif supertrend_dir < 0:
            sub_type = "bearish"
        else:
            sub_type = "neutral"
        confidence = min(80, int(40 + adx))
    elif bb_ratio > 1.5 or atr_ratio > 1.3:
        regime = "volatile"
        sub_type = "expanding"
        confidence = min(85, int(50 + bb_ratio * 20))
    elif adx < 15 and bb_ratio < 0.7 and vol_ratio < 0.7:
        regime = "quiet"
        sub_type = "low_activity"
        confidence = min(80, int(60 + (1 - bb_ratio) * 30))
    else:
        regime = "ranging"
        sub_type = "consolidation"
        confidence = min(75, int(40 + (25 - adx) * 2))

    return {
        "regime": regime,
        "sub_type": sub_type,
        "confidence": confidence,
        "details": {
            "adx": _safe_float(adx),
            "rsi": _safe_float(rsi),
            "bb_width": _safe_float(bb_width),
            "bb_width_ratio": _safe_float(bb_ratio),
            "atr": _safe_float(atr),
            "atr_ratio": _safe_float(atr_ratio),
            "volume_ratio": _safe_float(vol_ratio),
            "supertrend_direction": int(supertrend_dir) if supertrend_dir else 0,
            "emas_aligned_bull": emas_aligned_bull,
            "emas_aligned_bear": emas_aligned_bear,
        },
    }


def _generate_signals(df: pd.DataFrame) -> dict[str, Any]:
    """
    Generate trading signals from multiple strategies.

    Strategies evaluated:
    1. EMA crossover (trend following)
    2. RSI divergence (mean reversion)
    3. MACD momentum
    4. Supertrend directional
    5. Bollinger Band squeeze breakout
    """
    if df.empty or len(df) < 50:
        return {"signals": [], "consensus": "neutral", "details": "Insufficient data"}

    signals = []
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last

    # -- 1. EMA Crossover Signal --
    ema_9 = _safe_float(last.get("ema_9"))
    ema_21 = _safe_float(last.get("ema_21"))
    prev_ema_9 = _safe_float(prev.get("ema_9"))
    prev_ema_21 = _safe_float(prev.get("ema_21"))

    if all(v is not None for v in (ema_9, ema_21, prev_ema_9, prev_ema_21)):
        if prev_ema_9 <= prev_ema_21 and ema_9 > ema_21:
            signals.append(
                {
                    "strategy": "ema_crossover",
                    "direction": "long",
                    "strength": "strong",
                    "detail": f"EMA9 ({ema_9:.2f}) crossed above EMA21 ({ema_21:.2f})",
                }
            )
        elif prev_ema_9 >= prev_ema_21 and ema_9 < ema_21:
            signals.append(
                {
                    "strategy": "ema_crossover",
                    "direction": "short",
                    "strength": "strong",
                    "detail": f"EMA9 ({ema_9:.2f}) crossed below EMA21 ({ema_21:.2f})",
                }
            )
        elif ema_9 > ema_21:
            signals.append(
                {
                    "strategy": "ema_crossover",
                    "direction": "long",
                    "strength": "moderate",
                    "detail": f"EMA9 ({ema_9:.2f}) above EMA21 ({ema_21:.2f}), trend intact",
                }
            )
        elif ema_9 < ema_21:
            signals.append(
                {
                    "strategy": "ema_crossover",
                    "direction": "short",
                    "strength": "moderate",
                    "detail": f"EMA9 ({ema_9:.2f}) below EMA21 ({ema_21:.2f}), trend intact",
                }
            )

    # -- 2. RSI Signal --
    rsi = _safe_float(last.get("rsi"))
    if rsi is not None:
        if rsi < 30:
            signals.append(
                {
                    "strategy": "rsi_oversold",
                    "direction": "long",
                    "strength": "strong" if rsi < 20 else "moderate",
                    "detail": f"RSI at {rsi:.1f} (oversold)",
                }
            )
        elif rsi > 70:
            signals.append(
                {
                    "strategy": "rsi_overbought",
                    "direction": "short",
                    "strength": "strong" if rsi > 80 else "moderate",
                    "detail": f"RSI at {rsi:.1f} (overbought)",
                }
            )

    # -- 3. MACD Signal --
    macd = _safe_float(last.get("macd"))
    macd_signal = _safe_float(last.get("macd_signal"))
    macd_hist = _safe_float(last.get("macd_hist"))
    prev_macd = _safe_float(prev.get("macd"))
    prev_signal = _safe_float(prev.get("macd_signal"))

    if all(v is not None for v in (macd, macd_signal, prev_macd, prev_signal)):
        if prev_macd <= prev_signal and macd > macd_signal:
            signals.append(
                {
                    "strategy": "macd_crossover",
                    "direction": "long",
                    "strength": "strong",
                    "detail": f"MACD ({macd:.4f}) crossed above signal ({macd_signal:.4f})",
                }
            )
        elif prev_macd >= prev_signal and macd < macd_signal:
            signals.append(
                {
                    "strategy": "macd_crossover",
                    "direction": "short",
                    "strength": "strong",
                    "detail": f"MACD ({macd:.4f}) crossed below signal ({macd_signal:.4f})",
                }
            )
        elif macd_hist is not None:
            direction = "long" if macd_hist > 0 else "short"
            signals.append(
                {
                    "strategy": "macd_momentum",
                    "direction": direction,
                    "strength": "weak",
                    "detail": f"MACD histogram at {macd_hist:.4f}",
                }
            )

    # -- 4. Supertrend Signal --
    st_dir = _safe_float(last.get("supertrend_direction"))
    prev_st_dir = _safe_float(prev.get("supertrend_direction"))
    supertrend_val = _safe_float(last.get("supertrend"))

    if st_dir is not None and prev_st_dir is not None:
        if prev_st_dir < 0 and st_dir > 0:
            signals.append(
                {
                    "strategy": "supertrend",
                    "direction": "long",
                    "strength": "strong",
                    "detail": f"Supertrend flipped bullish at {supertrend_val}",
                }
            )
        elif prev_st_dir > 0 and st_dir < 0:
            signals.append(
                {
                    "strategy": "supertrend",
                    "direction": "short",
                    "strength": "strong",
                    "detail": f"Supertrend flipped bearish at {supertrend_val}",
                }
            )
        elif st_dir > 0:
            signals.append(
                {
                    "strategy": "supertrend",
                    "direction": "long",
                    "strength": "moderate",
                    "detail": f"Supertrend bullish, support at {supertrend_val}",
                }
            )
        elif st_dir < 0:
            signals.append(
                {
                    "strategy": "supertrend",
                    "direction": "short",
                    "strength": "moderate",
                    "detail": f"Supertrend bearish, resistance at {supertrend_val}",
                }
            )

    # -- 5. Bollinger Band Squeeze Signal --
    bb_width = _safe_float(last.get("bb_width"))
    bb_upper = _safe_float(last.get("bb_upper"))
    bb_lower = _safe_float(last.get("bb_lower"))
    close = _safe_float(last.get("close"))

    if all(v is not None for v in (bb_width, bb_upper, bb_lower, close)):
        recent_bb_width = df["bb_width"].tail(50).dropna()
        if len(recent_bb_width) > 10:
            avg_bb = recent_bb_width.mean()
            if bb_width < avg_bb * 0.6:
                # Squeeze detected - look for breakout direction
                if close > bb_upper:
                    signals.append(
                        {
                            "strategy": "bb_squeeze_breakout",
                            "direction": "long",
                            "strength": "strong",
                            "detail": f"BB squeeze breakout UP (width={bb_width:.4f}, avg={avg_bb:.4f})",
                        }
                    )
                elif close < bb_lower:
                    signals.append(
                        {
                            "strategy": "bb_squeeze_breakout",
                            "direction": "short",
                            "strength": "strong",
                            "detail": f"BB squeeze breakout DOWN (width={bb_width:.4f}, avg={avg_bb:.4f})",
                        }
                    )
                else:
                    signals.append(
                        {
                            "strategy": "bb_squeeze",
                            "direction": "neutral",
                            "strength": "weak",
                            "detail": f"BB squeeze forming (width={bb_width:.4f}, avg={avg_bb:.4f}). Breakout imminent.",
                        }
                    )

    # -- Consensus --
    long_count = sum(1 for s in signals if s["direction"] == "long")
    short_count = sum(1 for s in signals if s["direction"] == "short")
    strong_long = sum(1 for s in signals if s["direction"] == "long" and s["strength"] == "strong")
    strong_short = sum(
        1 for s in signals if s["direction"] == "short" and s["strength"] == "strong"
    )

    if strong_long >= 2 and long_count > short_count:
        consensus = "strong_long"
    elif strong_short >= 2 and short_count > long_count:
        consensus = "strong_short"
    elif long_count > short_count + 1:
        consensus = "long"
    elif short_count > long_count + 1:
        consensus = "short"
    else:
        consensus = "neutral"

    return {
        "signals": signals,
        "consensus": consensus,
        "long_signals": long_count,
        "short_signals": short_count,
        "strong_long": strong_long,
        "strong_short": strong_short,
    }


def _find_support_resistance(df: pd.DataFrame, window: int = 20) -> dict[str, Any]:
    """
    Find support and resistance levels using pivot-point detection and
    price-cluster analysis.
    """
    if df.empty or len(df) < window * 2:
        return {"support": [], "resistance": [], "details": "Insufficient data"}

    close = df["close"].astype(float).values
    high = df["high"].astype(float).values
    low = df["low"].astype(float).values
    current_price = close[-1]

    # --- 1. Pivot-point based S/R ---
    supports = []
    resistances = []

    for i in range(window, len(df) - window):
        # Local low (support)
        if low[i] == min(low[i - window : i + window + 1]):
            supports.append(
                {
                    "price": round(float(low[i]), 6),
                    "method": "pivot_low",
                    "strength": 1,
                    "index": int(i),
                }
            )
        # Local high (resistance)
        if high[i] == max(high[i - window : i + window + 1]):
            resistances.append(
                {
                    "price": round(float(high[i]), 6),
                    "method": "pivot_high",
                    "strength": 1,
                    "index": int(i),
                }
            )

    # --- 2. Cluster similar levels ---
    def _cluster_levels(levels: list[dict[str, Any]], tolerance_pct: float = 0.005) -> list[dict[str, Any]]:
        """Merge nearby price levels into clusters."""
        if not levels:
            return []
        sorted_levels = sorted(levels, key=lambda x: x["price"])
        clusters: list[dict[str, Any]] = []
        current_cluster = [sorted_levels[0]]

        for lvl in sorted_levels[1:]:
            cluster_avg = sum(l["price"] for l in current_cluster) / len(current_cluster)
            if abs(lvl["price"] - cluster_avg) / cluster_avg < tolerance_pct:
                current_cluster.append(lvl)
            else:
                avg_price = sum(l["price"] for l in current_cluster) / len(current_cluster)
                clusters.append(
                    {
                        "price": round(avg_price, 6),
                        "strength": len(current_cluster),
                        "touches": len(current_cluster),
                        "method": "cluster",
                    }
                )
                current_cluster = [lvl]

        if current_cluster:
            avg_price = sum(l["price"] for l in current_cluster) / len(current_cluster)
            clusters.append(
                {
                    "price": round(avg_price, 6),
                    "strength": len(current_cluster),
                    "touches": len(current_cluster),
                    "method": "cluster",
                }
            )
        return clusters

    clustered_supports = _cluster_levels(supports)
    clustered_resistances = _cluster_levels(resistances)

    # --- 3. Add EMA-based dynamic S/R ---
    for ema_col in ("ema_50", "ema_200"):
        if ema_col in df.columns:
            ema_val = _safe_float(df[ema_col].iloc[-1])
            if ema_val is not None and ema_val > 0:
                entry = {
                    "price": round(ema_val, 6),
                    "strength": 2,
                    "touches": 0,
                    "method": f"dynamic_{ema_col}",
                }
                if ema_val < current_price:
                    clustered_supports.append(entry)
                else:
                    clustered_resistances.append(entry)

    # --- 4. Add Supertrend as dynamic S/R ---
    if "supertrend" in df.columns:
        st_val = _safe_float(df["supertrend"].iloc[-1])
        st_dir = _safe_float(df["supertrend_direction"].iloc[-1])
        if st_val is not None and st_val > 0:
            entry = {
                "price": round(st_val, 6),
                "strength": 2,
                "touches": 0,
                "method": "dynamic_supertrend",
            }
            if st_dir and st_dir > 0:
                clustered_supports.append(entry)
            else:
                clustered_resistances.append(entry)

    # Sort and limit
    clustered_supports = sorted(clustered_supports, key=lambda x: -x["strength"])[:10]
    clustered_resistances = sorted(clustered_resistances, key=lambda x: -x["strength"])[:10]

    # Filter: supports below current price, resistances above
    filtered_supports = [s for s in clustered_supports if s["price"] < current_price]
    filtered_resistances = [r for r in clustered_resistances if r["price"] > current_price]

    # Nearest levels
    nearest_support = max(filtered_supports, key=lambda x: x["price"]) if filtered_supports else None
    nearest_resistance = (
        min(filtered_resistances, key=lambda x: x["price"]) if filtered_resistances else None
    )

    return {
        "current_price": round(current_price, 6),
        "support_levels": filtered_supports,
        "resistance_levels": filtered_resistances,
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
        "total_supports": len(filtered_supports),
        "total_resistances": len(filtered_resistances),
    }


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

_TOOL_DEFINITIONS: list[Tool] = [
    Tool(
        name="analyze_market",
        description=(
            "Run a full technical analysis on a symbol: all indicators, market regime, "
            "strategy signals, support/resistance levels, and a summary recommendation."
        ),
        inputSchema=AnalyzeMarketInput.model_json_schema(),
    ),
    Tool(
        name="get_regime",
        description=(
            "Detect the current market regime (strong_trend, moderate_trend, ranging, "
            "volatile, quiet) with confidence score and supporting indicators."
        ),
        inputSchema=GetRegimeInput.model_json_schema(),
    ),
    Tool(
        name="get_signals",
        description=(
            "Generate trading signals from all strategies (EMA crossover, RSI, MACD, "
            "Supertrend, BB squeeze) with direction and consensus."
        ),
        inputSchema=GetSignalsInput.model_json_schema(),
    ),
    Tool(
        name="get_support_resistance",
        description=(
            "Detect support and resistance levels using pivot points, price clustering, "
            "and dynamic levels from EMAs and Supertrend."
        ),
        inputSchema=GetSupportResistanceInput.model_json_schema(),
    ),
]


def create_analysis_server() -> Server:
    """Create and configure the Analysis MCP server."""
    server = Server("analysis-tools")

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


async def _dispatch_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Route tool calls to implementations."""
    handlers = {
        "analyze_market": _handle_analyze_market,
        "get_regime": _handle_get_regime,
        "get_signals": _handle_get_signals,
        "get_support_resistance": _handle_get_support_resistance,
    }
    handler = handlers.get(name)
    if handler is None:
        raise ValueError(f"Unknown tool: {name}")
    return await handler(arguments)


async def _handle_analyze_market(arguments: dict[str, Any]) -> dict[str, Any]:
    """Full market analysis combining all sub-analyses."""
    params = AnalyzeMarketInput(**arguments)
    df = await _fetch_enriched_df(params.symbol, params.timeframe)

    if df.empty:
        return {"error": "No data available for analysis"}

    last = df.iloc[-1]

    # Extract current indicator snapshot
    indicators = {}
    indicator_cols = [
        "close", "ema_9", "ema_21", "ema_50", "ema_200",
        "rsi", "macd", "macd_signal", "macd_hist",
        "adx", "plus_di", "minus_di",
        "supertrend", "supertrend_direction",
        "bb_upper", "bb_middle", "bb_lower", "bb_width",
        "atr", "volume_sma",
    ]
    for col in indicator_cols:
        if col in last.index:
            indicators[col] = _safe_float(last[col])

    # Sub-analyses
    regime = _detect_regime(df)
    signals = _generate_signals(df)
    sr_levels = _find_support_resistance(df)

    # Summary recommendation
    rec_direction = signals["consensus"]
    rec_confidence = regime["confidence"]

    if regime["regime"] in ("quiet", "ranging") and rec_direction not in (
        "strong_long",
        "strong_short",
    ):
        recommendation = "WAIT - Market is ranging/quiet, avoid entries unless strong signal."
    elif rec_direction in ("strong_long", "strong_short"):
        recommendation = (
            f"ENTRY {rec_direction.upper()} - Strong multi-signal consensus in "
            f"{regime['regime']} regime ({rec_confidence}% confidence)."
        )
    elif rec_direction in ("long", "short"):
        recommendation = (
            f"CONSIDER {rec_direction.upper()} - Moderate signal in "
            f"{regime['regime']} regime ({rec_confidence}% confidence). "
            "Verify with risk manager."
        )
    else:
        recommendation = "NEUTRAL - Mixed signals. Wait for clearer setup."

    result = {
        "symbol": params.symbol,
        "timeframe": params.timeframe,
        "candle_count": len(df),
        "indicators": indicators,
        "regime": regime,
        "signals": signals,
        "support_resistance": sr_levels,
        "recommendation": recommendation,
        "timestamp": _utc_now_iso(),
    }
    logger.info(
        "Full analysis for %s: regime=%s, consensus=%s",
        params.symbol,
        regime["regime"],
        signals["consensus"],
    )
    return result


async def _handle_get_regime(arguments: dict[str, Any]) -> dict[str, Any]:
    """Detect current market regime."""
    params = GetRegimeInput(**arguments)
    df = await _fetch_enriched_df(params.symbol, params.timeframe)

    regime = _detect_regime(df)
    regime["symbol"] = params.symbol
    regime["timeframe"] = params.timeframe
    regime["timestamp"] = _utc_now_iso()

    logger.info("Regime for %s: %s (%d%% confidence)", params.symbol, regime["regime"], regime["confidence"])
    return regime


async def _handle_get_signals(arguments: dict[str, Any]) -> dict[str, Any]:
    """Generate strategy signals."""
    params = GetSignalsInput(**arguments)
    df = await _fetch_enriched_df(params.symbol, params.timeframe)

    signals = _generate_signals(df)
    signals["symbol"] = params.symbol
    signals["timeframe"] = params.timeframe
    signals["timestamp"] = _utc_now_iso()

    logger.info("Signals for %s: consensus=%s", params.symbol, signals["consensus"])
    return signals


async def _handle_get_support_resistance(arguments: dict[str, Any]) -> dict[str, Any]:
    """Find support and resistance levels."""
    params = GetSupportResistanceInput(**arguments)
    # Use more candles for S/R on higher timeframes
    limit = 500 if params.timeframe in ("1h", "4h", "1d") else 300
    df = await _fetch_enriched_df(params.symbol, params.timeframe, limit=limit)

    sr = _find_support_resistance(df)
    sr["symbol"] = params.symbol
    sr["timeframe"] = params.timeframe
    sr["timestamp"] = _utc_now_iso()

    logger.info(
        "S/R for %s: %d supports, %d resistances",
        params.symbol,
        sr["total_supports"],
        sr["total_resistances"],
    )
    return sr


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    """Run the Analysis MCP server over stdio."""
    server = create_analysis_server()
    logger.info("Starting Analysis MCP server...")
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        await _close_market_client()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    asyncio.run(main())
