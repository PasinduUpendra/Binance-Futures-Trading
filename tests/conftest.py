"""
Shared test fixtures for Claude Quant test suite.
"""

import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pandas as pd
import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def sample_ohlcv_df() -> pd.DataFrame:
    """Generate a realistic OHLCV DataFrame for testing."""
    np.random.seed(42)
    n = 200

    # Generate somewhat realistic price data
    base_price = 43000.0
    returns = np.random.normal(0.0001, 0.005, n)
    prices = base_price * np.exp(np.cumsum(returns))

    # Generate OHLCV
    timestamps = pd.date_range(
        end=datetime.now(timezone.utc), periods=n, freq="5min"
    )

    df = pd.DataFrame({
        "timestamp": timestamps,
        "open": prices,
        "high": prices * (1 + np.abs(np.random.normal(0, 0.002, n))),
        "low": prices * (1 - np.abs(np.random.normal(0, 0.002, n))),
        "close": prices * (1 + np.random.normal(0, 0.001, n)),
        "volume": np.random.uniform(100, 1000, n),
    })

    # Ensure OHLCV integrity: Low <= Open,Close <= High
    df["high"] = df[["open", "high", "close"]].max(axis=1)
    df["low"] = df[["open", "low", "close"]].min(axis=1)

    return df


@pytest.fixture
def trending_ohlcv_df() -> pd.DataFrame:
    """Generate OHLCV data for a trending market (ADX > 25)."""
    np.random.seed(123)
    n = 200

    # Strong uptrend
    base_price = 43000.0
    trend = np.linspace(0, 0.15, n)  # 15% uptrend
    noise = np.random.normal(0, 0.002, n)
    prices = base_price * (1 + trend + noise)

    timestamps = pd.date_range(
        end=datetime.now(timezone.utc), periods=n, freq="5min"
    )

    df = pd.DataFrame({
        "timestamp": timestamps,
        "open": prices,
        "high": prices * (1 + np.abs(np.random.normal(0.001, 0.001, n))),
        "low": prices * (1 - np.abs(np.random.normal(0.001, 0.001, n))),
        "close": prices * (1 + np.random.normal(0.0005, 0.001, n)),
        "volume": np.random.uniform(500, 1500, n),  # Higher volume in trends
    })

    df["high"] = df[["open", "high", "close"]].max(axis=1)
    df["low"] = df[["open", "low", "close"]].min(axis=1)

    return df


@pytest.fixture
def ranging_ohlcv_df() -> pd.DataFrame:
    """Generate OHLCV data for a ranging market (ADX < 20)."""
    np.random.seed(456)
    n = 200

    base_price = 43000.0
    # Oscillating around base with small amplitude
    oscillation = 200 * np.sin(np.linspace(0, 8 * np.pi, n))
    noise = np.random.normal(0, 50, n)
    prices = base_price + oscillation + noise

    timestamps = pd.date_range(
        end=datetime.now(timezone.utc), periods=n, freq="5min"
    )

    df = pd.DataFrame({
        "timestamp": timestamps,
        "open": prices,
        "high": prices + np.abs(np.random.normal(30, 20, n)),
        "low": prices - np.abs(np.random.normal(30, 20, n)),
        "close": prices + np.random.normal(0, 30, n),
        "volume": np.random.uniform(50, 300, n),  # Lower volume in ranges
    })

    df["high"] = df[["open", "high", "close"]].max(axis=1)
    df["low"] = df[["open", "low", "close"]].min(axis=1)

    return df


@pytest.fixture
def mock_exchange():
    """Mock ccxt exchange for testing."""
    exchange = MagicMock()
    exchange.fetch_ohlcv = AsyncMock(return_value=[
        [1704067200000, 43000, 43100, 42900, 43050, 500],
        [1704067500000, 43050, 43150, 42950, 43100, 600],
    ])
    exchange.fetch_ticker = AsyncMock(return_value={
        "symbol": "BTC/USDT:USDT",
        "last": 43100.0,
        "high": 43500.0,
        "low": 42800.0,
        "bid": 43095.0,
        "ask": 43105.0,
        "volume": 15000.0,
    })
    exchange.fetch_balance = AsyncMock(return_value={
        "total": {"USDT": 75.0},
        "free": {"USDT": 60.0},
        "used": {"USDT": 15.0},
    })
    exchange.fetch_positions = AsyncMock(return_value=[])
    exchange.create_order = AsyncMock(return_value={
        "id": "12345",
        "status": "closed",
        "filled": 0.001,
        "average": 43100.0,
        "cost": 43.10,
        "fee": {"cost": 0.017, "currency": "USDT"},
    })
    return exchange


@pytest.fixture
def sample_trades() -> list[dict]:
    """Sample trade history for testing."""
    return [
        {
            "trade_id": "t_001",
            "timestamp": "2024-01-01T10:00:00Z",
            "symbol": "BTC/USDT:USDT",
            "direction": "long",
            "entry_price": 43000.0,
            "exit_price": 43500.0,
            "size": 0.001,
            "leverage": 5,
            "pnl": 2.50,
            "pnl_pct": 0.058,
            "strategy": "trend_follower",
            "regime": "trending",
            "confidence": 75,
        },
        {
            "trade_id": "t_002",
            "timestamp": "2024-01-01T15:00:00Z",
            "symbol": "ETH/USDT:USDT",
            "direction": "short",
            "entry_price": 2300.0,
            "exit_price": 2320.0,
            "size": 0.05,
            "leverage": 3,
            "pnl": -1.00,
            "pnl_pct": -0.029,
            "strategy": "mean_reversion",
            "regime": "ranging",
            "confidence": 55,
        },
        {
            "trade_id": "t_003",
            "timestamp": "2024-01-02T08:00:00Z",
            "symbol": "SOL/USDT:USDT",
            "direction": "long",
            "entry_price": 100.0,
            "exit_price": 103.0,
            "size": 0.5,
            "leverage": 5,
            "pnl": 7.50,
            "pnl_pct": 0.15,
            "strategy": "breakout_trader",
            "regime": "volatile",
            "confidence": 80,
        },
    ]
