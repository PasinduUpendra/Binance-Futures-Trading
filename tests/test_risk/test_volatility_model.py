"""
Tests for GARCH volatility model.
"""

import numpy as np
import pandas as pd
import pytest
from decimal import Decimal

from src.risk.volatility_model import (
    VolatilityModel,
    VolatilityState,
    _VOL_VERY_HIGH,
    _VOL_HIGH,
    _VOL_ELEVATED,
    _SCALE_VERY_HIGH,
    _SCALE_HIGH,
    _SCALE_ELEVATED,
    _SCALE_NORMAL,
    _SCALE_LOW,
    _SCALE_VERY_LOW,
)


def _make_price_df(n: int = 200, base: float = 100.0, volatility: float = 0.02) -> pd.DataFrame:
    """Generate synthetic OHLCV data with controlled volatility."""
    np.random.seed(42)
    returns = np.random.normal(0, volatility, n)
    close = base * np.exp(np.cumsum(returns))
    high = close * (1 + np.abs(np.random.normal(0, volatility / 2, n)))
    low = close * (1 - np.abs(np.random.normal(0, volatility / 2, n)))
    open_ = close * (1 + np.random.normal(0, volatility / 4, n))
    volume = np.random.uniform(1000, 5000, n)

    return pd.DataFrame({
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    })


class TestVolatilityModel:
    """Test GARCH volatility model."""

    def setup_method(self):
        self.model = VolatilityModel(forecast_horizon=1)

    def test_forecast_returns_state(self):
        """Forecast should return a VolatilityState with valid fields."""
        df = _make_price_df(200)
        state = self.model.forecast_simple(df)
        assert state is not None
        assert state.current_vol > 0
        assert state.mean_vol > 0
        assert state.vol_ratio > 0
        assert state.leverage_scale > 0
        assert state.observations_used >= 100

    def test_forecast_insufficient_data(self):
        """Should return None with too few observations."""
        df = _make_price_df(50)
        state = self.model.forecast(df)
        assert state is None

    def test_forecast_no_close_column(self):
        """Should return None if close column missing."""
        df = pd.DataFrame({"price": [100, 101, 102]})
        state = self.model.forecast(df)
        assert state is None

    def test_ewma_fallback(self):
        """EWMA fallback should work when GARCH fails."""
        df = _make_price_df(200)
        state = self.model.forecast_simple(df)
        assert state is not None
        assert state.forecast_horizon == 1

    def test_vol_ratio_scaling_very_high(self):
        """Very high vol ratio -> very low leverage scale."""
        scale = VolatilityModel._vol_ratio_to_scale(Decimal("2.5"))
        assert scale == _SCALE_VERY_HIGH  # 0.25

    def test_vol_ratio_scaling_high(self):
        """High vol ratio -> low leverage scale."""
        scale = VolatilityModel._vol_ratio_to_scale(Decimal("1.6"))
        assert scale == _SCALE_HIGH  # 0.50

    def test_vol_ratio_scaling_elevated(self):
        """Elevated vol ratio -> reduced leverage scale."""
        scale = VolatilityModel._vol_ratio_to_scale(Decimal("1.3"))
        assert scale == _SCALE_ELEVATED  # 0.75

    def test_vol_ratio_scaling_normal(self):
        """Normal vol ratio -> no adjustment."""
        scale = VolatilityModel._vol_ratio_to_scale(Decimal("1.0"))
        assert scale == _SCALE_NORMAL  # 1.00

    def test_vol_ratio_scaling_low(self):
        """Low vol ratio -> slight boost."""
        scale = VolatilityModel._vol_ratio_to_scale(Decimal("0.6"))
        assert scale == _SCALE_LOW  # 1.15

    def test_vol_ratio_scaling_very_low(self):
        """Very low vol ratio -> max boost."""
        scale = VolatilityModel._vol_ratio_to_scale(Decimal("0.4"))
        assert scale == _SCALE_VERY_LOW  # 1.25

    def test_adjust_leverage_reduces_on_high_vol(self):
        """High vol should reduce leverage."""
        state = VolatilityState(
            current_vol=0.05, mean_vol=0.025,
            vol_ratio=Decimal("2.0"), leverage_scale=_SCALE_VERY_HIGH,
            forecast_horizon=1, observations_used=200,
        )
        adjusted = VolatilityModel.adjust_leverage(8, state, max_leverage=10)
        assert adjusted == 2  # 8 * 0.25 = 2

    def test_adjust_leverage_no_change_normal_vol(self):
        """Normal vol should not change leverage."""
        state = VolatilityState(
            current_vol=0.02, mean_vol=0.02,
            vol_ratio=Decimal("1.0"), leverage_scale=_SCALE_NORMAL,
            forecast_horizon=1, observations_used=200,
        )
        adjusted = VolatilityModel.adjust_leverage(5, state, max_leverage=10)
        assert adjusted == 5

    def test_adjust_leverage_none_state_passthrough(self):
        """None vol_state should pass through requested leverage."""
        adjusted = VolatilityModel.adjust_leverage(7, None, max_leverage=10)
        assert adjusted == 7

    def test_adjust_leverage_respects_max(self):
        """Even with boost, should not exceed max_leverage."""
        state = VolatilityState(
            current_vol=0.01, mean_vol=0.04,
            vol_ratio=Decimal("0.3"), leverage_scale=_SCALE_VERY_LOW,
            forecast_horizon=1, observations_used=200,
        )
        adjusted = VolatilityModel.adjust_leverage(9, state, max_leverage=10)
        assert adjusted <= 10

    def test_adjust_leverage_minimum_one(self):
        """Adjusted leverage should never go below 1."""
        state = VolatilityState(
            current_vol=0.10, mean_vol=0.02,
            vol_ratio=Decimal("5.0"), leverage_scale=_SCALE_VERY_HIGH,
            forecast_horizon=1, observations_used=200,
        )
        adjusted = VolatilityModel.adjust_leverage(1, state, max_leverage=10)
        assert adjusted >= 1
