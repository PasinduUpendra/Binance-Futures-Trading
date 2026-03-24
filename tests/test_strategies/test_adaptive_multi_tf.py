"""
Tests for multi-timeframe methods in AdaptiveStrategy.

Covers:
- select_strategy() routing based on RegimeState
- check_supertrend_reversal() exit logic
- get_signal_multi_tf() integration (implicitly via strategy routing)
- Convenience accessor for supertrend_trend property
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
import pytest

from src.strategies.adaptive_strategy import AdaptiveStrategy
from src.strategies.base_strategy import SignalDirection
from src.strategies.breakout_trader import BreakoutTrader
from src.strategies.mean_reversion import MeanReversion
from src.strategies.regime_detector import MarketRegime, RegimeState
from src.strategies.supertrend_trend import SupertrendTrend
from src.strategies.trend_follower import TrendFollower


# ---------------------------------------------------------------------------
# Helper: build indicator-enriched DataFrames
# ---------------------------------------------------------------------------

INDICATOR_COLUMNS = [
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "ema_9",
    "ema_21",
    "ema_50",
    "ema_200",
    "rsi",
    "macd",
    "macd_signal",
    "macd_hist",
    "adx",
    "plus_di",
    "minus_di",
    "supertrend",
    "supertrend_direction",
    "bb_upper",
    "bb_middle",
    "bb_lower",
    "bb_width",
    "atr",
    "volume_sma",
    "zscore",
]


def _make_indicator_df(
    n: int = 200,
    adx: float = 30.0,
    supertrend_dirs: Optional[list[float]] = None,
    close: float = 3000.0,
    rsi: float = 50.0,
    atr: float = 50.0,
    bb_width: float = 0.04,
    volume: float = 1000.0,
    ema_9: Optional[float] = None,
    ema_21: Optional[float] = None,
) -> pd.DataFrame:
    """Generate a DataFrame with all 26 indicator columns and controlled values.

    Parameters
    ----------
    n : int
        Number of rows.
    adx : float
        Constant ADX value across all rows.
    supertrend_dirs : list[float] or None
        If provided, must be length *n*.  Otherwise defaults to all 1.0.
    close : float
        Base close price.  A small linear drift is added.
    rsi : float
        Constant RSI value.
    atr : float
        Constant ATR value.
    bb_width : float
        Fractional BB width as ratio of close (e.g., 0.04 = 4%).
    volume : float
        Constant volume value.
    ema_9 : float or None
        If None, defaults to ``close + 5``.
    ema_21 : float or None
        If None, defaults to ``close - 5``.
    """
    if supertrend_dirs is None:
        supertrend_dirs = [1.0] * n
    assert len(supertrend_dirs) == n, "supertrend_dirs length must equal n"

    if ema_9 is None:
        ema_9 = close + 5.0
    if ema_21 is None:
        ema_21 = close - 5.0

    # Small linear drift to avoid perfectly flat data
    drift = np.linspace(0, 10, n)
    closes = close + drift

    bb_half = close * bb_width / 2.0
    bb_upper_val = close + bb_half
    bb_lower_val = close - bb_half
    bb_middle_val = close

    timestamps = pd.date_range("2025-01-01", periods=n, freq="4h")

    data = {
        "timestamp": timestamps,
        "open": closes - 1.0,
        "high": closes + 20.0,
        "low": closes - 20.0,
        "close": closes,
        "volume": [volume] * n,
        "ema_9": [ema_9] * n,
        "ema_21": [ema_21] * n,
        "ema_50": [close - 20.0] * n,
        "ema_200": [close - 50.0] * n,
        "rsi": [rsi] * n,
        "macd": [1.0] * n,
        "macd_signal": [0.5] * n,
        "macd_hist": [0.5] * n,
        "adx": [adx] * n,
        "plus_di": [20.0] * n,
        "minus_di": [15.0] * n,
        "supertrend": [close - 30.0] * n,
        "supertrend_direction": supertrend_dirs,
        "bb_upper": [bb_upper_val] * n,
        "bb_middle": [bb_middle_val] * n,
        "bb_lower": [bb_lower_val] * n,
        "bb_width": [bb_width] * n,
        "atr": [atr] * n,
        "volume_sma": [volume] * n,
        "zscore": [0.0] * n,
    }
    return pd.DataFrame(data)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def adaptive() -> AdaptiveStrategy:
    return AdaptiveStrategy()


# ---------------------------------------------------------------------------
# 1. TRENDING ADX >= 18 -> SupertrendTrend
# ---------------------------------------------------------------------------


def test_trending_routes_to_supertrend(adaptive: AdaptiveStrategy) -> None:
    regime = RegimeState(
        regime=MarketRegime.TRENDING,
        confidence=75.0,
        adx=30.0,
        bb_width_ratio=1.0,
        atr_ratio=1.0,
        volume_ratio=1.0,
    )
    strategy = adaptive.select_strategy(regime)
    assert isinstance(strategy, SupertrendTrend)


# ---------------------------------------------------------------------------
# 2. TRENDING ADX < 18 -> TrendFollower (fallback)
# ---------------------------------------------------------------------------


def test_trending_low_adx_routes_to_none(adaptive: AdaptiveStrategy) -> None:
    """v4 fix: TrendFollower disabled (30% WR) — low ADX trending returns None."""
    regime = RegimeState(
        regime=MarketRegime.TRENDING,
        confidence=60.0,
        adx=15.0,
        bb_width_ratio=1.0,
        atr_ratio=1.0,
        volume_ratio=1.0,
    )
    strategy = adaptive.select_strategy(regime)
    assert strategy is None


# ---------------------------------------------------------------------------
# 3. RANGING -> MeanReversion
# ---------------------------------------------------------------------------


def test_ranging_routes_to_mean_reversion(adaptive: AdaptiveStrategy) -> None:
    """RANGING regime returns None (MeanReversion disabled: 25% WR paper, 5.3% historical)."""
    regime = RegimeState(
        regime=MarketRegime.RANGING,
        confidence=70.0,
        adx=18.0,
        bb_width_ratio=0.7,
        atr_ratio=0.8,
        volume_ratio=0.6,
    )
    strategy = adaptive.select_strategy(regime)
    assert strategy is None


# ---------------------------------------------------------------------------
# 4. VOLATILE + BB expanding -> BreakoutTrader
# ---------------------------------------------------------------------------


def test_volatile_routes_to_breakout_trader(adaptive: AdaptiveStrategy) -> None:
    """VOLATILE regime returns None (BreakoutTrader disabled: 23.9% WR, negative EV)."""
    regime = RegimeState(
        regime=MarketRegime.VOLATILE,
        confidence=65.0,
        adx=22.0,
        bb_width_ratio=1.2,
        atr_ratio=1.3,
        volume_ratio=1.5,
    )
    strategy = adaptive.select_strategy(regime)
    assert strategy is None


# ---------------------------------------------------------------------------
# 5. QUIET -> None
# ---------------------------------------------------------------------------


def test_quiet_routes_to_none(adaptive: AdaptiveStrategy) -> None:
    regime = RegimeState(
        regime=MarketRegime.QUIET,
        confidence=80.0,
        adx=10.0,
        bb_width_ratio=0.4,
        atr_ratio=0.4,
        volume_ratio=0.4,
    )
    strategy = adaptive.select_strategy(regime)
    assert strategy is None


# ---------------------------------------------------------------------------
# 6. Supertrend reversal: long position + bearish flip -> True
# ---------------------------------------------------------------------------


def test_supertrend_reversal_long_bearish_flip(adaptive: AdaptiveStrategy) -> None:
    # Last supertrend_direction is -1 (bearish) while position is long -> exit
    st_dirs = [1.0] * 199 + [-1.0]
    df_4h = _make_indicator_df(n=200, supertrend_dirs=st_dirs)
    result = adaptive.check_supertrend_reversal(df_4h, position_direction="long")
    assert result is True


# ---------------------------------------------------------------------------
# 7. Supertrend reversal: short position + bullish flip -> True
# ---------------------------------------------------------------------------


def test_supertrend_reversal_short_bullish_flip(adaptive: AdaptiveStrategy) -> None:
    # Last supertrend_direction is 1 (bullish) while position is short -> exit
    st_dirs = [-1.0] * 199 + [1.0]
    df_4h = _make_indicator_df(n=200, supertrend_dirs=st_dirs)
    result = adaptive.check_supertrend_reversal(df_4h, position_direction="short")
    assert result is True


# ---------------------------------------------------------------------------
# 8. Supertrend reversal: no flip (same direction) -> False
# ---------------------------------------------------------------------------


def test_supertrend_reversal_no_flip(adaptive: AdaptiveStrategy) -> None:
    # supertrend_direction is 1 (bullish) and position is long -> no exit
    st_dirs = [1.0] * 200
    df_4h = _make_indicator_df(n=200, supertrend_dirs=st_dirs)
    result = adaptive.check_supertrend_reversal(df_4h, position_direction="long")
    assert result is False


# ---------------------------------------------------------------------------
# 9. Supertrend reversal: missing column -> False (graceful)
# ---------------------------------------------------------------------------


def test_supertrend_reversal_missing_column(adaptive: AdaptiveStrategy) -> None:
    df_4h = _make_indicator_df(n=200)
    df_4h = df_4h.drop(columns=["supertrend_direction"])
    result = adaptive.check_supertrend_reversal(df_4h, position_direction="long")
    assert result is False


# ---------------------------------------------------------------------------
# 10. supertrend_trend property returns SupertrendTrend instance
# ---------------------------------------------------------------------------


def test_has_supertrend_trend_accessor(adaptive: AdaptiveStrategy) -> None:
    assert isinstance(adaptive.supertrend_trend, SupertrendTrend)
