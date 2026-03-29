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
