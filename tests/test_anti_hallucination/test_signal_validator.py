"""
Tests for anti-hallucination signal validator (Layer 3).
"""

import pytest
from decimal import Decimal

from src.anti_hallucination.signal_validator import (
    SignalValidator,
    SignalValidation,
    TradingSignal,
)


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _make_signal(**overrides):
    defaults = {
        "signal_id": "sig_001",
        "symbol": "ETH/USDT:USDT",
        "direction": "long",
        "strategy": "SupertrendTrend",
        "entry_price": Decimal("3450"),
        "stop_loss": Decimal("3300"),
        "take_profit": Decimal("3750"),
        "leverage": 5,
        "confidence": 75.0,
        "indicators": {"RSI": 65.0, "ADX": 30.0},
    }
    defaults.update(overrides)
    return TradingSignal(**defaults)


def _make_raw_data(**overrides):
    defaults = {
        "bid": Decimal("3449"),
        "ask": Decimal("3451"),
        "indicators": {"RSI": 65.0, "ADX": 30.0},
        "candles": [{"close": 3450.0}],
    }
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# TestValidateSignal
# ---------------------------------------------------------------------------


class TestValidateSignal:
    """Top-level validate_signal integration tests."""

    def test_valid_signal_with_matching_indicators(self):
        """Valid signal with matching indicators returns valid=True, no issues."""
        validator = SignalValidator()
        signal = _make_signal()
        raw_data = _make_raw_data()

        result = validator.validate_signal(signal, raw_data)

        assert result.valid is True
        assert result.issues == []

    def test_empty_indicators_invalid(self):
        """Signal with empty indicators dict is rejected."""
        validator = SignalValidator()
        signal = _make_signal(indicators={})
        raw_data = _make_raw_data()

        result = validator.validate_signal(signal, raw_data)

        assert result.valid is False
        assert any("no indicator" in issue.lower() for issue in result.issues)

    def test_vague_string_indicator_invalid(self):
        """Signal with vague string indicator ('bullish') is rejected."""
        validator = SignalValidator()
        signal = _make_signal(indicators={"RSI": "bullish"})
        raw_data = _make_raw_data()

        result = validator.validate_signal(signal, raw_data)

        assert result.valid is False
        assert any("vague" in issue.lower() for issue in result.issues)


# ---------------------------------------------------------------------------
# TestIndicatorValues
# ---------------------------------------------------------------------------


class TestIndicatorValues:
    """Tests for indicator cross-check against raw data."""

    def test_values_within_tolerance(self):
        """Values within 0.5% tolerance pass validation."""
        validator = SignalValidator()
        # Signal says RSI=65.0, raw says 65.0 -> 0% diff -> pass
        signal = _make_signal(indicators={"RSI": 65.0})
        raw_data = _make_raw_data(indicators={"RSI": 65.0})

        result = validator.validate_signal(signal, raw_data)

        assert result.indicator_checks["RSI"] is True

    def test_values_outside_tolerance(self):
        """Values outside 0.5% tolerance fail with diff info."""
        validator = SignalValidator()
        # Signal says RSI=65.0, raw says 70.0 -> ~7.1% diff -> fail
        signal = _make_signal(indicators={"RSI": 65.0})
        raw_data = _make_raw_data(indicators={"RSI": 70.0})

        result = validator.validate_signal(signal, raw_data)

        assert result.indicator_checks["RSI"] is False
        assert any("diff" in issue.lower() for issue in result.issues)

    def test_indicator_not_in_raw_data(self):
        """Indicator missing from raw_data fails with 'not found in raw data'."""
        validator = SignalValidator()
        signal = _make_signal(indicators={"RSI": 65.0, "MACD": 1.5})
        raw_data = _make_raw_data(indicators={"RSI": 65.0})  # no MACD

        result = validator.validate_signal(signal, raw_data)

        assert result.indicator_checks["MACD"] is False
        assert any("not found in raw data" in issue.lower() for issue in result.issues)


# ---------------------------------------------------------------------------
# TestRiskReward
# ---------------------------------------------------------------------------


class TestRiskReward:
    """Tests for risk/reward ratio validation."""

    def test_long_valid_rr(self):
        """Long with good R/R (entry=100, SL=95, TP=115 -> R/R=3.0) passes."""
        validator = SignalValidator()
        signal = _make_signal(
            entry_price=Decimal("100"),
            stop_loss=Decimal("95"),
            take_profit=Decimal("115"),
        )
        raw_data = _make_raw_data(
            bid=Decimal("99.50"),
            ask=Decimal("100.50"),
            candles=[{"close": 100.0}],
        )

        result = validator.validate_signal(signal, raw_data)

        # R/R = (115-100)/(100-95) = 15/5 = 3.0 >= 2.0 -> pass
        rr_issues = [i for i in result.issues if "r/r ratio" in i.lower()]
        assert rr_issues == []

    def test_long_bad_rr(self):
        """Long with bad R/R (entry=100, SL=95, TP=104 -> R/R=0.8) fails."""
        validator = SignalValidator()
        signal = _make_signal(
            entry_price=Decimal("100"),
            stop_loss=Decimal("95"),
            take_profit=Decimal("104"),
        )
        raw_data = _make_raw_data(
            bid=Decimal("99.50"),
            ask=Decimal("100.50"),
            candles=[{"close": 100.0}],
        )

        result = validator.validate_signal(signal, raw_data)

        # R/R = (104-100)/(100-95) = 4/5 = 0.8 < 2.0 -> fail
        assert result.valid is False
        assert any("r/r ratio" in issue.lower() for issue in result.issues)

    def test_short_valid_rr(self):
        """Short with good R/R (entry=100, SL=105, TP=85 -> R/R=3.0) passes."""
        validator = SignalValidator()
        signal = _make_signal(
            direction="short",
            entry_price=Decimal("100"),
            stop_loss=Decimal("105"),
            take_profit=Decimal("85"),
        )
        raw_data = _make_raw_data(
            bid=Decimal("99.50"),
            ask=Decimal("100.50"),
            candles=[{"close": 100.0}],
        )

        result = validator.validate_signal(signal, raw_data)

        # R/R = (100-85)/(105-100) = 15/5 = 3.0 >= 2.0 -> pass
        rr_issues = [i for i in result.issues if "r/r ratio" in i.lower()]
        assert rr_issues == []

    def test_sl_zero_fails(self):
        """Stop-loss of 0 fails with 'not set'."""
        validator = SignalValidator()
        signal = _make_signal(
            entry_price=Decimal("100"),
            stop_loss=Decimal("0"),
            take_profit=Decimal("115"),
        )
        raw_data = _make_raw_data(
            bid=Decimal("99.50"),
            ask=Decimal("100.50"),
            candles=[{"close": 100.0}],
        )

        result = validator.validate_signal(signal, raw_data)

        assert result.valid is False
        assert any("not set" in issue.lower() for issue in result.issues)


# ---------------------------------------------------------------------------
# TestEntryPrice
# ---------------------------------------------------------------------------


class TestEntryPrice:
    """Tests for entry price realism against bid/ask spread."""

    def test_entry_within_spread(self):
        """Entry within bid/ask spread +/- 0.5% passes."""
        validator = SignalValidator()
        signal = _make_signal()  # entry=3450, within 3449-3451 spread
        raw_data = _make_raw_data()

        result = validator.validate_signal(signal, raw_data)

        entry_issues = [i for i in result.issues if "outside realistic spread" in i.lower()]
        assert entry_issues == []

    def test_entry_far_outside_spread(self):
        """Entry far outside spread (4000 vs 3449-3451) fails."""
        validator = SignalValidator()
        signal = _make_signal(entry_price=Decimal("4000"))
        raw_data = _make_raw_data()  # bid=3449, ask=3451

        result = validator.validate_signal(signal, raw_data)

        assert result.valid is False
        assert any("outside realistic spread" in issue.lower() for issue in result.issues)

    def test_missing_bid_ask(self):
        """Missing bid/ask in raw_data fails with 'Missing bid/ask'."""
        validator = SignalValidator()
        signal = _make_signal()
        raw_data = _make_raw_data()
        del raw_data["bid"]
        del raw_data["ask"]

        result = validator.validate_signal(signal, raw_data)

        assert result.valid is False
        assert any("missing bid/ask" in issue.lower() for issue in result.issues)
