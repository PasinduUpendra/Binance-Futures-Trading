"""
Integration tests for the full trading pipeline.
"""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from src.data.indicator_engine import IndicatorEngine
from src.data.data_validator import DataValidator
from src.strategies.regime_detector import RegimeDetector, MarketRegime
from src.strategies.adaptive_strategy import AdaptiveStrategy
from src.risk.circuit_breaker import CircuitBreaker, CircuitBreakerLevel


class TestFullPipeline:
    """Integration tests for data -> analysis -> risk -> decision pipeline."""

    def setup_method(self):
        self.indicator_engine = IndicatorEngine()
        self.data_validator = DataValidator()
        self.regime_detector = RegimeDetector()
        self.adaptive_strategy = AdaptiveStrategy()

    def test_data_to_indicators(self, sample_ohlcv_df):
        """OHLCV data should produce valid indicators."""
        df = self.indicator_engine.calculate_all(sample_ohlcv_df)

        # Check key indicator columns exist
        expected_columns = [
            "ema_9", "ema_21", "ema_50", "ema_200",
            "rsi", "macd", "adx", "atr",
            "bb_upper", "bb_middle", "bb_lower",
            "zscore",
        ]
        for col in expected_columns:
            assert col in df.columns, f"Missing indicator column: {col}"

    def test_indicators_to_regime(self, sample_ohlcv_df):
        """Indicators should produce a valid regime classification."""
        df = self.indicator_engine.calculate_all(sample_ohlcv_df)
        regime = self.regime_detector.detect(df)

        assert regime.regime in MarketRegime
        assert 0 <= regime.confidence <= 100

    def test_regime_to_signal(self, sample_ohlcv_df):
        """Regime should map to appropriate strategy and generate valid signal."""
        df = self.indicator_engine.calculate_all(sample_ohlcv_df)
        signal = self.adaptive_strategy.get_signal(df)

        # Signal can be None (no trade), which is valid
        if signal is not None:
            assert signal.direction.value in ("long", "short")
            assert signal.stop_loss is not None
            assert signal.take_profit is not None
            assert signal.confidence >= 40

    def test_circuit_breaker_levels(self):
        """Circuit breaker should return correct levels."""
        assert CircuitBreaker.check_level(Decimal("75")) == CircuitBreakerLevel.GREEN
        assert CircuitBreaker.check_level(Decimal("52")) == CircuitBreakerLevel.YELLOW
        assert CircuitBreaker.check_level(Decimal("37")) == CircuitBreakerLevel.RED
        assert CircuitBreaker.check_level(Decimal("20")) == CircuitBreakerLevel.DEAD

    def test_dead_circuit_breaker_blocks_all(self):
        """DEAD circuit breaker should prevent all trading."""
        state = CircuitBreaker.is_trading_allowed(Decimal("20"))
        assert state.constraints.trading_allowed is False

    def test_data_validation_pipeline(self, sample_ohlcv_df):
        """Data validator should pass clean data."""
        validation = self.data_validator.validate_ohlcv(sample_ohlcv_df)
        assert validation.passed is True

    @pytest.mark.integration
    def test_full_pipeline_no_crashes(self, sample_ohlcv_df):
        """Full pipeline should complete without exceptions."""
        # Validate data
        validation = self.data_validator.validate_ohlcv(sample_ohlcv_df)
        assert validation.passed

        # Calculate indicators
        df = self.indicator_engine.calculate_all(sample_ohlcv_df)

        # Detect regime
        regime = self.regime_detector.detect(df)

        # Get signal
        signal = self.adaptive_strategy.get_signal(df)

        # Check circuit breaker
        level = CircuitBreaker.check_level(Decimal("75"))
        assert level == CircuitBreakerLevel.GREEN
