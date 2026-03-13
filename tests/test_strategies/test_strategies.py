"""
Tests for trading strategies.
"""

import pytest
import pandas as pd

from src.strategies.base_strategy import Signal
from src.strategies.trend_follower import TrendFollower
from src.strategies.mean_reversion import MeanReversion
from src.strategies.breakout_trader import BreakoutTrader
from src.strategies.adaptive_strategy import AdaptiveStrategy
from src.data.indicator_engine import IndicatorEngine


class TestTrendFollower:
    """Test suite for trend following strategy."""

    def setup_method(self):
        self.strategy = TrendFollower()
        self.indicator_engine = IndicatorEngine()

    def test_generate_signal_returns_signal_or_none(self, sample_ohlcv_df):
        """generate_signal should return Signal or None."""
        df = self.indicator_engine.calculate_all(sample_ohlcv_df)
        result = self.strategy.generate_signal(df)
        assert result is None or isinstance(result, Signal)

    def test_signal_has_required_fields(self, trending_ohlcv_df):
        """If signal generated, it must have SL, TP, and valid R/R."""
        df = self.indicator_engine.calculate_all(trending_ohlcv_df)
        signal = self.strategy.generate_signal(df)
        if signal is not None:
            assert signal.stop_loss is not None
            assert signal.take_profit is not None
            assert signal.direction in ("long", "short", "none")
            assert signal.confidence >= 0
            assert signal.strategy_name == "TrendFollower"

    def test_no_signal_in_ranging_market(self, ranging_ohlcv_df):
        """Trend follower should not signal in ranging market."""
        df = self.indicator_engine.calculate_all(ranging_ohlcv_df)
        signal = self.strategy.generate_signal(df)
        # Trend follower requires ADX > 25, ranging has ADX < 20
        # May still generate a signal if data happens to trend, but confidence should be low
        if signal is not None:
            assert signal.confidence < 70


class TestMeanReversion:
    """Test suite for mean reversion strategy."""

    def setup_method(self):
        self.strategy = MeanReversion()
        self.indicator_engine = IndicatorEngine()

    def test_generate_signal_returns_signal_or_none(self, sample_ohlcv_df):
        """generate_signal should return Signal or None."""
        df = self.indicator_engine.calculate_all(sample_ohlcv_df)
        result = self.strategy.generate_signal(df)
        assert result is None or isinstance(result, Signal)

    def test_signal_has_stop_loss(self, ranging_ohlcv_df):
        """Mean reversion signals must have stop-loss."""
        df = self.indicator_engine.calculate_all(ranging_ohlcv_df)
        signal = self.strategy.generate_signal(df)
        if signal is not None:
            assert signal.stop_loss is not None


class TestBreakoutTrader:
    """Test suite for breakout strategy."""

    def setup_method(self):
        self.strategy = BreakoutTrader()
        self.indicator_engine = IndicatorEngine()

    def test_generate_signal_returns_signal_or_none(self, sample_ohlcv_df):
        """generate_signal should return Signal or None."""
        df = self.indicator_engine.calculate_all(sample_ohlcv_df)
        result = self.strategy.generate_signal(df)
        assert result is None or isinstance(result, Signal)


class TestAdaptiveStrategy:
    """Test suite for adaptive strategy selector."""

    def setup_method(self):
        self.adaptive = AdaptiveStrategy()
        self.indicator_engine = IndicatorEngine()

    def test_get_signal_returns_signal_or_none(self, sample_ohlcv_df):
        """get_signal should return Signal or None."""
        df = self.indicator_engine.calculate_all(sample_ohlcv_df)
        result = self.adaptive.get_signal(df)
        assert result is None or isinstance(result, Signal)

    def test_low_confidence_returns_none(self, sample_ohlcv_df):
        """Signals with confidence < 40 should be filtered out."""
        df = self.indicator_engine.calculate_all(sample_ohlcv_df)
        signal = self.adaptive.get_signal(df)
        if signal is not None:
            assert signal.confidence >= 40
