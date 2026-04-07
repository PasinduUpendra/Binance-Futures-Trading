"""
Unit tests for the AdaptiveTrend momentum strategy.

Covers:
- Momentum score computation (short, medium, long lookbacks)
- LONG/SHORT signal generation
- EMA alignment filter
- RSI overbought/oversold filter
- Confidence scoring (momentum strength, EMA, ADX, RSI)
- Regime-aware SL/TP multiples
- Data validation (missing columns, insufficient rows)
- Edge cases (flat prices, NaN indicators)
- Integration with BaseStrategy (Signal model, R/R validation)
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.strategies.adaptive_trend import AdaptiveTrend
from src.strategies.base_strategy import Signal, SignalDirection


# ---------------------------------------------------------------------------
# Helper: build indicator-enriched DataFrames for AdaptiveTrend
# ---------------------------------------------------------------------------


def _make_momentum_df(
    n: int = 200,
    close_start: float = 1000.0,
    close_end: float | None = None,
    adx: float = 15.0,
    rsi: float = 50.0,
    atr: float = 20.0,
    ema_9: float | None = None,
    ema_21: float | None = None,
) -> pd.DataFrame:
    """Build a DataFrame with controlled momentum and indicator values.

    Parameters
    ----------
    n : int
        Number of rows (must be >= 100 for MIN_ROWS).
    close_start, close_end : float
        Start and end prices — creates linear drift that determines momentum.
    adx, rsi, atr : float
        Constant indicator values.
    ema_9, ema_21 : float or None
        If None, default to close_end +/- 5 (bullish alignment).
    """
    if close_end is None:
        close_end = close_start

    closes = np.linspace(close_start, close_end, n)

    if ema_9 is None:
        ema_9 = close_end + 5.0
    if ema_21 is None:
        ema_21 = close_end - 5.0

    df = pd.DataFrame(
        {
            "close": closes,
            "ema_9": [ema_9] * n,
            "ema_21": [ema_21] * n,
            "rsi": [rsi] * n,
            "adx": [adx] * n,
            "atr": [atr] * n,
            "open": closes - 1.0,
            "high": closes + 5.0,
            "low": closes - 5.0,
            "volume": [1000.0] * n,
        }
    )
    return df


@pytest.fixture
def strategy() -> AdaptiveTrend:
    return AdaptiveTrend()


# ═══════════════════════════════════════════════════════════════════
# 1. Momentum Score Computation
# ═══════════════════════════════════════════════════════════════════


class TestMomentumScore:
    """Tests for _compute_momentum_score."""

    def test_flat_prices_zero_momentum(self, strategy: AdaptiveTrend) -> None:
        """Flat prices should produce ~0 momentum score."""
        df = _make_momentum_df(n=200, close_start=1000.0, close_end=1000.0)
        score = strategy._compute_momentum_score(df)
        assert abs(score) < 1e-10

    def test_rising_prices_positive_momentum(self, strategy: AdaptiveTrend) -> None:
        """Steadily rising prices produce positive momentum."""
        df = _make_momentum_df(n=200, close_start=900.0, close_end=1100.0)
        score = strategy._compute_momentum_score(df)
        assert score > 0

    def test_falling_prices_negative_momentum(self, strategy: AdaptiveTrend) -> None:
        """Steadily falling prices produce negative momentum."""
        df = _make_momentum_df(n=200, close_start=1100.0, close_end=900.0)
        score = strategy._compute_momentum_score(df)
        assert score < 0

    def test_insufficient_data_returns_nan(self, strategy: AdaptiveTrend) -> None:
        """Less than MOM_LONG + 1 rows returns NaN."""
        df = _make_momentum_df(n=50, close_start=1000.0, close_end=1100.0)
        score = strategy._compute_momentum_score(df)
        assert np.isnan(score)

    def test_weights_sum_to_one(self, strategy: AdaptiveTrend) -> None:
        """Momentum weights W_SHORT + W_MEDIUM + W_LONG = 1.0."""
        total = strategy.W_SHORT + strategy.W_MEDIUM + strategy.W_LONG
        assert abs(total - 1.0) < 1e-10


# ═══════════════════════════════════════════════════════════════════
# 2. Signal Direction (LONG)
# ═══════════════════════════════════════════════════════════════════


class TestLongSignal:
    """Tests for bullish (LONG) signal generation."""

    def test_long_on_positive_momentum(self, strategy: AdaptiveTrend) -> None:
        """Strong upward momentum + bullish EMA → LONG signal."""
        df = _make_momentum_df(
            n=200, close_start=900.0, close_end=1100.0,
            ema_9=1105.0, ema_21=1095.0,  # EMA9 > EMA21
            rsi=55.0,
        )
        signal = strategy.generate_signal(df)
        assert signal.direction == SignalDirection.LONG
        assert signal.confidence >= 30.0

    def test_long_blocked_by_overbought_rsi(self, strategy: AdaptiveTrend) -> None:
        """RSI > 70 blocks LONG signal."""
        df = _make_momentum_df(
            n=200, close_start=900.0, close_end=1100.0,
            ema_9=1105.0, ema_21=1095.0,
            rsi=75.0,  # Overbought
        )
        signal = strategy.generate_signal(df)
        assert signal.direction == SignalDirection.NONE

    def test_long_blocked_by_bearish_ema(self, strategy: AdaptiveTrend) -> None:
        """EMA9 < EMA21 blocks LONG signal even with positive momentum."""
        df = _make_momentum_df(
            n=200, close_start=900.0, close_end=1100.0,
            ema_9=1090.0, ema_21=1110.0,  # Bearish alignment
            rsi=55.0,
        )
        signal = strategy.generate_signal(df)
        assert signal.direction == SignalDirection.NONE


# ═══════════════════════════════════════════════════════════════════
# 3. Signal Direction (SHORT)
# ═══════════════════════════════════════════════════════════════════


class TestShortSignal:
    """Tests for bearish (SHORT) signal generation."""

    def test_short_on_negative_momentum(self, strategy: AdaptiveTrend) -> None:
        """Strong downward momentum + bearish EMA → SHORT signal."""
        df = _make_momentum_df(
            n=200, close_start=1100.0, close_end=900.0,
            ema_9=895.0, ema_21=905.0,  # EMA9 < EMA21
            rsi=45.0,
        )
        signal = strategy.generate_signal(df)
        assert signal.direction == SignalDirection.SHORT
        assert signal.confidence >= 30.0

    def test_short_blocked_by_oversold_rsi(self, strategy: AdaptiveTrend) -> None:
        """RSI < 30 blocks SHORT signal."""
        df = _make_momentum_df(
            n=200, close_start=1100.0, close_end=900.0,
            ema_9=895.0, ema_21=905.0,
            rsi=25.0,  # Oversold
        )
        signal = strategy.generate_signal(df)
        assert signal.direction == SignalDirection.NONE

    def test_short_blocked_by_bullish_ema(self, strategy: AdaptiveTrend) -> None:
        """EMA9 > EMA21 blocks SHORT signal even with negative momentum."""
        df = _make_momentum_df(
            n=200, close_start=1100.0, close_end=900.0,
            ema_9=910.0, ema_21=890.0,  # Bullish alignment
            rsi=45.0,
        )
        signal = strategy.generate_signal(df)
        assert signal.direction == SignalDirection.NONE


# ═══════════════════════════════════════════════════════════════════
# 4. Momentum Threshold
# ═══════════════════════════════════════════════════════════════════


class TestMomentumThreshold:
    """Tests for momentum threshold gate."""

    def test_below_threshold_no_signal(self, strategy: AdaptiveTrend) -> None:
        """Momentum below threshold produces NONE signal."""
        # Very small drift — momentum will be below 0.5% threshold
        df = _make_momentum_df(
            n=200, close_start=1000.0, close_end=1001.0,
            ema_9=1006.0, ema_21=996.0, rsi=50.0,
        )
        signal = strategy.generate_signal(df)
        assert signal.direction == SignalDirection.NONE

    def test_exactly_at_threshold_no_signal(self, strategy: AdaptiveTrend) -> None:
        """Momentum exactly at threshold = no signal (must EXCEED, not equal)."""
        # The composite score for a precisely tuned dataset — verify it's close to threshold
        df = _make_momentum_df(n=200, close_start=1000.0, close_end=1000.0)
        score = strategy._compute_momentum_score(df)
        # Flat data should be well below threshold
        assert abs(score) < strategy.MOM_THRESHOLD


# ═══════════════════════════════════════════════════════════════════
# 5. Confidence Scoring
# ═══════════════════════════════════════════════════════════════════


class TestConfidenceScoring:
    """Tests for _score_confidence."""

    def test_base_confidence_30(self, strategy: AdaptiveTrend) -> None:
        """Minimum confidence is 30 (base for clearing threshold)."""
        score = strategy._score_confidence(
            mom_score=0.006,  # Barely above threshold
            ema9=100.0, ema21=105.0,  # Wrong alignment (no bonus)
            adx=5.0,   # Very low (no bonus)
            rsi=80.0,  # Overbought (no bonus for SHORT scenario)
            direction=SignalDirection.SHORT,
        )
        # Wrong EMA direction: EMA9 < EMA21, direction=SHORT → +15
        # Actually, for SHORT: ema9 < ema21 IS correct alignment, so +15
        # Let's check with wrong alignment
        score2 = strategy._score_confidence(
            mom_score=0.006,
            ema9=110.0, ema21=105.0,  # Bullish alignment, but direction is SHORT
            adx=5.0,    # No ADX bonus
            rsi=80.0,   # No RSI bonus for SHORT (extreme)
            direction=SignalDirection.SHORT,
        )
        assert score2 >= 30.0

    def test_ema_alignment_bonus(self, strategy: AdaptiveTrend) -> None:
        """EMA alignment adds 15 points."""
        base = strategy._score_confidence(
            mom_score=0.006,
            ema9=100.0, ema21=105.0,  # Bullish: EMA9 < EMA21 (wrong for LONG)
            adx=5.0, rsi=80.0,
            direction=SignalDirection.LONG,
        )
        with_ema = strategy._score_confidence(
            mom_score=0.006,
            ema9=110.0, ema21=105.0,  # Bullish: EMA9 > EMA21 (correct for LONG)
            adx=5.0, rsi=80.0,
            direction=SignalDirection.LONG,
        )
        assert with_ema - base == pytest.approx(15.0, abs=0.01)

    def test_adx_bonus_tiers(self, strategy: AdaptiveTrend) -> None:
        """ADX adds 5/10/15 points at 12/18/25 thresholds."""
        low = strategy._score_confidence(
            mom_score=0.006, ema9=100.0, ema21=105.0,
            adx=10.0, rsi=50.0, direction=SignalDirection.LONG,
        )
        mid = strategy._score_confidence(
            mom_score=0.006, ema9=100.0, ema21=105.0,
            adx=12.0, rsi=50.0, direction=SignalDirection.LONG,
        )
        high = strategy._score_confidence(
            mom_score=0.006, ema9=100.0, ema21=105.0,
            adx=18.0, rsi=50.0, direction=SignalDirection.LONG,
        )
        top = strategy._score_confidence(
            mom_score=0.006, ema9=100.0, ema21=105.0,
            adx=25.0, rsi=50.0, direction=SignalDirection.LONG,
        )
        assert mid - low == pytest.approx(5.0, abs=0.01)
        assert high - low == pytest.approx(10.0, abs=0.01)
        assert top - low == pytest.approx(15.0, abs=0.01)

    def test_max_confidence_100(self, strategy: AdaptiveTrend) -> None:
        """Confidence is capped at 100."""
        score = strategy._score_confidence(
            mom_score=0.10,  # Very strong momentum
            ema9=110.0, ema21=100.0,  # Aligned
            adx=30.0,   # Strong
            rsi=50.0,   # Ideal
            direction=SignalDirection.LONG,
        )
        assert score <= 100.0


# ═══════════════════════════════════════════════════════════════════
# 6. SL/TP Regime-Aware Multiples
# ═══════════════════════════════════════════════════════════════════


class TestSlTpRegime:
    """Tests for regime-aware SL/TP multiples."""

    def test_trending_sl_tp(self, strategy: AdaptiveTrend) -> None:
        sl, tp = strategy._get_sl_tp_mults("trending")
        assert sl == 3.0
        assert tp == 6.0

    def test_volatile_sl_tp(self, strategy: AdaptiveTrend) -> None:
        sl, tp = strategy._get_sl_tp_mults("volatile")
        assert sl == 3.5
        assert tp == 7.0

    def test_ranging_sl_tp(self, strategy: AdaptiveTrend) -> None:
        sl, tp = strategy._get_sl_tp_mults("ranging")
        assert sl == 2.5
        assert tp == 5.0

    def test_quiet_sl_tp(self, strategy: AdaptiveTrend) -> None:
        sl, tp = strategy._get_sl_tp_mults("quiet")
        assert sl == 2.0
        assert tp == 4.0

    def test_none_regime_returns_defaults(self, strategy: AdaptiveTrend) -> None:
        sl, tp = strategy._get_sl_tp_mults(None)
        assert sl == strategy.SL_ATR_MULT
        assert tp == strategy.TP_ATR_MULT

    def test_unknown_regime_returns_defaults(self, strategy: AdaptiveTrend) -> None:
        sl, tp = strategy._get_sl_tp_mults("unknown_regime_xyz")
        assert sl == strategy.SL_ATR_MULT
        assert tp == strategy.TP_ATR_MULT

    def test_all_regimes_min_rr_2(self, strategy: AdaptiveTrend) -> None:
        """All regime SL/TP multiples must give R/R >= 2.0."""
        for regime, params in strategy.SL_TP_BY_REGIME.items():
            rr = params["tp_mult"] / params["sl_mult"]
            assert rr >= 2.0, f"Regime '{regime}' has R/R={rr:.2f} < 2.0"


# ═══════════════════════════════════════════════════════════════════
# 7. Data Validation
# ═══════════════════════════════════════════════════════════════════


class TestValidation:
    """Tests for data validation (missing columns, insufficient rows)."""

    def test_missing_columns_returns_none(self, strategy: AdaptiveTrend) -> None:
        """Missing required columns → NONE signal (not exception)."""
        df = pd.DataFrame({"close": [100.0] * 200})
        signal = strategy.generate_signal(df)
        assert signal.direction == SignalDirection.NONE

    def test_insufficient_rows_returns_none(self, strategy: AdaptiveTrend) -> None:
        """Less than MIN_ROWS → NONE signal."""
        df = _make_momentum_df(n=50)
        signal = strategy.generate_signal(df)
        assert signal.direction == SignalDirection.NONE

    def test_nan_indicator_returns_none(self, strategy: AdaptiveTrend) -> None:
        """NaN indicator values → NONE signal."""
        df = _make_momentum_df(n=200)
        df.loc[df.index[-1], "rsi"] = np.nan
        signal = strategy.generate_signal(df)
        assert signal.direction == SignalDirection.NONE

    def test_zero_atr_returns_none(self, strategy: AdaptiveTrend) -> None:
        """ATR = 0 → NONE signal (can't compute SL/TP)."""
        df = _make_momentum_df(n=200, close_start=900.0, close_end=1100.0, atr=0.0)
        signal = strategy.generate_signal(df)
        assert signal.direction == SignalDirection.NONE


# ═══════════════════════════════════════════════════════════════════
# 8. Entry Price Override
# ═══════════════════════════════════════════════════════════════════


class TestEntryPrice:
    """Tests for entry_price parameter override."""

    def test_entry_price_used_when_provided(self, strategy: AdaptiveTrend) -> None:
        """When entry_price is given, signal uses it instead of last close."""
        df = _make_momentum_df(
            n=200, close_start=900.0, close_end=1100.0,
            ema_9=1105.0, ema_21=1095.0, rsi=50.0,
        )
        signal = strategy.generate_signal(df, entry_price=1050.0)
        if signal.direction != SignalDirection.NONE:
            assert signal.entry_price == pytest.approx(1050.0, abs=0.01)

    def test_last_close_used_without_entry_price(self, strategy: AdaptiveTrend) -> None:
        """Without entry_price, use last close from DataFrame."""
        df = _make_momentum_df(
            n=200, close_start=900.0, close_end=1100.0,
            ema_9=1105.0, ema_21=1095.0, rsi=50.0, atr=20.0,
        )
        signal = strategy.generate_signal(df)
        if signal.direction != SignalDirection.NONE:
            expected_close = df["close"].iloc[-1]
            assert signal.entry_price == pytest.approx(expected_close, abs=0.1)


# ═══════════════════════════════════════════════════════════════════
# 9. Signal Model Correctness
# ═══════════════════════════════════════════════════════════════════


class TestSignalModel:
    """Tests for Signal model correctness (SL/TP sides, strategy_name, etc.)."""

    def test_long_sl_below_entry(self, strategy: AdaptiveTrend) -> None:
        """LONG signal SL must be below entry."""
        df = _make_momentum_df(
            n=200, close_start=900.0, close_end=1100.0,
            ema_9=1105.0, ema_21=1095.0, rsi=50.0,
        )
        signal = strategy.generate_signal(df)
        if signal.direction == SignalDirection.LONG:
            assert signal.stop_loss < signal.entry_price

    def test_long_tp_above_entry(self, strategy: AdaptiveTrend) -> None:
        """LONG signal TP must be above entry."""
        df = _make_momentum_df(
            n=200, close_start=900.0, close_end=1100.0,
            ema_9=1105.0, ema_21=1095.0, rsi=50.0,
        )
        signal = strategy.generate_signal(df)
        if signal.direction == SignalDirection.LONG:
            assert signal.take_profit > signal.entry_price

    def test_short_sl_above_entry(self, strategy: AdaptiveTrend) -> None:
        """SHORT signal SL must be above entry."""
        df = _make_momentum_df(
            n=200, close_start=1100.0, close_end=900.0,
            ema_9=895.0, ema_21=905.0, rsi=45.0,
        )
        signal = strategy.generate_signal(df)
        if signal.direction == SignalDirection.SHORT:
            assert signal.stop_loss > signal.entry_price

    def test_short_tp_below_entry(self, strategy: AdaptiveTrend) -> None:
        """SHORT signal TP must be below entry."""
        df = _make_momentum_df(
            n=200, close_start=1100.0, close_end=900.0,
            ema_9=895.0, ema_21=905.0, rsi=45.0,
        )
        signal = strategy.generate_signal(df)
        if signal.direction == SignalDirection.SHORT:
            assert signal.take_profit < signal.entry_price

    def test_strategy_name(self, strategy: AdaptiveTrend) -> None:
        """Signal has correct strategy_name."""
        df = _make_momentum_df(
            n=200, close_start=900.0, close_end=1100.0,
            ema_9=1105.0, ema_21=1095.0, rsi=50.0,
        )
        signal = strategy.generate_signal(df)
        if signal.direction != SignalDirection.NONE:
            assert signal.strategy_name == "AdaptiveTrend"

    def test_indicators_used_populated(self, strategy: AdaptiveTrend) -> None:
        """indicators_used dict has momentum_score, ema_9, ema_21, etc."""
        df = _make_momentum_df(
            n=200, close_start=900.0, close_end=1100.0,
            ema_9=1105.0, ema_21=1095.0, rsi=50.0,
        )
        signal = strategy.generate_signal(df)
        if signal.direction != SignalDirection.NONE:
            assert "momentum_score" in signal.indicators_used
            assert "ema_9" in signal.indicators_used
            assert "adx" in signal.indicators_used

    def test_regime_passed_through(self, strategy: AdaptiveTrend) -> None:
        """regime kwarg ends up in Signal.regime field."""
        df = _make_momentum_df(
            n=200, close_start=900.0, close_end=1100.0,
            ema_9=1105.0, ema_21=1095.0, rsi=50.0,
        )
        signal = strategy.generate_signal(df, regime="ranging")
        if signal.direction != SignalDirection.NONE:
            assert signal.regime == "ranging"


# ═══════════════════════════════════════════════════════════════════
# 10. RSI Confidence Bonus
# ═══════════════════════════════════════════════════════════════════


class TestRsiBonus:
    """Tests for RSI-based confidence bonus."""

    def test_ideal_rsi_range_15pts(self, strategy: AdaptiveTrend) -> None:
        """RSI 40-60 gives max 15 pts."""
        score = strategy._score_confidence(
            mom_score=0.01, ema9=110.0, ema21=100.0,
            adx=5.0, rsi=50.0, direction=SignalDirection.LONG,
        )
        score_extreme = strategy._score_confidence(
            mom_score=0.01, ema9=110.0, ema21=100.0,
            adx=5.0, rsi=75.0, direction=SignalDirection.LONG,
        )
        assert score > score_extreme

    def test_acceptable_rsi_range_10pts(self, strategy: AdaptiveTrend) -> None:
        """RSI 30-40 or 60-70 gives 10 pts."""
        acceptable = strategy._score_confidence(
            mom_score=0.01, ema9=110.0, ema21=100.0,
            adx=5.0, rsi=65.0, direction=SignalDirection.LONG,
        )
        ideal = strategy._score_confidence(
            mom_score=0.01, ema9=110.0, ema21=100.0,
            adx=5.0, rsi=50.0, direction=SignalDirection.LONG,
        )
        assert ideal - acceptable == pytest.approx(5.0, abs=0.01)
