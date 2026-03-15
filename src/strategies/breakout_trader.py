"""
Breakout trading strategy for the Claude Quant trading system.

Identifies support/resistance levels from recent swing highs/lows,
then enters when price breaks through with volume confirmation and a
Bollinger Band squeeze release.

Indicator columns consumed (from IndicatorEngine):
  - bb_upper_20, bb_lower_20, bb_mid_20
  - atr_14
  - adx_14
  - volume
  - close, high, low, open
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


class BreakoutTrader(BaseStrategy):
    """Breakout strategy using S/R levels + volume + BB squeeze release.

    Entry conditions
    ~~~~~~~~~~~~~~~~
    - **Long**: close breaks above resistance  **and**  volume > 2x avg
      **and**  BB expanding (squeeze release)
    - **Short**: close breaks below support  **and**  volume > 2x avg
      **and**  BB expanding (squeeze release)

    Support / Resistance
    ~~~~~~~~~~~~~~~~~~~~
    Identified from swing highs and swing lows over the last
    ``SR_LOOKBACK`` candles (default 50).  A swing high/low requires
    *n* bars on each side (``SWING_ORDER``, default 5).

    Risk management
    ~~~~~~~~~~~~~~~
    - SL: just inside the breakout level (below resistance for longs,
      above support for shorts) offset by ``SL_BUFFER_ATR_MULT * ATR``
    - TP: 1:3 R/R target

    Confidence scoring
    ~~~~~~~~~~~~~~~~~~
    Derived from volume surge strength, time spent in the squeeze,
    and the number of previous tests of the broken level.
    """

    name: str = "BreakoutTrader"

    # -- Column names ------------------------------------------------------
    COL_BB_UPPER: str = "bb_upper"
    COL_BB_LOWER: str = "bb_lower"
    COL_BB_MID: str = "bb_middle"
    COL_ATR: str = "atr"
    COL_ADX: str = "adx"
    COL_VOLUME: str = "volume"
    COL_CLOSE: str = "close"
    COL_HIGH: str = "high"
    COL_LOW: str = "low"
    COL_OPEN: str = "open"

    # -- Tunables ----------------------------------------------------------
    SR_LOOKBACK: int = 50
    SWING_ORDER: int = 3          # bars on each side (was 5, too strict for crypto)
    VOLUME_SURGE_MULT: float = 1.3  # Crypto volume is noisy; 2x was too strict
    VOLUME_AVG_WINDOW: int = 20
    BB_SQUEEZE_PERCENTILE: float = 20.0  # BB width below this % = squeeze
    BB_EXPANSION_MULT: float = 1.05      # lowered from 1.1 for more sensitivity
    SL_BUFFER_ATR_MULT: float = 0.5      # how far inside breakout level
    RR_TARGET: float = 3.0               # reward / risk target
    SR_PROXIMITY_PCT: float = 0.003      # 0.3% for clustering (wider for crypto)
    MIN_SR_TESTS: int = 1                # minimum touches to validate a level

    # ------------------------------------------------------------------
    # Strategy interface
    # ------------------------------------------------------------------

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        try:
            self._validate(df)
        except (KeyError, ValueError) as exc:
            self.logger.warning("BreakoutTrader validation failed: %s", exc)
            return self._no_signal(str(exc))

        close = self._safe_last(df[self.COL_CLOSE])
        prev_close = self._safe_prev(df[self.COL_CLOSE])
        atr = self._safe_last(df[self.COL_ATR])
        volume = self._safe_last(df[self.COL_VOLUME])

        for name, val in [("close", close), ("prev_close", prev_close), ("atr", atr)]:
            if np.isnan(val):
                return self._no_signal(f"Indicator '{name}' is NaN")

        # --- Volume surge check -------------------------------------------
        vol_avg = self._volume_average(df)
        if vol_avg <= 0 or np.isnan(volume):
            return self._no_signal("Cannot compute volume average")

        vol_ratio = volume / vol_avg
        has_volume_surge = vol_ratio >= self.VOLUME_SURGE_MULT

        # --- BB squeeze release -------------------------------------------
        has_bb_release = self._bb_squeeze_releasing(df)

        # Require at least one of: volume surge OR BB squeeze release
        # (requiring both simultaneously is too restrictive for crypto)
        if not has_volume_surge and not has_bb_release:
            return self._no_signal(
                f"Neither volume surge ({vol_ratio:.2f}x < {self.VOLUME_SURGE_MULT}x) "
                f"nor BB squeeze release detected"
            )

        # --- Support / Resistance identification --------------------------
        resistances, supports = self._find_sr_levels(df)

        if not resistances and not supports:
            return self._no_signal("No S/R levels identified in lookback window")

        # --- Breakout detection -------------------------------------------
        direction = SignalDirection.NONE
        broken_level: float = 0.0
        level_tests: int = 0

        # Long breakout: current close above resistance, previous close was at or below
        for level, tests in resistances:
            if close > level >= prev_close:
                direction = SignalDirection.LONG
                broken_level = level
                level_tests = tests
                break

        # Short breakout: current close below support, previous close was at or above
        if direction == SignalDirection.NONE:
            for level, tests in supports:
                if close < level <= prev_close:
                    direction = SignalDirection.SHORT
                    broken_level = level
                    level_tests = tests
                    break

        if direction == SignalDirection.NONE:
            return self._no_signal(
                f"No breakout detected. Resistances: {[r[0] for r in resistances[:3]]}, "
                f"Supports: {[s[0] for s in supports[:3]]}"
            )

        # --- Entry / SL / TP --------------------------------------------
        entry_price = close
        sl_buffer = atr * self.SL_BUFFER_ATR_MULT

        if direction == SignalDirection.LONG:
            # SL just below the broken resistance level
            stop_loss = broken_level - sl_buffer
            risk = entry_price - stop_loss
            if risk <= 0:
                return self._no_signal("Long SL computation yielded non-positive risk")
            take_profit = entry_price + (risk * self.RR_TARGET)
        else:
            # SL just above the broken support level
            stop_loss = broken_level + sl_buffer
            risk = stop_loss - entry_price
            if risk <= 0:
                return self._no_signal("Short SL computation yielded non-positive risk")
            take_profit = entry_price - (risk * self.RR_TARGET)

        if stop_loss <= 0 or take_profit <= 0:
            return self._no_signal("Computed SL or TP is non-positive")

        rr = calculate_rr_ratio(entry_price, stop_loss, take_profit)
        if rr < 2.0:
            return self._no_signal(f"R/R {rr:.2f} below 2.0 minimum")

        # --- Confidence scoring ------------------------------------------
        squeeze_bars = self._count_squeeze_bars(df)
        confidence = self._compute_confidence(
            vol_ratio=vol_ratio,
            squeeze_bars=squeeze_bars,
            level_tests=level_tests,
            atr=atr,
            entry_price=entry_price,
            broken_level=broken_level,
        )

        # --- Reasoning ---------------------------------------------------
        reasoning = self._build_reasoning(
            direction, close, broken_level, level_tests,
            vol_ratio, squeeze_bars, stop_loss, take_profit,
            rr, confidence,
        )

        indicators_snapshot = {
            "broken_level": round(broken_level, 6),
            "level_tests": level_tests,
            "volume_ratio": round(vol_ratio, 2),
            "squeeze_bars": squeeze_bars,
            "atr_14": round(atr, 6),
            "close": round(close, 6),
        }

        return Signal(
            direction=direction,
            confidence=confidence,
            entry_price=round(entry_price, 8),
            stop_loss=round(stop_loss, 8),
            take_profit=round(take_profit, 8),
            strategy_name=self.name,
            regime="volatile",
            indicators_used=indicators_snapshot,
            reasoning=reasoning,
        )

    # ------------------------------------------------------------------
    # S/R level identification
    # ------------------------------------------------------------------

    def _find_sr_levels(
        self, df: pd.DataFrame
    ) -> tuple[list[tuple[float, int]], list[tuple[float, int]]]:
        """Return (resistances, supports) as lists of (price, touch_count).

        Each list is sorted by proximity to the current close (nearest first).
        """
        lookback = df.iloc[-self.SR_LOOKBACK:]
        highs = lookback[self.COL_HIGH].values
        lows = lookback[self.COL_LOW].values
        n = self.SWING_ORDER

        swing_highs: list[float] = []
        swing_lows: list[float] = []

        for i in range(n, len(highs) - n):
            # Swing high: highs[i] is the max in the window
            if highs[i] == max(highs[i - n : i + n + 1]):
                swing_highs.append(float(highs[i]))
            # Swing low: lows[i] is the min in the window
            if lows[i] == min(lows[i - n : i + n + 1]):
                swing_lows.append(float(lows[i]))

        # Cluster nearby levels
        resistance_levels = self._cluster_levels(swing_highs)
        support_levels = self._cluster_levels(swing_lows)

        # Filter by minimum number of tests
        resistance_levels = [(p, c) for p, c in resistance_levels if c >= self.MIN_SR_TESTS]
        support_levels = [(p, c) for p, c in support_levels if c >= self.MIN_SR_TESTS]

        close = self._safe_last(df[self.COL_CLOSE])

        # Filter: resistances above prev close, supports below prev close
        prev_close = self._safe_prev(df[self.COL_CLOSE])
        if not np.isnan(prev_close):
            resistance_levels = [(p, c) for p, c in resistance_levels if p >= prev_close * 0.998]
            support_levels = [(p, c) for p, c in support_levels if p <= prev_close * 1.002]

        # Sort by proximity to close
        resistance_levels.sort(key=lambda x: abs(x[0] - close))
        support_levels.sort(key=lambda x: abs(x[0] - close))

        return resistance_levels, support_levels

    def _cluster_levels(self, prices: list[float]) -> list[tuple[float, int]]:
        """Cluster nearby price levels and return (avg_price, count)."""
        if not prices:
            return []

        sorted_prices = sorted(prices)
        clusters: list[list[float]] = [[sorted_prices[0]]]

        for price in sorted_prices[1:]:
            # If within SR_PROXIMITY_PCT of the cluster centre, merge
            cluster_avg = sum(clusters[-1]) / len(clusters[-1])
            if abs(price - cluster_avg) / cluster_avg <= self.SR_PROXIMITY_PCT:
                clusters[-1].append(price)
            else:
                clusters.append([price])

        return [
            (sum(cluster) / len(cluster), len(cluster))
            for cluster in clusters
        ]

    # ------------------------------------------------------------------
    # BB squeeze detection
    # ------------------------------------------------------------------

    def _bb_squeeze_releasing(self, df: pd.DataFrame) -> bool:
        """True if BB width is expanding after a squeeze period."""
        bb_width = df[self.COL_BB_UPPER] - df[self.COL_BB_LOWER]
        bb_width_clean = bb_width.dropna()

        if len(bb_width_clean) < 20:
            return False

        # Check if recently we were in a squeeze (low BB width)
        recent_width = bb_width_clean.iloc[-20:]
        percentile_val = np.percentile(bb_width_clean.values, self.BB_SQUEEZE_PERCENTILE)

        # Was in squeeze: some of the recent bars had width below the squeeze percentile
        bars_in_squeeze = (recent_width < percentile_val).sum()
        if bars_in_squeeze < 3:
            return False

        # Is expanding: current width > previous bar width * multiplier
        current_width = float(bb_width_clean.iloc[-1])
        prev_width = float(bb_width_clean.iloc[-2])
        if prev_width <= 0:
            return False

        return current_width > prev_width * self.BB_EXPANSION_MULT

    def _count_squeeze_bars(self, df: pd.DataFrame) -> int:
        """Count how many of the last 20 bars were in a squeeze."""
        bb_width = (df[self.COL_BB_UPPER] - df[self.COL_BB_LOWER]).dropna()
        if len(bb_width) < 20:
            return 0

        percentile_val = np.percentile(bb_width.values, self.BB_SQUEEZE_PERCENTILE)
        recent = bb_width.iloc[-20:]
        return int((recent < percentile_val).sum())

    # ------------------------------------------------------------------
    # Volume helper
    # ------------------------------------------------------------------

    def _volume_average(self, df: pd.DataFrame) -> float:
        vol = df[self.COL_VOLUME].dropna()
        if len(vol) < self.VOLUME_AVG_WINDOW:
            return float(vol.mean()) if len(vol) > 0 else 0.0
        return float(vol.iloc[-self.VOLUME_AVG_WINDOW:].mean())

    # ------------------------------------------------------------------
    # Confidence scoring
    # ------------------------------------------------------------------

    def _compute_confidence(
        self,
        *,
        vol_ratio: float,
        squeeze_bars: int,
        level_tests: int,
        atr: float,
        entry_price: float,
        broken_level: float,
    ) -> float:
        """Compute 0-100 confidence.

        Breakdown (max 100):
          Volume surge strength  : up to 30
          Time in squeeze        : up to 25
          Number of S/R tests    : up to 25
          Breakout magnitude     : up to 20
        """
        score = 0.0

        # 1. Volume surge (2x -> 15, 3x -> 25, 4x+ -> 30)
        vol_score = min(30.0, max(0.0, (vol_ratio - self.VOLUME_SURGE_MULT) / 2.0) * 15.0 + 15.0)
        if vol_ratio < self.VOLUME_SURGE_MULT:
            vol_score = 0.0
        score += vol_score

        # 2. Squeeze duration (3 bars -> 10, 8+ bars -> 25)
        squeeze_score = min(25.0, max(0.0, (squeeze_bars - 2) / 6.0) * 15.0 + 10.0)
        if squeeze_bars < 3:
            squeeze_score = 0.0
        score += squeeze_score

        # 3. Level tests (more tests = stronger level = more significant breakout)
        test_score = min(25.0, level_tests * 8.0)
        score += test_score

        # 4. Breakout magnitude (how far past the level relative to ATR)
        if atr > 0:
            breakout_dist = abs(entry_price - broken_level) / atr
            mag_score = min(20.0, breakout_dist * 15.0)
            score += mag_score

        return min(100.0, max(0.0, round(score, 2)))

    # ------------------------------------------------------------------
    # Validation & formatting
    # ------------------------------------------------------------------

    def _validate(self, df: pd.DataFrame) -> None:
        required = [
            self.COL_BB_UPPER, self.COL_BB_LOWER, self.COL_BB_MID,
            self.COL_ATR, self.COL_ADX, self.COL_VOLUME,
            self.COL_CLOSE, self.COL_HIGH, self.COL_LOW, self.COL_OPEN,
        ]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise KeyError(f"Missing columns: {missing}")
        if len(df) < self.SR_LOOKBACK:
            raise ValueError(
                f"Need at least {self.SR_LOOKBACK} rows for S/R identification, "
                f"got {len(df)}"
            )

    def _build_reasoning(
        self,
        direction: SignalDirection,
        close: float,
        broken_level: float,
        level_tests: int,
        vol_ratio: float,
        squeeze_bars: int,
        sl: float,
        tp: float,
        rr: float,
        confidence: float,
    ) -> str:
        dir_label = direction.value.upper()
        level_type = "resistance" if direction == SignalDirection.LONG else "support"
        parts = [
            f"{dir_label} breakout signal at {close:.6f}.",
            f"Broke {level_type} at {broken_level:.6f} (tested {level_tests}x).",
            f"Volume surge: {vol_ratio:.1f}x average.",
            f"BB squeeze release after {squeeze_bars} bars in squeeze.",
            f"SL={sl:.6f}, TP={tp:.6f}, R/R={rr:.2f}.",
            f"Confidence: {confidence:.0f}%.",
        ]
        return " ".join(parts)
