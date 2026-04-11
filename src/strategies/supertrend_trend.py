"""
4H Supertrend trend strategy for the Claude Quant trading system.

Trades on 4H Supertrend direction flips — the highest-performing signal
in backtesting (+94% return, 60.9% win rate, Sharpe 3.31 over 172 days).

Uses 4H timeframe for signal generation (NOT 1H). Entry price comes from
the current 1H close for precise timing.

Indicator columns consumed (from IndicatorEngine on 4H data):
  - supertrend_direction  (1 = bullish, -1 = bearish)
  - adx
  - ema_9, ema_21
  - rsi
  - atr
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from src.strategies.base_strategy import (
    BaseStrategy,
    Signal,
    SignalDirection,
    calculate_rr_ratio,
)

logger = logging.getLogger(__name__)


class SupertrendTrend(BaseStrategy):
    """Trade 4H Supertrend direction flips.

    Entry conditions
    ~~~~~~~~~~~~~~~~
    - **Long**: Supertrend flips from bearish (-1) to bullish (1) **and** ADX >= 18
    - **Short**: Supertrend flips from bullish (1) to bearish (-1) **and** ADX >= 18

    Risk management
    ~~~~~~~~~~~~~~~
    - SL: 3.0 x ATR(4H) from entry
    - TP: 6.0 x ATR(4H) from entry (2:1 R/R)
    - Trailing stop: activate after 2.0 ATR favorable, trail at 2.5 ATR

    Exit on Supertrend reversal
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~
    When Supertrend flips against the position, the position should be closed
    immediately (even though these are individual losers, they enable capital
    recycling into the new direction → net positive for the system).
    """

    name: str = "SupertrendTrend"

    # Column names
    COL_SUPERTREND_DIR: str = "supertrend_direction"
    COL_ADX: str = "adx"
    COL_EMA9: str = "ema_9"
    COL_EMA21: str = "ema_21"
    COL_RSI: str = "rsi"
    COL_ATR: str = "atr"
    COL_CLOSE: str = "close"

    # Tunables (static defaults — used when regime is not specified)
    ADX_MIN: float = 18.0
    SL_ATR_MULT: float = 3.0
    TP_ATR_MULT: float = 6.0
    # Minimum R/R with epsilon to avoid float boundary rejection (2.0 - 1e-9)
    MIN_RR: float = 2.0 - 1e-9

    # Dynamic SL/TP by regime (Sprint 1.3)
    # All entries maintain minimum 2.0 R/R (Immutable Rule #9)
    # NOTE: Trending SL kept at 3.0 (proven baseline) — tighter 2.5 caused
    # excess stop-outs on backtest (-22% return regression).  Only TP is
    # widened to let winners run further.
    SL_TP_BY_REGIME: dict[str, dict[str, float]] = {
        "trending": {"sl_mult": 3.0, "tp_mult": 6.0},  # R/R=2.0 — same as proven baseline
        "volatile": {"sl_mult": 4.0, "tp_mult": 8.0},  # R/R=2.0 — wider SL for noise
        "ranging":  {"sl_mult": 2.5, "tp_mult": 5.0},  # R/R=2.0 — tighter range
        "quiet":    {"sl_mult": 2.0, "tp_mult": 4.0},  # R/R=2.0 — conservative
    }

    # Minimum consecutive bars the 4H Supertrend must hold the same direction
    # to qualify as an "established trend" for 1H continuation entries.
    MIN_ESTABLISHED_BARS: int = 3

    # Extended lookback windows for flip detection.
    # With 30-min polling, a 1H flip is only detectable for 1 bar (1 hour).
    # Extending to 5 bars (5 hours) covers ~10 polling cycles.
    # For 15m flips, 3 bars (45 min) covers ~1.5 polling cycles.
    # v6.20: Extended 1H lookback 5→8 bars to survive 4H candle transitions.
    # After a 4H candle close, v6.19's 5-bar window expired instantly (C1/C2 had
    # 3 signals; C3-C7 had zero because the flip aged out of the 5-bar window).
    CONTINUATION_LOOKBACK_1H: int = 8
    FAST_LOOKBACK_15M: int = 3

    def _find_recent_flip(
        self,
        st_dir_series: pd.Series,
        expected_dir: int,
        lookback: int = 1,
    ) -> int | None:
        """Find how recently a flip to *expected_dir* occurred.

        Scans the last *lookback* consecutive-candle transitions in
        *st_dir_series*.  Returns the "age" (1 = most recent pair, i.e.
        iloc[-2] → iloc[-1]), or ``None`` if no flip was found.

        A lower age means a fresher (more actionable) flip.
        """
        clean = st_dir_series.dropna()
        if len(clean) < 2:
            return None

        max_check = min(lookback, len(clean) - 1)
        for age in range(1, max_check + 1):
            cur_idx = len(clean) - age
            prev_idx = cur_idx - 1
            cur_val = int(clean.iloc[cur_idx])
            prev_val = int(clean.iloc[prev_idx])
            if prev_val != expected_dir and cur_val == expected_dir:
                return age

        return None

    def _get_sl_tp_mults(self, regime: str | None) -> tuple[float, float]:
        """Return (sl_mult, tp_mult) for the given regime.

        Falls back to static ``SL_ATR_MULT`` / ``TP_ATR_MULT`` when regime
        is not provided or not in the mapping.
        """
        if regime and regime in self.SL_TP_BY_REGIME:
            params = self.SL_TP_BY_REGIME[regime]
            return params["sl_mult"], params["tp_mult"]
        return self.SL_ATR_MULT, self.TP_ATR_MULT

    def generate_signal(
        self,
        df: pd.DataFrame,
        entry_price: float | None = None,
        regime: str | None = None,
    ) -> Signal:
        """Evaluate the Supertrend flip setup on 4H data.

        Parameters
        ----------
        df : DataFrame
            4H indicator-enriched data (NOT 1H).
        entry_price : float, optional
            If provided, use this as entry price (e.g., current 1H close).
            Otherwise uses the 4H close.
        """
        try:
            self._validate(df)
        except (KeyError, ValueError) as exc:
            self.logger.warning("SupertrendTrend validation failed: %s", exc)
            return self._no_signal(str(exc))

        # Current and previous bar values
        st_dir = self._safe_last(df[self.COL_SUPERTREND_DIR])
        prev_st_dir = self._safe_prev(df[self.COL_SUPERTREND_DIR])
        adx = self._safe_last(df[self.COL_ADX])
        atr = self._safe_last(df[self.COL_ATR])
        ema9 = self._safe_last(df[self.COL_EMA9])
        ema21 = self._safe_last(df[self.COL_EMA21])
        rsi = self._safe_last(df[self.COL_RSI])
        close = entry_price if entry_price is not None else self._safe_last(df[self.COL_CLOSE])

        for name, val in [
            ("st_dir", st_dir), ("prev_st_dir", prev_st_dir),
            ("adx", adx), ("atr", atr), ("close", close),
        ]:
            if np.isnan(val):
                return self._no_signal(f"Indicator '{name}' is NaN")

        # Gate: minimum trend strength
        if adx < self.ADX_MIN:
            return self._no_signal(f"ADX {adx:.1f} < {self.ADX_MIN} — too weak for trend trade")

        # Detect Supertrend flip
        direction = SignalDirection.NONE
        if prev_st_dir < 0 and st_dir > 0:
            direction = SignalDirection.LONG
        elif prev_st_dir > 0 and st_dir < 0:
            direction = SignalDirection.SHORT
        else:
            return self._no_signal(
                f"No Supertrend flip: prev_dir={prev_st_dir:.0f}, cur_dir={st_dir:.0f}"
            )

        # Entry / SL / TP (regime-aware)
        sl_mult, tp_mult = self._get_sl_tp_mults(regime)
        sl_distance = atr * sl_mult
        tp_distance = atr * tp_mult

        if direction == SignalDirection.LONG:
            stop_loss = close - sl_distance
            take_profit = close + tp_distance
        else:
            stop_loss = close + sl_distance
            take_profit = close - tp_distance

        if stop_loss <= 0 or take_profit <= 0:
            return self._no_signal("Computed SL or TP is non-positive")

        rr = calculate_rr_ratio(close, stop_loss, take_profit)
        if rr < self.MIN_RR:
            return self._no_signal(f"R/R {rr:.2f} below 2.0 minimum")

        # Confidence scoring
        confidence = self._compute_confidence(
            adx=adx, ema9=ema9, ema21=ema21, rsi=rsi, direction=direction,
        )

        # Reasoning
        dir_label = direction.value.upper()
        ema_aligned = (
            (direction == SignalDirection.LONG and ema9 > ema21) or
            (direction == SignalDirection.SHORT and ema9 < ema21)
        )
        reasoning = (
            f"{dir_label} Supertrend flip signal at {close:.6f}. "
            f"4H Supertrend flipped {'bullish' if st_dir > 0 else 'bearish'}. "
            f"ADX={adx:.1f} confirms trend strength. "
            f"EMA alignment: {'confirmed' if ema_aligned else 'divergent'}. "
            f"RSI={rsi:.1f}. "
            f"SL={stop_loss:.6f}, TP={take_profit:.6f}, R/R={rr:.2f}. "
            f"Confidence: {confidence:.0f}%."
        )

        indicators_snapshot = {
            "supertrend_dir": int(st_dir),
            "prev_supertrend_dir": int(prev_st_dir),
            "adx": round(adx, 2),
            "ema_9": round(ema9, 6) if not np.isnan(ema9) else None,
            "ema_21": round(ema21, 6) if not np.isnan(ema21) else None,
            "rsi": round(rsi, 2) if not np.isnan(rsi) else None,
            "atr": round(atr, 6),
            "close": round(close, 6),
        }

        return Signal(
            direction=direction,
            confidence=confidence,
            entry_price=round(close, 8),
            stop_loss=round(stop_loss, 8),
            take_profit=round(take_profit, 8),
            strategy_name=self.name,
            regime="trending",
            indicators_used=indicators_snapshot,
            reasoning=reasoning,
        )

    def _validate(self, df: pd.DataFrame) -> None:
        required = [
            self.COL_SUPERTREND_DIR, self.COL_ADX, self.COL_ATR, self.COL_CLOSE,
        ]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise KeyError(f"Missing columns: {missing}")
        if len(df) < 2:
            raise ValueError("Need at least 2 rows for flip detection")

    def _compute_confidence(
        self, *, adx: float, ema9: float, ema21: float,
        rsi: float, direction: SignalDirection,
    ) -> float:
        """Compute 0-100 confidence.

        Breakdown (max 100):
          Base for Supertrend flip : 40
          ADX strength             : up to 20
          EMA alignment            : up to 20
          RSI position             : up to 10
          Flip quality             : 10 (always given for valid flip)
        """
        score = 40.0  # Base for valid Supertrend flip

        # ADX strength (18-40 mapped to 0-20)
        adx_score = min(20.0, max(0.0, (adx - self.ADX_MIN) / 22.0) * 20.0)
        score += adx_score

        # EMA alignment
        if not np.isnan(ema9) and not np.isnan(ema21):
            if direction == SignalDirection.LONG and ema9 > ema21:
                score += 20.0
            elif direction == SignalDirection.SHORT and ema9 < ema21:
                score += 20.0
            else:
                score += 5.0  # Partial credit for divergent alignment

        # RSI position (not at extremes = better)
        if not np.isnan(rsi):
            if direction == SignalDirection.LONG and 30 < rsi < 65:
                score += 10.0
            elif direction == SignalDirection.SHORT and 35 < rsi < 70:
                score += 10.0

        # Flip quality bonus (always given for valid flip)
        score += 10.0

        return min(100.0, max(0.0, round(score, 2)))

    # ------------------------------------------------------------------
    # 1H Continuation entry (sustained 4H trend + 1H flip in same dir)
    # ------------------------------------------------------------------

    def generate_continuation_signal(
        self,
        df_4h: pd.DataFrame,
        df_1h: pd.DataFrame,
        regime: str | None = None,
    ) -> Signal:
        """Generate a continuation entry using a 1H Supertrend flip.

        When the 4H Supertrend is established (same direction for
        ``MIN_ESTABLISHED_BARS``+ bars), a 1H Supertrend flip in the
        same direction provides a continuation entry opportunity.

        Lower confidence ceiling than a 4H flip signal (80 vs 100).
        """
        try:
            self._validate(df_4h)
            self._validate(df_1h)
        except (KeyError, ValueError) as exc:
            return self._no_signal(f"Continuation validation failed: {exc}")

        # Check 4H trend is established (same direction for N bars)
        st_dir_4h = df_4h[self.COL_SUPERTREND_DIR].dropna()
        if len(st_dir_4h) < self.MIN_ESTABLISHED_BARS + 1:
            return self._no_signal("Not enough 4H bars for continuation check")

        recent_dirs = st_dir_4h.iloc[-self.MIN_ESTABLISHED_BARS:]
        if not (recent_dirs == recent_dirs.iloc[0]).all():
            return self._no_signal("4H Supertrend not established — mixed directions")

        established_dir = int(recent_dirs.iloc[0])

        # ADX gate on 4H data
        adx = self._safe_last(df_4h[self.COL_ADX])
        if np.isnan(adx) or adx < self.ADX_MIN:
            return self._no_signal(
                f"ADX {adx:.1f} < {self.ADX_MIN} — too weak for continuation"
            )

        # Check 1H Supertrend flip in same direction as 4H established trend.
        # Extended lookback: scan last CONTINUATION_LOOKBACK_1H bars instead
        # of only the last bar, so flips that occurred between 30-min polling
        # cycles are still caught (v6.19: fixes zero-signal live trading gap).
        st_dir_1h = self._safe_last(df_1h[self.COL_SUPERTREND_DIR])

        if np.isnan(st_dir_1h):
            return self._no_signal("1H Supertrend direction is NaN")

        # Current 1H must be aligned with 4H before checking for recent flip
        if int(st_dir_1h) != established_dir:
            return self._no_signal(
                f"No 1H continuation flip: 4H_est={established_dir}, "
                f"1H cur={st_dir_1h:.0f} (not aligned)"
            )

        flip_age = self._find_recent_flip(
            df_1h[self.COL_SUPERTREND_DIR],
            expected_dir=established_dir,
            lookback=self.CONTINUATION_LOOKBACK_1H,
        )
        if flip_age is None:
            return self._no_signal(
                f"No 1H continuation flip: 4H_est={established_dir}, "
                f"1H aligned but no flip in last "
                f"{self.CONTINUATION_LOOKBACK_1H} bars"
            )

        direction = (
            SignalDirection.LONG if established_dir > 0
            else SignalDirection.SHORT
        )

        # Entry / SL / TP — use 1H ATR for continuation entries (tighter
        # stops matching the 1H entry timeframe).  4H ATR is 2.1-2.5x wider
        # and creates swing-trade level risk for what is an intra-trend entry.
        atr = self._safe_last(df_1h[self.COL_ATR])
        close = float(df_1h["close"].dropna().iloc[-1])

        if np.isnan(atr):
            return self._no_signal("ATR is NaN")

        sl_mult, tp_mult = self._get_sl_tp_mults(regime)
        sl_distance = atr * sl_mult
        tp_distance = atr * tp_mult

        if direction == SignalDirection.LONG:
            stop_loss = close - sl_distance
            take_profit = close + tp_distance
        else:
            stop_loss = close + sl_distance
            take_profit = close - tp_distance

        if stop_loss <= 0 or take_profit <= 0:
            return self._no_signal("Computed SL or TP is non-positive")

        rr = calculate_rr_ratio(close, stop_loss, take_profit)
        if rr < self.MIN_RR:
            return self._no_signal(f"R/R {rr:.2f} below 2.0 minimum")

        # Confidence: lower base for continuation (30 vs 40), no flip bonus
        # Decay for stale flips: -10% per bar of age beyond 1
        ema9 = self._safe_last(df_4h[self.COL_EMA9])
        ema21 = self._safe_last(df_4h[self.COL_EMA21])
        rsi = self._safe_last(df_4h[self.COL_RSI])
        confidence = self._compute_continuation_confidence(
            adx=adx, ema9=ema9, ema21=ema21, rsi=rsi, direction=direction,
        )
        # Decay: 100% at age=1, 90% at 2, 80% at 3, ...
        staleness_factor = max(0.6, 1.0 - (flip_age - 1) * 0.1)
        confidence = round(confidence * staleness_factor, 2)

        dir_label = direction.value.upper()
        ema_aligned = (
            (direction == SignalDirection.LONG
             and not np.isnan(ema9) and not np.isnan(ema21) and ema9 > ema21)
            or (direction == SignalDirection.SHORT
                and not np.isnan(ema9) and not np.isnan(ema21) and ema9 < ema21)
        )
        reasoning = (
            f"{dir_label} 1H continuation in established 4H trend. "
            f"4H Supertrend={established_dir} for {self.MIN_ESTABLISHED_BARS}+ bars. "
            f"1H Supertrend flipped {'bullish' if st_dir_1h > 0 else 'bearish'} "
            f"({flip_age} bar(s) ago). "
            f"ADX={adx:.1f}. "
            f"EMA alignment: {'confirmed' if ema_aligned else 'divergent'}. "
            f"SL={stop_loss:.6f}, TP={take_profit:.6f}, R/R={rr:.2f}. "
            f"Confidence: {confidence:.0f}%."
        )

        indicators_snapshot = {
            "supertrend_dir_4h": established_dir,
            "supertrend_dir_1h": int(st_dir_1h),
            "flip_age_1h": flip_age,
            "adx": round(adx, 2),
            "ema_9": round(ema9, 6) if not np.isnan(ema9) else None,
            "ema_21": round(ema21, 6) if not np.isnan(ema21) else None,
            "rsi": round(rsi, 2) if not np.isnan(rsi) else None,
            "atr": round(atr, 6),
            "atr_source": "1h",
            "close": round(close, 6),
            "entry_type": "continuation",
        }

        return Signal(
            direction=direction,
            confidence=confidence,
            entry_price=round(close, 8),
            stop_loss=round(stop_loss, 8),
            take_profit=round(take_profit, 8),
            strategy_name=self.name,
            regime="trending",
            indicators_used=indicators_snapshot,
            reasoning=reasoning,
        )

    def _compute_continuation_confidence(
        self, *, adx: float, ema9: float, ema21: float,
        rsi: float, direction: SignalDirection,
    ) -> float:
        """Confidence for 1H continuation entries (lower ceiling than flip).

        Breakdown (max 80):
          Base for continuation  : 30
          ADX strength           : up to 20
          EMA alignment          : up to 20
          RSI position           : up to 10
        """
        score = 30.0

        adx_score = min(20.0, max(0.0, (adx - self.ADX_MIN) / 22.0) * 20.0)
        score += adx_score

        if not np.isnan(ema9) and not np.isnan(ema21):
            if direction == SignalDirection.LONG and ema9 > ema21:
                score += 20.0
            elif direction == SignalDirection.SHORT and ema9 < ema21:
                score += 20.0
            else:
                score += 5.0

        if not np.isnan(rsi):
            if direction == SignalDirection.LONG and 30 < rsi < 65:
                score += 10.0
            elif direction == SignalDirection.SHORT and 35 < rsi < 70:
                score += 10.0

        return min(80.0, max(0.0, round(score, 2)))

    # ------------------------------------------------------------------
    # 15m Fast Entry (established 4H trend + 1H aligned + 15m flip)
    # ------------------------------------------------------------------

    def generate_fast_signal(
        self,
        df_4h: pd.DataFrame,
        df_1h: pd.DataFrame,
        df_15m: pd.DataFrame,
        regime: str | None = None,
    ) -> Signal:
        """Generate a fast entry via 15m Supertrend flip.

        Requires: 4H Supertrend established, 1H Supertrend aligned, and
        15m Supertrend flip in the same direction. Lower confidence ceiling
        (70) than 1H continuation (80) and 4H flip (100).

        Uses 15m ATR for SL/TP (tightest stops, matching the entry timeframe).
        """
        try:
            self._validate(df_4h)
            self._validate(df_1h)
            self._validate(df_15m)
        except (KeyError, ValueError) as exc:
            return self._no_signal(f"Fast signal validation failed: {exc}")

        # Check 4H trend is established
        st_dir_4h = df_4h[self.COL_SUPERTREND_DIR].dropna()
        if len(st_dir_4h) < self.MIN_ESTABLISHED_BARS + 1:
            return self._no_signal("Not enough 4H bars for fast entry check")

        recent_dirs = st_dir_4h.iloc[-self.MIN_ESTABLISHED_BARS:]
        if not (recent_dirs == recent_dirs.iloc[0]).all():
            return self._no_signal("4H Supertrend not established for fast entry")

        established_dir = int(recent_dirs.iloc[0])

        # ADX gate on 4H
        adx = self._safe_last(df_4h[self.COL_ADX])
        if np.isnan(adx) or adx < self.ADX_MIN:
            return self._no_signal(
                f"ADX {adx:.1f} < {self.ADX_MIN} — too weak for fast entry"
            )

        # 1H Supertrend must be aligned with 4H direction
        st_dir_1h = self._safe_last(df_1h[self.COL_SUPERTREND_DIR])
        if np.isnan(st_dir_1h) or int(st_dir_1h) != established_dir:
            return self._no_signal(
                f"1H not aligned with 4H for fast entry: "
                f"4H={established_dir}, 1H={st_dir_1h}"
            )

        # Check 15m Supertrend flip in same direction.
        # Extended lookback: scan last FAST_LOOKBACK_15M bars (45 min)
        # instead of only the last bar, so flips that occurred between
        # 30-min polling cycles are still caught (v6.19).
        st_dir_15m = self._safe_last(df_15m[self.COL_SUPERTREND_DIR])

        if np.isnan(st_dir_15m):
            return self._no_signal("15m Supertrend direction is NaN")

        # Current 15m must be aligned before checking for recent flip
        if int(st_dir_15m) != established_dir:
            return self._no_signal(
                f"15m not aligned for fast entry: 4H_est={established_dir}, "
                f"15m cur={st_dir_15m:.0f}"
            )

        flip_age = self._find_recent_flip(
            df_15m[self.COL_SUPERTREND_DIR],
            expected_dir=established_dir,
            lookback=self.FAST_LOOKBACK_15M,
        )
        if flip_age is None:
            return self._no_signal(
                f"No 15m fast flip: 4H_est={established_dir}, "
                f"15m aligned but no flip in last "
                f"{self.FAST_LOOKBACK_15M} bars"
            )

        direction = (
            SignalDirection.LONG if established_dir > 0
            else SignalDirection.SHORT
        )

        # Entry / SL / TP — use 15m ATR (tightest stops for fast entries)
        atr = self._safe_last(df_15m[self.COL_ATR])
        close = float(df_15m["close"].dropna().iloc[-1])

        if np.isnan(atr):
            return self._no_signal("15m ATR is NaN")

        sl_mult, tp_mult = self._get_sl_tp_mults(regime)
        sl_distance = atr * sl_mult
        tp_distance = atr * tp_mult

        if direction == SignalDirection.LONG:
            stop_loss = close - sl_distance
            take_profit = close + tp_distance
        else:
            stop_loss = close + sl_distance
            take_profit = close - tp_distance

        if stop_loss <= 0 or take_profit <= 0:
            return self._no_signal("Computed SL or TP is non-positive (fast)")

        rr = calculate_rr_ratio(close, stop_loss, take_profit)
        if rr < self.MIN_RR:
            return self._no_signal(f"R/R {rr:.2f} below 2.0 minimum (fast)")

        # Confidence: lower ceiling (max 70) for fast entries
        ema9 = self._safe_last(df_4h[self.COL_EMA9])
        ema21 = self._safe_last(df_4h[self.COL_EMA21])
        rsi = self._safe_last(df_4h[self.COL_RSI])
        confidence = self._compute_fast_confidence(
            adx=adx, ema9=ema9, ema21=ema21, rsi=rsi, direction=direction,
        )
        # Decay for stale 15m flips
        staleness_factor = max(0.6, 1.0 - (flip_age - 1) * 0.1)
        confidence = round(confidence * staleness_factor, 2)

        dir_label = direction.value.upper()
        reasoning = (
            f"{dir_label} 15m fast entry in established 4H+1H trend. "
            f"4H Supertrend={established_dir} for {self.MIN_ESTABLISHED_BARS}+ bars. "
            f"1H aligned. 15m Supertrend flipped "
            f"{'bullish' if st_dir_15m > 0 else 'bearish'} "
            f"({flip_age} bar(s) ago). "
            f"ADX={adx:.1f}. "
            f"SL={stop_loss:.6f}, TP={take_profit:.6f}, R/R={rr:.2f}. "
            f"ATR source: 15m. Confidence: {confidence:.0f}%."
        )

        indicators_snapshot = {
            "supertrend_dir_4h": established_dir,
            "supertrend_dir_1h": int(st_dir_1h),
            "supertrend_dir_15m": int(st_dir_15m),
            "flip_age_15m": flip_age,
            "adx": round(adx, 2),
            "atr": round(atr, 6),
            "atr_source": "15m",
            "close": round(close, 6),
            "entry_type": "fast_15m",
        }

        return Signal(
            direction=direction,
            confidence=confidence,
            entry_price=round(close, 8),
            stop_loss=round(stop_loss, 8),
            take_profit=round(take_profit, 8),
            strategy_name=self.name,
            regime="trending",
            indicators_used=indicators_snapshot,
            reasoning=reasoning,
        )

    def _compute_fast_confidence(
        self, *, adx: float, ema9: float, ema21: float,
        rsi: float, direction: SignalDirection,
    ) -> float:
        """Confidence for 15m fast entries (lower ceiling than continuation).

        Breakdown (max 70):
          Base for fast entry    : 25
          ADX strength           : up to 15
          EMA alignment          : up to 20
          RSI position           : up to 10
        """
        score = 25.0

        adx_score = min(15.0, max(0.0, (adx - self.ADX_MIN) / 22.0) * 15.0)
        score += adx_score

        if not np.isnan(ema9) and not np.isnan(ema21):
            if direction == SignalDirection.LONG and ema9 > ema21:
                score += 20.0
            elif direction == SignalDirection.SHORT and ema9 < ema21:
                score += 20.0
            else:
                score += 5.0

        if not np.isnan(rsi):
            if direction == SignalDirection.LONG and 30 < rsi < 65:
                score += 10.0
            elif direction == SignalDirection.SHORT and 35 < rsi < 70:
                score += 10.0

        return min(70.0, max(0.0, round(score, 2)))

    # ------------------------------------------------------------------
    # Aligned Trend Entry (all TFs aligned, no recent flip — momentum entry)
    # ------------------------------------------------------------------

    # RSI thresholds for pullback detection
    # v6.20: Raised LONG 45→55, lowered SHORT 55→45.
    # In bullish markets RSI stays 54-59; old 45 threshold never triggered.
    # BTC had ADX=42 (100% regime confidence) but RSI min=57.2 → zero signal.
    RSI_PULLBACK_LONG: float = 55.0   # RSI must have dipped below this
    RSI_PULLBACK_SHORT: float = 45.0  # RSI must have risen above this
    RSI_LOOKBACK_BARS: int = 5        # How many 1H bars to scan for pullback

    # Volume confirmation: current volume must exceed SMA by this factor
    ALIGNED_VOLUME_MULT: float = 0.8  # Relaxed — just needs to be near average

    def generate_aligned_signal(
        self,
        df_4h: pd.DataFrame,
        df_1h: pd.DataFrame,
        regime: str | None = None,
    ) -> Signal:
        """Generate entry when all timeframes are already aligned.

        Fires when:
        - 4H Supertrend established (N bars same direction)
        - 1H Supertrend aligned with 4H (already established, NOT a flip)
        - ADX >= 18
        - EMA alignment confirmed
        - 1H RSI pullback recovery: RSI dipped toward 50 in recent bars
          and has now recovered, indicating a trend re-entry opportunity
        - Volume near or above average

        Lower confidence ceiling (55) — this is a late momentum entry, not
        a fresh flip or continuation.  Uses 1H ATR for SL/TP.
        """
        try:
            self._validate(df_4h)
            self._validate(df_1h)
        except (KeyError, ValueError) as exc:
            return self._no_signal(f"Aligned validation failed: {exc}")

        # Check 4H trend is established
        st_dir_4h = df_4h[self.COL_SUPERTREND_DIR].dropna()
        if len(st_dir_4h) < self.MIN_ESTABLISHED_BARS + 1:
            return self._no_signal("Not enough 4H bars for aligned check")

        recent_dirs = st_dir_4h.iloc[-self.MIN_ESTABLISHED_BARS:]
        if not (recent_dirs == recent_dirs.iloc[0]).all():
            return self._no_signal("4H Supertrend not established for aligned entry")

        established_dir = int(recent_dirs.iloc[0])

        # ADX gate
        adx = self._safe_last(df_4h[self.COL_ADX])
        if np.isnan(adx) or adx < self.ADX_MIN:
            return self._no_signal(
                f"ADX {adx:.1f} < {self.ADX_MIN} — too weak for aligned entry"
            )

        # 1H must be aligned with 4H (same direction, NOT a flip)
        st_dir_1h = self._safe_last(df_1h[self.COL_SUPERTREND_DIR])
        if np.isnan(st_dir_1h) or int(st_dir_1h) != established_dir:
            return self._no_signal(
                f"1H not aligned for aligned entry: "
                f"4H={established_dir}, 1H={st_dir_1h}"
            )

        # EMA alignment on 4H
        ema9 = self._safe_last(df_4h[self.COL_EMA9])
        ema21 = self._safe_last(df_4h[self.COL_EMA21])
        if np.isnan(ema9) or np.isnan(ema21):
            return self._no_signal("EMA values are NaN for aligned entry")

        ema_aligned = (
            (established_dir > 0 and ema9 > ema21)
            or (established_dir < 0 and ema9 < ema21)
        )
        if not ema_aligned:
            return self._no_signal(
                f"EMA not aligned for aligned entry: "
                f"dir={established_dir}, EMA9={ema9:.2f}, EMA21={ema21:.2f}"
            )

        # RSI pullback-recovery filter on 1H data
        # Prevents firing every single cycle — requires a genuine dip+recovery
        rsi_1h = df_1h[self.COL_RSI].dropna()
        if len(rsi_1h) < self.RSI_LOOKBACK_BARS:
            return self._no_signal("Not enough 1H RSI bars for pullback check")

        recent_rsi = rsi_1h.iloc[-self.RSI_LOOKBACK_BARS:]
        current_rsi = float(recent_rsi.iloc[-1])

        if established_dir > 0:
            # LONG: RSI must have dipped below threshold recently AND recovered
            min_rsi = float(recent_rsi.min())
            if min_rsi >= self.RSI_PULLBACK_LONG:
                return self._no_signal(
                    f"No RSI pullback for aligned LONG: "
                    f"min_RSI={min_rsi:.1f} >= {self.RSI_PULLBACK_LONG}"
                )
            if current_rsi < self.RSI_PULLBACK_LONG:
                return self._no_signal(
                    f"RSI still in pullback for aligned LONG: "
                    f"RSI={current_rsi:.1f} < {self.RSI_PULLBACK_LONG}"
                )
        else:
            # SHORT: RSI must have spiked above threshold recently AND dropped
            max_rsi = float(recent_rsi.max())
            if max_rsi <= self.RSI_PULLBACK_SHORT:
                return self._no_signal(
                    f"No RSI pullback for aligned SHORT: "
                    f"max_RSI={max_rsi:.1f} <= {self.RSI_PULLBACK_SHORT}"
                )
            if current_rsi > self.RSI_PULLBACK_SHORT:
                return self._no_signal(
                    f"RSI still in pullback for aligned SHORT: "
                    f"RSI={current_rsi:.1f} > {self.RSI_PULLBACK_SHORT}"
                )

        # Volume check on 1H (relaxed — near average is fine)
        if "volume_sma" in df_1h.columns and "volume" in df_1h.columns:
            vol = self._safe_last(df_1h["volume"])
            vol_sma = self._safe_last(df_1h["volume_sma"])
            if not np.isnan(vol) and not np.isnan(vol_sma) and vol_sma > 0:
                if vol < vol_sma * self.ALIGNED_VOLUME_MULT:
                    return self._no_signal(
                        f"Volume too low for aligned entry: "
                        f"{vol:.0f} < {vol_sma * self.ALIGNED_VOLUME_MULT:.0f}"
                    )

        direction = (
            SignalDirection.LONG if established_dir > 0
            else SignalDirection.SHORT
        )

        # Entry / SL / TP — use 1H ATR
        atr = self._safe_last(df_1h[self.COL_ATR])
        close = float(df_1h["close"].dropna().iloc[-1])

        if np.isnan(atr):
            return self._no_signal("1H ATR is NaN for aligned entry")

        sl_mult, tp_mult = self._get_sl_tp_mults(regime)
        sl_distance = atr * sl_mult
        tp_distance = atr * tp_mult

        if direction == SignalDirection.LONG:
            stop_loss = close - sl_distance
            take_profit = close + tp_distance
        else:
            stop_loss = close + sl_distance
            take_profit = close - tp_distance

        if stop_loss <= 0 or take_profit <= 0:
            return self._no_signal("Computed SL or TP is non-positive (aligned)")

        rr = calculate_rr_ratio(close, stop_loss, take_profit)
        if rr < self.MIN_RR:
            return self._no_signal(f"R/R {rr:.2f} below 2.0 minimum (aligned)")

        # Confidence (max 55 — lowest tier)
        rsi_4h = self._safe_last(df_4h[self.COL_RSI])
        confidence = self._compute_aligned_confidence(
            adx=adx, ema9=ema9, ema21=ema21, rsi=rsi_4h,
            direction=direction,
        )

        dir_label = direction.value.upper()
        reasoning = (
            f"{dir_label} aligned trend entry (all TFs aligned, RSI pullback "
            f"recovery). 4H Supertrend={established_dir} for "
            f"{self.MIN_ESTABLISHED_BARS}+ bars. 1H aligned. "
            f"1H RSI pullback: min={float(recent_rsi.min()):.1f}, "
            f"current={current_rsi:.1f}. ADX={adx:.1f}. "
            f"EMA9={ema9:.2f} > EMA21={ema21:.2f}. "
            f"SL={stop_loss:.6f}, TP={take_profit:.6f}, R/R={rr:.2f}. "
            f"Confidence: {confidence:.0f}%."
        )

        indicators_snapshot = {
            "supertrend_dir_4h": established_dir,
            "supertrend_dir_1h": int(st_dir_1h),
            "adx": round(adx, 2),
            "ema_9": round(ema9, 6),
            "ema_21": round(ema21, 6),
            "rsi_1h": round(current_rsi, 2),
            "rsi_1h_min": round(float(recent_rsi.min()), 2),
            "atr": round(atr, 6),
            "atr_source": "1h",
            "close": round(close, 6),
            "entry_type": "aligned_trend",
        }

        return Signal(
            direction=direction,
            confidence=confidence,
            entry_price=round(close, 8),
            stop_loss=round(stop_loss, 8),
            take_profit=round(take_profit, 8),
            strategy_name=self.name,
            regime="trending",
            indicators_used=indicators_snapshot,
            reasoning=reasoning,
        )

    def _compute_aligned_confidence(
        self, *, adx: float, ema9: float, ema21: float,
        rsi: float, direction: SignalDirection,
    ) -> float:
        """Confidence for aligned trend entries (lowest ceiling: 55).

        Breakdown (max 55):
          Base for aligned entry : 20
          ADX strength           : up to 15
          EMA alignment          : up to 10 (always given — pre-checked)
          RSI position           : up to 10
        """
        score = 20.0

        adx_score = min(15.0, max(0.0, (adx - self.ADX_MIN) / 22.0) * 15.0)
        score += adx_score

        # EMA alignment is pre-checked, always give full credit
        score += 10.0

        if not np.isnan(rsi):
            if direction == SignalDirection.LONG and 30 < rsi < 65:
                score += 10.0
            elif direction == SignalDirection.SHORT and 35 < rsi < 70:
                score += 10.0

        return min(55.0, max(0.0, round(score, 2)))
