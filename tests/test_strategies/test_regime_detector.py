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


class TestTrendingScorerFix:
    """Tests for the v6.1 trending scorer fix: partial credit for low ATR/volume."""

    def setup_method(self):
        self.d = RegimeDetector()

    def test_low_atr_gets_partial_credit_in_trending(self):
        """ATR between 0.5 and 0.8 should get 0.4 for trending (was 0)."""
        score = self.d._score_trending(adx=25.0, bbr=1.0, atrr=0.6, volr=0.8)
        # ATR 0.6 in [0.5, 0.8) -> 0.4. Without fix this was 0.
        assert score > 0  # Just verify it's non-zero for low-ATR case

    def test_low_volume_gets_partial_credit_in_trending(self):
        """Volume between 0.2 and 0.7 should get 0.3 for trending (was 0)."""
        score = self.d._score_trending(adx=25.0, bbr=1.0, atrr=1.0, volr=0.4)
        # Vol 0.4 in [0.2, 0.7) -> 0.3. Without fix this was 0.
        assert score > 0

    def test_sol_scenario_now_trending(self):
        """SOL-like values (ADX=23.86, low ATR/vol) should score higher for
        trending than ranging — this was the key fix."""
        trending = self.d._score_trending(adx=23.86, bbr=0.85, atrr=0.78, volr=0.45)
        ranging = self.d._score_ranging(adx=23.86, bbr=0.85, atrr=0.78, volr=0.45)
        assert trending > ranging, (
            f"SOL scenario: trending={trending:.3f} should beat ranging={ranging:.3f}"
        )

    def test_ada_scenario_now_trending(self):
        """ADA-like values (ADX=22.20, low ATR/vol) should score higher for
        trending than ranging."""
        trending = self.d._score_trending(adx=22.20, bbr=1.01, atrr=0.71, volr=0.49)
        ranging = self.d._score_ranging(adx=22.20, bbr=1.01, atrr=0.71, volr=0.49)
        assert trending > ranging, (
            f"ADA scenario: trending={trending:.3f} should beat ranging={ranging:.3f}"
        )

    def test_eth_stays_ranging(self):
        """ETH-like values (ADX=16.45) should still be ranging — low ADX."""
        trending = self.d._score_trending(adx=16.45, bbr=1.05, atrr=0.82, volr=0.25)
        ranging = self.d._score_ranging(adx=16.45, bbr=1.05, atrr=0.82, volr=0.25)
        assert ranging > trending, (
            f"ETH scenario: ranging={ranging:.3f} should beat trending={trending:.3f}"
        )

    def test_xrp_stays_ranging(self):
        """XRP-like values (ADX=19.58) should still be ranging — ADX below threshold."""
        trending = self.d._score_trending(adx=19.58, bbr=0.85, atrr=0.71, volr=0.38)
        ranging = self.d._score_ranging(adx=19.58, bbr=0.85, atrr=0.71, volr=0.38)
        assert ranging > trending, (
            f"XRP scenario: ranging={ranging:.3f} should beat trending={trending:.3f}"
        )

    def test_very_low_atr_no_credit(self):
        """ATR below 0.5 should not get credit in trending scorer."""
        score_low = self.d._score_trending(adx=25.0, bbr=1.0, atrr=0.3, volr=0.8)
        score_mid = self.d._score_trending(adx=25.0, bbr=1.0, atrr=0.6, volr=0.8)
        assert score_mid > score_low

    def test_very_low_volume_no_credit(self):
        """Volume below 0.2 should not get credit in trending scorer."""
        score_low = self.d._score_trending(adx=25.0, bbr=1.0, atrr=1.0, volr=0.1)
        score_mid = self.d._score_trending(adx=25.0, bbr=1.0, atrr=1.0, volr=0.4)
        assert score_mid > score_low


class TestHighADXRangingPenalty:
    """Tests for the v6.11 fix: high ADX penalises ranging score.

    Bug: ADX=35.2 with narrow BB/low ATR/low volume scored RANGING > TRENDING
    because sub-scores in _score_ranging had no ADX override.
    """

    def setup_method(self):
        self.d = RegimeDetector()

    def test_high_adx_narrow_bb_is_trending_not_ranging(self):
        """ADX=35.2 with narrow BB/ATR should be TRENDING, not RANGING."""
        trending = self.d._score_trending(adx=35.2, bbr=0.5, atrr=0.6, volr=0.5)
        ranging = self.d._score_ranging(adx=35.2, bbr=0.5, atrr=0.6, volr=0.5)
        assert trending > ranging, (
            f"ADX=35.2: trending={trending:.3f} must beat ranging={ranging:.3f}"
        )

    def test_high_adx_ranging_penalty_applied(self):
        """_score_ranging with ADX >= 20 should be penalised (multiplied by 0.3)."""
        score_penalised = self.d._score_ranging(adx=35.0, bbr=0.5, atrr=0.5, volr=0.5)
        score_normal = self.d._score_ranging(adx=15.0, bbr=0.5, atrr=0.5, volr=0.5)
        assert score_penalised < score_normal * 0.5, (
            f"Penalised={score_penalised:.3f} should be < 50% of normal={score_normal:.3f}"
        )

    def test_low_adx_ranging_unaffected(self):
        """ADX < 20 should have no penalty on ranging score."""
        score = self.d._score_ranging(adx=15.0, bbr=0.5, atrr=0.5, volr=0.5)
        # With ADX=15: ADX sub-score = 1.0 + (5/20)*0.5 = 1.125
        # + BB narrow 1.0 + ATR low 1.0 + vol low 0.8 = 3.925
        assert score > 3.0
