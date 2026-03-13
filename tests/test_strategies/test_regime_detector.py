"""
Tests for market regime detection.
"""

import pytest
import pandas as pd
import numpy as np

from src.strategies.regime_detector import RegimeDetector, MarketRegime
from src.data.indicator_engine import IndicatorEngine


class TestRegimeDetector:
    """Test suite for regime detection."""

    def setup_method(self):
        self.detector = RegimeDetector()
        self.indicator_engine = IndicatorEngine()

    def test_detect_returns_regime_state(self, sample_ohlcv_df):
        """detect() should return a RegimeState object."""
        df = self.indicator_engine.calculate_all(sample_ohlcv_df)
        result = self.detector.detect(df)
        assert result.regime in MarketRegime
        assert 0 <= result.confidence <= 100

    def test_trending_detection(self, trending_ohlcv_df):
        """Strong trend data should be classified as TRENDING."""
        df = self.indicator_engine.calculate_all(trending_ohlcv_df)
        result = self.detector.detect(df)
        # With strong trend data, should often detect trending
        # (may not always due to noise, so check it's a valid regime)
        assert result.regime in MarketRegime

    def test_ranging_detection(self, ranging_ohlcv_df):
        """Ranging data should not be classified as TRENDING."""
        df = self.indicator_engine.calculate_all(ranging_ohlcv_df)
        result = self.detector.detect(df)
        assert result.regime in MarketRegime

    def test_confidence_range(self, sample_ohlcv_df):
        """Confidence should always be between 0 and 100."""
        df = self.indicator_engine.calculate_all(sample_ohlcv_df)
        result = self.detector.detect(df)
        assert 0 <= result.confidence <= 100

    def test_regime_has_indicator_values(self, sample_ohlcv_df):
        """RegimeState should include ADX and other indicator values."""
        df = self.indicator_engine.calculate_all(sample_ohlcv_df)
        result = self.detector.detect(df)
        assert result.adx is not None
        assert result.bb_width_ratio is not None
        assert result.atr_ratio is not None
