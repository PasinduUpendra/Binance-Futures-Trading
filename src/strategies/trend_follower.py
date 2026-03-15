"""
Trend-following strategy for the Claude Quant trading system.

Enters positions in the direction of an established trend confirmed by
multiple indicators:  EMA crossover, ADX filter, Supertrend alignment,
and RSI guard against buying into exhausted moves.

Indicator columns consumed (from IndicatorEngine):
  - ema_9, ema_21, ema_50, ema_200
  - adx_14
  - supertrend_14, supertrend_dir_14   (dir: 1 = bullish, -1 = bearish)
  - rsi_14
  - atr_14
  - volume
  - close
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


class TrendFollower(BaseStrategy):
    """Trend-following strategy using EMA crossover + ADX + Supertrend.

    Entry conditions
    ~~~~~~~~~~~~~~~~
    - **Long**: EMA9 > EMA21  **and**  Supertrend bullish  **and**  ADX > 25
      **and**  30 < RSI < 70
    - **Short**: EMA9 < EMA21  **and**  Supertrend bearish **and**  ADX > 25
      **and**  30 < RSI < 70

    Risk management
    ~~~~~~~~~~~~~~~
    - SL: 1.5 x ATR from entry
    - TP: 3.0 x ATR from entry  (minimum R/R = 2.0)

    Confidence is derived from ADX strength, higher-timeframe EMA alignment
    (EMA50 / EMA200), and volume relative to average.
    """

    name: str = "TrendFollower"

    # -- Column names ------------------------------------------------------
    COL_EMA9: str = "ema_9"
    COL_EMA21: str = "ema_21"
    COL_EMA50: str = "ema_50"
    COL_EMA200: str = "ema_200"
    COL_ADX: str = "adx"
    COL_SUPERTREND: str = "supertrend"
    COL_SUPERTREND_DIR: str = "supertrend_direction"
    COL_RSI: str = "rsi"
    COL_ATR: str = "atr"
    COL_VOLUME: str = "volume"
    COL_CLOSE: str = "close"

    # -- Tunables ----------------------------------------------------------
    # ADX 22 captures moderate crypto trends (ADX>25 fires only ~50% of time on 1H)
    ADX_MIN: float = 22.0
    RSI_LOW: float = 30.0
    RSI_HIGH: float = 70.0
    # Wider stops for crypto volatility: 1H ATR ~1.25% of price,
    # so 2.0x = ~2.5% SL room (1.5x was too tight, hit by normal pullbacks)
    SL_ATR_MULT: float = 2.0
    TP_ATR_MULT: float = 4.0  # Maintain 2:1 R/R with wider SL
    VOLUME_AVG_WINDOW: int = 20

    # ------------------------------------------------------------------
    # Strategy interface
    # ------------------------------------------------------------------

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """Evaluate the trend-following setup on the latest bar of *df*."""

        try:
            self._validate(df)
        except (KeyError, ValueError) as exc:
            self.logger.warning("TrendFollower validation failed: %s", exc)
            return self._no_signal(str(exc))

        # Current indicator values
        ema9 = self._safe_last(df[self.COL_EMA9])
        ema21 = self._safe_last(df[self.COL_EMA21])
        ema50 = self._safe_last(df[self.COL_EMA50])
        ema200 = self._safe_last(df[self.COL_EMA200])
        adx = self._safe_last(df[self.COL_ADX])
        st_dir = self._safe_last(df[self.COL_SUPERTREND_DIR])
        rsi = self._safe_last(df[self.COL_RSI])
        atr = self._safe_last(df[self.COL_ATR])
        close = self._safe_last(df[self.COL_CLOSE])
        volume = self._safe_last(df[self.COL_VOLUME])

        # Check for NaN in critical indicators
        for name, val in [
            ("ema9", ema9), ("ema21", ema21), ("adx", adx),
            ("supertrend_dir", st_dir), ("rsi", rsi), ("atr", atr), ("close", close),
        ]:
            if np.isnan(val):
                return self._no_signal(f"Indicator '{name}' is NaN")

        # Previous bar EMA values for crossover detection
        prev_ema9 = self._safe_prev(df[self.COL_EMA9])
        prev_ema21 = self._safe_prev(df[self.COL_EMA21])

        # --- Gate checks --------------------------------------------------
        if adx < self.ADX_MIN:
            return self._no_signal(f"ADX {adx:.1f} below minimum {self.ADX_MIN}")

        if not (self.RSI_LOW <= rsi <= self.RSI_HIGH):
            return self._no_signal(f"RSI {rsi:.1f} at extreme (outside {self.RSI_LOW}-{self.RSI_HIGH})")

        # --- Direction detection ------------------------------------------
        direction = SignalDirection.NONE

        # Long: EMA9 >= EMA21 (with recent cross or sustained) + bullish Supertrend
        ema_bullish = ema9 > ema21
        ema_cross_up = (not np.isnan(prev_ema9)) and (prev_ema9 <= prev_ema21) and ema_bullish
        supertrend_bullish = st_dir > 0  # 1 = bullish

        # Short: EMA9 <= EMA21 + bearish Supertrend
        ema_bearish = ema9 < ema21
        ema_cross_down = (not np.isnan(prev_ema9)) and (prev_ema9 >= prev_ema21) and ema_bearish
        supertrend_bearish = st_dir < 0  # -1 = bearish

        if ema_bullish and supertrend_bullish:
            direction = SignalDirection.LONG
        elif ema_bearish and supertrend_bearish:
            direction = SignalDirection.SHORT
        else:
            return self._no_signal("EMA and Supertrend do not agree on direction")

        # --- Entry / SL / TP --------------------------------------------
        entry_price = close
        sl_distance = atr * self.SL_ATR_MULT
        tp_distance = atr * self.TP_ATR_MULT

        if direction == SignalDirection.LONG:
            stop_loss = entry_price - sl_distance
            take_profit = entry_price + tp_distance
        else:
            stop_loss = entry_price + sl_distance
            take_profit = entry_price - tp_distance

        # Sanity: ensure positive prices
        if stop_loss <= 0 or take_profit <= 0:
            return self._no_signal("Computed SL or TP is non-positive")

        # R/R check (should always pass with 1.5 / 3.0 multipliers, but be safe)
        rr = calculate_rr_ratio(entry_price, stop_loss, take_profit)
        if rr < 2.0:
            return self._no_signal(f"R/R {rr:.2f} below 2.0 minimum")

        # --- Confidence scoring ------------------------------------------
        confidence = self._compute_confidence(
            adx=adx,
            ema9=ema9,
            ema21=ema21,
            ema50=ema50,
            ema200=ema200,
            direction=direction,
            volume=volume,
            df=df,
            ema_crossed=ema_cross_up if direction == SignalDirection.LONG else ema_cross_down,
        )

        # --- Reasoning string --------------------------------------------
        reasoning = self._build_reasoning(
            direction, adx, rsi, ema9, ema21, ema50, ema200,
            st_dir, close, stop_loss, take_profit, rr, confidence,
            ema_cross_up if direction == SignalDirection.LONG else ema_cross_down,
        )

        indicators_snapshot = {
            "ema_9": round(ema9, 6),
            "ema_21": round(ema21, 6),
            "ema_50": round(ema50, 6) if not np.isnan(ema50) else None,
            "ema_200": round(ema200, 6) if not np.isnan(ema200) else None,
            "adx_14": round(adx, 2),
            "rsi_14": round(rsi, 2),
            "supertrend_dir": int(st_dir),
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
            regime="trending",
            indicators_used=indicators_snapshot,
            reasoning=reasoning,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _validate(self, df: pd.DataFrame) -> None:
        required = [
            self.COL_EMA9, self.COL_EMA21, self.COL_EMA50, self.COL_EMA200,
            self.COL_ADX, self.COL_SUPERTREND, self.COL_SUPERTREND_DIR,
            self.COL_RSI, self.COL_ATR, self.COL_VOLUME, self.COL_CLOSE,
        ]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise KeyError(f"Missing columns: {missing}")
        if len(df) < 2:
            raise ValueError("Need at least 2 rows for crossover detection")

    def _compute_confidence(
        self,
        *,
        adx: float,
        ema9: float,
        ema21: float,
        ema50: float,
        ema200: float,
        direction: SignalDirection,
        volume: float,
        df: pd.DataFrame,
        ema_crossed: bool,
    ) -> float:
        """Compute a 0-100 confidence score from multiple factors.

        Breakdown (max 100):
          ADX strength        : up to 30
          EMA alignment (50/200): up to 25
          Fresh EMA crossover : up to 15
          Volume              : up to 20
          RSI mid-range bonus : up to 10
        """
        score = 0.0

        # 1. ADX strength (25-50 mapped to 0-30)
        adx_score = min(30.0, max(0.0, (adx - self.ADX_MIN) / 25.0) * 30.0)
        score += adx_score

        # 2. Higher-timeframe EMA alignment
        if not np.isnan(ema50) and not np.isnan(ema200):
            if direction == SignalDirection.LONG:
                if ema9 > ema21 > ema50 > ema200:
                    score += 25.0  # perfect bullish alignment
                elif ema9 > ema21 > ema50:
                    score += 18.0
                elif ema9 > ema50:
                    score += 10.0
            else:  # SHORT
                if ema9 < ema21 < ema50 < ema200:
                    score += 25.0  # perfect bearish alignment
                elif ema9 < ema21 < ema50:
                    score += 18.0
                elif ema9 < ema50:
                    score += 10.0
        elif not np.isnan(ema50):
            # Only EMA50 available
            aligned = (direction == SignalDirection.LONG and ema9 > ema50) or (
                direction == SignalDirection.SHORT and ema9 < ema50
            )
            score += 12.0 if aligned else 0.0

        # 3. Fresh EMA crossover (more actionable)
        if ema_crossed:
            score += 15.0

        # 4. Volume (above 20-period average is bullish for trend)
        vol_series = df[self.COL_VOLUME].dropna()
        if len(vol_series) >= self.VOLUME_AVG_WINDOW:
            vol_avg = vol_series.iloc[-self.VOLUME_AVG_WINDOW:].mean()
            if vol_avg > 0:
                vol_ratio = volume / vol_avg
                vol_score = min(20.0, max(0.0, (vol_ratio - 0.5) / 1.5) * 20.0)
                score += vol_score
        else:
            score += 10.0  # neutral if insufficient history

        # 5. RSI mid-range bonus (further from extremes = stronger confirmation)
        rsi = self._safe_last(df[self.COL_RSI])
        if not np.isnan(rsi):
            mid_dist = abs(rsi - 50.0)
            # Within 10 of 50 gets full bonus; linearly decays to 0 at 20 from 50
            rsi_bonus = max(0.0, 10.0 - mid_dist * 0.5)
            score += rsi_bonus

        return min(100.0, max(0.0, round(score, 2)))

    def _build_reasoning(
        self,
        direction: SignalDirection,
        adx: float,
        rsi: float,
        ema9: float,
        ema21: float,
        ema50: float,
        ema200: float,
        st_dir: float,
        close: float,
        sl: float,
        tp: float,
        rr: float,
        confidence: float,
        fresh_cross: bool,
    ) -> str:
        parts: list[str] = []
        dir_label = direction.value.upper()
        parts.append(f"{dir_label} trend signal at {close:.6f}.")

        if fresh_cross:
            parts.append(f"Fresh EMA9/21 crossover detected.")
        parts.append(f"EMA9={ema9:.6f}, EMA21={ema21:.6f}.")

        if not np.isnan(ema50):
            parts.append(f"EMA50={ema50:.6f}.")
        if not np.isnan(ema200):
            parts.append(f"EMA200={ema200:.6f}.")

        st_label = "bullish" if st_dir > 0 else "bearish"
        parts.append(f"Supertrend is {st_label}.")
        parts.append(f"ADX={adx:.1f} confirms trend strength.")
        parts.append(f"RSI={rsi:.1f} in safe zone.")
        parts.append(f"SL={sl:.6f}, TP={tp:.6f}, R/R={rr:.2f}.")
        parts.append(f"Confidence: {confidence:.0f}%.")

        return " ".join(parts)
