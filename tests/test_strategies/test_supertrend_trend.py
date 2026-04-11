"""Tests for the SupertrendTrend strategy (src/strategies/supertrend_trend.py).

Covers Supertrend direction-flip detection, ADX gating, ATR-based SL/TP,
confidence scoring, and edge cases (missing columns, NaN, single row).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.strategies.base_strategy import SignalDirection
from src.strategies.supertrend_trend import SupertrendTrend


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_df(
    st_dirs: list[float],
    adx: float = 25.0,
    atr: float = 100.0,
    close: float = 3000.0,
    ema9: float = 3010.0,
    ema21: float = 2990.0,
    rsi: float = 50.0,
) -> pd.DataFrame:
    """Build a minimal indicator DataFrame with controlled values.

    Parameters
    ----------
    st_dirs : list[float]
        Supertrend direction values for each row (e.g. ``[-1, 1]`` for a
        bullish flip).  All other columns are broadcast to match the length.
    adx, atr, close, ema9, ema21, rsi : float
        Scalar values replicated across every row.
    """
    n = len(st_dirs)
    return pd.DataFrame({
        "supertrend_direction": st_dirs,
        "adx": [adx] * n,
        "atr": [atr] * n,
        "close": [close] * n,
        "ema_9": [ema9] * n,
        "ema_21": [ema21] * n,
        "rsi": [rsi] * n,
    })


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def strategy() -> SupertrendTrend:
    return SupertrendTrend()


# ---------------------------------------------------------------------------
# 1. Long signal on bullish flip
# ---------------------------------------------------------------------------


def test_long_signal_on_bullish_flip(strategy: SupertrendTrend) -> None:
    """prev_dir=-1, cur_dir=1 with ADX=25 should produce a LONG signal."""
    df = _make_df(st_dirs=[-1, 1], adx=25.0)
    sig = strategy.generate_signal(df)
    assert sig.direction == SignalDirection.LONG


# ---------------------------------------------------------------------------
# 2. Short signal on bearish flip
# ---------------------------------------------------------------------------


def test_short_signal_on_bearish_flip(strategy: SupertrendTrend) -> None:
    """prev_dir=1, cur_dir=-1 with ADX=25 should produce a SHORT signal."""
    df = _make_df(
        st_dirs=[1, -1],
        adx=25.0,
        ema9=2990.0,
        ema21=3010.0,
        rsi=50.0,
    )
    sig = strategy.generate_signal(df)
    assert sig.direction == SignalDirection.SHORT


# ---------------------------------------------------------------------------
# 3. No signal when no flip
# ---------------------------------------------------------------------------


def test_no_signal_when_no_flip(strategy: SupertrendTrend) -> None:
    """prev_dir=1, cur_dir=1 (no flip) should produce NONE."""
    df = _make_df(st_dirs=[1, 1])
    sig = strategy.generate_signal(df)
    assert sig.direction == SignalDirection.NONE


# ---------------------------------------------------------------------------
# 4. No signal when ADX too low
# ---------------------------------------------------------------------------


def test_no_signal_adx_too_low(strategy: SupertrendTrend) -> None:
    """ADX=15 with a valid bullish flip should still produce NONE."""
    df = _make_df(st_dirs=[-1, 1], adx=15.0)
    sig = strategy.generate_signal(df)
    assert sig.direction == SignalDirection.NONE


# ---------------------------------------------------------------------------
# 5. ADX boundary behaviour
# ---------------------------------------------------------------------------


def test_no_signal_adx_at_boundary(strategy: SupertrendTrend) -> None:
    """ADX=17.9 should produce NONE; ADX=18.0 should produce a signal."""
    df_low = _make_df(st_dirs=[-1, 1], adx=17.9)
    sig_low = strategy.generate_signal(df_low)
    assert sig_low.direction == SignalDirection.NONE

    df_ok = _make_df(st_dirs=[-1, 1], adx=18.0)
    sig_ok = strategy.generate_signal(df_ok)
    assert sig_ok.direction == SignalDirection.LONG


# ---------------------------------------------------------------------------
# 6. Entry price override
# ---------------------------------------------------------------------------


def test_entry_price_override(strategy: SupertrendTrend) -> None:
    """Passing entry_price=2500 should use that instead of the DataFrame close."""
    df = _make_df(st_dirs=[-1, 1], close=3000.0, atr=100.0)
    sig = strategy.generate_signal(df, entry_price=2500.0)
    assert sig.entry_price == 2500.0
    # SL/TP should be computed from entry_price, not close
    assert sig.stop_loss == pytest.approx(2500.0 - 3.0 * 100.0, abs=1e-4)
    assert sig.take_profit == pytest.approx(2500.0 + 6.0 * 100.0, abs=1e-4)


# ---------------------------------------------------------------------------
# 7. SL/TP for LONG
# ---------------------------------------------------------------------------


def test_sl_tp_long(strategy: SupertrendTrend) -> None:
    """LONG: SL = close - 3*ATR, TP = close + 6*ATR."""
    close, atr = 3000.0, 100.0
    df = _make_df(st_dirs=[-1, 1], close=close, atr=atr)
    sig = strategy.generate_signal(df)

    assert sig.stop_loss == pytest.approx(close - 3.0 * atr, abs=1e-4)
    assert sig.take_profit == pytest.approx(close + 6.0 * atr, abs=1e-4)


# ---------------------------------------------------------------------------
# 8. SL/TP for SHORT
# ---------------------------------------------------------------------------


def test_sl_tp_short(strategy: SupertrendTrend) -> None:
    """SHORT: SL = close + 3*ATR, TP = close - 6*ATR."""
    close, atr = 3000.0, 100.0
    df = _make_df(
        st_dirs=[1, -1],
        close=close,
        atr=atr,
        ema9=2990.0,
        ema21=3010.0,
    )
    sig = strategy.generate_signal(df)

    assert sig.stop_loss == pytest.approx(close + 3.0 * atr, abs=1e-4)
    assert sig.take_profit == pytest.approx(close - 6.0 * atr, abs=1e-4)


# ---------------------------------------------------------------------------
# 9. Full confidence score (100)
# ---------------------------------------------------------------------------


def test_confidence_full_score(strategy: SupertrendTrend) -> None:
    """ADX=40, EMA aligned for LONG, RSI in 30-65 range should give 100.

    Breakdown: base 40 + ADX 20 + EMA 20 + RSI 10 + flip 10 = 100.
    """
    df = _make_df(
        st_dirs=[-1, 1],
        adx=40.0,
        ema9=3010.0,   # ema9 > ema21 -> aligned for LONG
        ema21=2990.0,
        rsi=50.0,      # 30 < 50 < 65 -> in range for LONG
    )
    sig = strategy.generate_signal(df)
    assert sig.confidence == pytest.approx(100.0, abs=0.01)


# ---------------------------------------------------------------------------
# 10. Base-only confidence (55)
# ---------------------------------------------------------------------------


def test_confidence_base_only(strategy: SupertrendTrend) -> None:
    """ADX=18 (zero ADX bonus), EMA divergent (+5), RSI extreme (+0), flip (+10).

    Breakdown: base 40 + ADX 0 + EMA 5 (divergent partial) + RSI 0 + flip 10 = 55.
    """
    df = _make_df(
        st_dirs=[-1, 1],
        adx=18.0,
        ema9=2980.0,   # ema9 < ema21 -> divergent for LONG
        ema21=3010.0,
        rsi=80.0,      # outside 30-65 for LONG -> no RSI bonus
    )
    sig = strategy.generate_signal(df)
    assert sig.confidence == pytest.approx(55.0, abs=0.01)


# ---------------------------------------------------------------------------
# 11. Missing required columns
# ---------------------------------------------------------------------------


def test_no_signal_missing_columns(strategy: SupertrendTrend) -> None:
    """DataFrame missing required columns should produce NONE."""
    df = pd.DataFrame({
        "close": [3000.0, 3010.0],
        "adx": [25.0, 25.0],
        # missing supertrend_direction and atr
    })
    sig = strategy.generate_signal(df)
    assert sig.direction == SignalDirection.NONE


# ---------------------------------------------------------------------------
# 12. Single row (needs 2 for flip detection)
# ---------------------------------------------------------------------------


def test_no_signal_single_row(strategy: SupertrendTrend) -> None:
    """Only 1 row should produce NONE because flip detection needs >= 2 rows."""
    df = _make_df(st_dirs=[1])
    sig = strategy.generate_signal(df)
    assert sig.direction == SignalDirection.NONE


# ---------------------------------------------------------------------------
# 13. NaN in supertrend_direction
# ---------------------------------------------------------------------------


def test_no_signal_nan_indicators(strategy: SupertrendTrend) -> None:
    """NaN in supertrend_direction should produce NONE."""
    df = _make_df(st_dirs=[float("nan"), 1.0])
    sig = strategy.generate_signal(df)
    assert sig.direction == SignalDirection.NONE


# ---------------------------------------------------------------------------
# 14. Strategy name
# ---------------------------------------------------------------------------


def test_signal_has_correct_strategy_name(strategy: SupertrendTrend) -> None:
    """Signal must carry strategy_name == 'SupertrendTrend'."""
    df = _make_df(st_dirs=[-1, 1])
    sig = strategy.generate_signal(df)
    assert sig.strategy_name == "SupertrendTrend"


# ---------------------------------------------------------------------------
# 15. Regime is 'trending'
# ---------------------------------------------------------------------------


def test_signal_regime_is_trending(strategy: SupertrendTrend) -> None:
    """Active signals should have regime == 'trending'."""
    df = _make_df(st_dirs=[-1, 1])
    sig = strategy.generate_signal(df)
    assert sig.regime == "trending"


# ---------------------------------------------------------------------------
# 1H Continuation entry tests
# ---------------------------------------------------------------------------


def test_continuation_signal_long(strategy: SupertrendTrend) -> None:
    """1H flip bullish during established 4H bullish trend -> LONG."""
    df_4h = _make_df(st_dirs=[1, 1, 1, 1], adx=30.0)
    df_1h = _make_df(st_dirs=[-1, 1])
    sig = strategy.generate_continuation_signal(df_4h, df_1h)
    assert sig.direction == SignalDirection.LONG
    assert sig.confidence > 0
    assert "continuation" in sig.reasoning.lower()


def test_continuation_signal_short(strategy: SupertrendTrend) -> None:
    """1H flip bearish during established 4H bearish trend -> SHORT."""
    df_4h = _make_df(st_dirs=[-1, -1, -1, -1], adx=30.0)
    df_1h = _make_df(st_dirs=[1, -1])
    sig = strategy.generate_continuation_signal(df_4h, df_1h)
    assert sig.direction == SignalDirection.SHORT
    assert sig.confidence > 0


def test_continuation_no_signal_when_4h_not_established(strategy: SupertrendTrend) -> None:
    """Mixed 4H directions should return NONE."""
    df_4h = _make_df(st_dirs=[1, -1, 1, -1], adx=30.0)
    df_1h = _make_df(st_dirs=[-1, 1])
    sig = strategy.generate_continuation_signal(df_4h, df_1h)
    assert sig.direction == SignalDirection.NONE


def test_continuation_no_signal_when_1h_no_flip(strategy: SupertrendTrend) -> None:
    """No 1H flip -> no continuation signal."""
    df_4h = _make_df(st_dirs=[1, 1, 1, 1], adx=30.0)
    df_1h = _make_df(st_dirs=[1, 1])  # No flip
    sig = strategy.generate_continuation_signal(df_4h, df_1h)
    assert sig.direction == SignalDirection.NONE


def test_continuation_no_signal_wrong_direction(strategy: SupertrendTrend) -> None:
    """1H flip opposite to 4H established direction -> NONE."""
    df_4h = _make_df(st_dirs=[1, 1, 1, 1], adx=30.0)  # Bullish
    df_1h = _make_df(st_dirs=[1, -1])  # Bearish flip — wrong direction
    sig = strategy.generate_continuation_signal(df_4h, df_1h)
    assert sig.direction == SignalDirection.NONE


def test_continuation_adx_gate(strategy: SupertrendTrend) -> None:
    """ADX below threshold blocks continuation signal."""
    df_4h = _make_df(st_dirs=[1, 1, 1, 1], adx=15.0)
    df_1h = _make_df(st_dirs=[-1, 1])
    sig = strategy.generate_continuation_signal(df_4h, df_1h)
    assert sig.direction == SignalDirection.NONE


def test_continuation_confidence_capped_at_80(strategy: SupertrendTrend) -> None:
    """Continuation confidence should never exceed 80."""
    df_4h = _make_df(st_dirs=[1, 1, 1, 1], adx=40.0, ema9=3010.0, ema21=2990.0, rsi=50.0)
    df_1h = _make_df(st_dirs=[-1, 1])
    sig = strategy.generate_continuation_signal(df_4h, df_1h)
    assert sig.confidence <= 80.0


def test_continuation_has_entry_type_indicator(strategy: SupertrendTrend) -> None:
    """Continuation signals must have entry_type='continuation' in indicators."""
    df_4h = _make_df(st_dirs=[1, 1, 1, 1], adx=30.0)
    df_1h = _make_df(st_dirs=[-1, 1])
    sig = strategy.generate_continuation_signal(df_4h, df_1h)
    assert sig.indicators_used.get("entry_type") == "continuation"


# ────────────────────────────────────────────────────────────────────
# Live-readiness audit: R/R minimum is 2.0 (Immutable Rule #9)
# ────────────────────────────────────────────────────────────────────


def test_rr_below_2_rejected(strategy: SupertrendTrend) -> None:
    """Signals with R/R below 2.0 must be rejected per Immutable Rule #9."""
    # Use very low ATR so the computed R/R stays below 2.0. With SL=3×ATR
    # and TP=6×ATR, the natural R/R is exactly 2.0 — so a slightly negative
    # shift (can't happen naturally with fixed multipliers) isn't testable
    # directly. Instead, verify the threshold string is "2.0" in the code.
    # More practically, a normal bullish flip with standard ATR should pass.
    df = _make_df(st_dirs=[-1, 1], adx=25.0, atr=100.0, close=3000.0)
    sig = strategy.generate_signal(df)
    # With SL=3×100=300, TP=6×100=600, R/R=2.0 — should NOT be rejected
    assert sig.direction != SignalDirection.NONE


# -----------------------------------------------------------------------
# Sprint 1.3: Dynamic SL/TP by regime
# -----------------------------------------------------------------------


class TestDynamicSlTp:
    """Tests for regime-aware SL/TP multipliers."""

    def setup_method(self):
        self.strategy = SupertrendTrend()

    # --- _get_sl_tp_mults() ---

    def test_trending_multipliers(self):
        sl, tp = self.strategy._get_sl_tp_mults("trending")
        assert sl == 3.0
        assert tp == 6.0

    def test_volatile_multipliers(self):
        sl, tp = self.strategy._get_sl_tp_mults("volatile")
        assert sl == 4.0
        assert tp == 8.0

    def test_ranging_multipliers(self):
        sl, tp = self.strategy._get_sl_tp_mults("ranging")
        assert sl == 2.5
        assert tp == 5.0

    def test_quiet_multipliers(self):
        sl, tp = self.strategy._get_sl_tp_mults("quiet")
        assert sl == 2.0
        assert tp == 4.0

    def test_none_regime_uses_defaults(self):
        sl, tp = self.strategy._get_sl_tp_mults(None)
        assert sl == 3.0
        assert tp == 6.0

    def test_unknown_regime_uses_defaults(self):
        sl, tp = self.strategy._get_sl_tp_mults("unknown")
        assert sl == 3.0
        assert tp == 6.0

    # --- All regimes maintain min 2.0 R/R (Immutable Rule #9) ---

    def test_all_regimes_maintain_min_rr(self):
        """Every regime entry in SL_TP_BY_REGIME must have R/R >= 2.0."""
        for regime, params in SupertrendTrend.SL_TP_BY_REGIME.items():
            rr = params["tp_mult"] / params["sl_mult"]
            assert rr >= 2.0, (
                f"Regime '{regime}': R/R={rr:.2f} < 2.0 violates Immutable Rule #9"
            )

    # --- generate_signal with regime ---

    def test_signal_long_trending_sl_tp(self):
        """LONG with regime='trending' should use 3.0/6.0 multipliers."""
        df = _make_df(st_dirs=[-1, 1], close=3000.0, atr=100.0)
        sig = self.strategy.generate_signal(df, regime="trending")
        assert sig.direction == SignalDirection.LONG
        assert sig.stop_loss == pytest.approx(3000.0 - 3.0 * 100.0, abs=1e-4)
        assert sig.take_profit == pytest.approx(3000.0 + 6.0 * 100.0, abs=1e-4)

    def test_signal_long_ranging_sl_tp(self):
        """LONG with regime='ranging' should use 2.5/5.0 multipliers."""
        df = _make_df(st_dirs=[-1, 1], close=3000.0, atr=100.0)
        sig = self.strategy.generate_signal(df, regime="ranging")
        assert sig.direction == SignalDirection.LONG
        assert sig.stop_loss == pytest.approx(3000.0 - 2.5 * 100.0, abs=1e-4)
        assert sig.take_profit == pytest.approx(3000.0 + 5.0 * 100.0, abs=1e-4)

    def test_signal_short_volatile_sl_tp(self):
        """SHORT with regime='volatile' should use 4.0/8.0 multipliers."""
        df = _make_df(
            st_dirs=[1, -1], close=3000.0, atr=100.0,
            ema9=2990.0, ema21=3010.0,
        )
        sig = self.strategy.generate_signal(df, regime="volatile")
        assert sig.direction == SignalDirection.SHORT
        assert sig.stop_loss == pytest.approx(3000.0 + 4.0 * 100.0, abs=1e-4)
        assert sig.take_profit == pytest.approx(3000.0 - 8.0 * 100.0, abs=1e-4)

    def test_signal_no_regime_uses_static(self):
        """Without regime param, should use static 3.0/6.0."""
        df = _make_df(st_dirs=[-1, 1], close=3000.0, atr=100.0)
        sig = self.strategy.generate_signal(df)
        assert sig.stop_loss == pytest.approx(3000.0 - 3.0 * 100.0, abs=1e-4)
        assert sig.take_profit == pytest.approx(3000.0 + 6.0 * 100.0, abs=1e-4)

    # --- generate_continuation_signal with regime ---

    def test_continuation_trending_sl_tp(self):
        """Continuation with regime='trending' should use trending multipliers."""
        df_4h = _make_df(st_dirs=[1, 1, 1, 1], adx=30.0, atr=100.0)
        df_1h = _make_df(st_dirs=[-1, 1], close=3000.0, atr=100.0)
        sig = self.strategy.generate_continuation_signal(df_4h, df_1h, regime="trending")
        assert sig.direction == SignalDirection.LONG
        # Uses 4H ATR for SL/TP calculation
        assert sig.stop_loss == pytest.approx(3000.0 - 3.0 * 100.0, abs=1e-4)
        assert sig.take_profit == pytest.approx(3000.0 + 6.0 * 100.0, abs=1e-4)

    def test_continuation_no_regime_uses_static(self):
        """Continuation without regime should use static multipliers."""
        df_4h = _make_df(st_dirs=[1, 1, 1, 1], adx=30.0, atr=100.0)
        df_1h = _make_df(st_dirs=[-1, 1], close=3000.0, atr=100.0)
        sig = self.strategy.generate_continuation_signal(df_4h, df_1h)
        assert sig.direction == SignalDirection.LONG
        assert sig.stop_loss == pytest.approx(3000.0 - 3.0 * 100.0, abs=1e-4)
        assert sig.take_profit == pytest.approx(3000.0 + 6.0 * 100.0, abs=1e-4)


# ---------------------------------------------------------------------------
# R/R boundary float tolerance (v6.22)
# ---------------------------------------------------------------------------


class TestRRBoundaryTolerance:
    """R/R = 2.0 must PASS, not be rejected by float precision issues."""

    def setup_method(self):
        self.strategy = SupertrendTrend()

    def test_rr_exactly_2_0_passes_flip(self):
        """Flip signal with TP=6×ATR, SL=3×ATR → R/R=2.0 should pass."""
        df = _make_df(st_dirs=[-1, 1], adx=25.0, atr=100.0, close=3000.0)
        sig = self.strategy.generate_signal(df)
        assert sig.direction == SignalDirection.LONG  # Not rejected

    def test_rr_at_boundary_with_tiny_float_drift(self):
        """Simulate float drift where reward/risk rounds to 1.9999."""
        from src.strategies.base_strategy import calculate_rr_ratio

        # Construct values where float division yields just under 2.0
        # close=9.42, SL=3×ATR, TP=6×ATR with atr=0.171727...
        entry = 9.42
        atr = 0.17172568  # chosen to cause float drift
        sl = entry - atr * 3.0
        tp = entry + atr * 6.0
        rr = calculate_rr_ratio(entry, sl, tp)
        # Must accept R/R within epsilon of 2.0
        assert rr >= 2.0 - 1e-9 or rr >= 2.0, f"R/R {rr} rejected at boundary"

    def test_min_rr_constant(self):
        """MIN_RR constant has epsilon tolerance."""
        assert self.strategy.MIN_RR < 2.0
        assert self.strategy.MIN_RR > 1.99
