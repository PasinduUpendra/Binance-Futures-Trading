"""
Mean-reversion strategy for the Claude Quant trading system.

Trades reversion to the mean when price is at statistical extremes
(Bollinger Bands + Z-score + RSI) while the market is range-bound
(ADX filter).

Indicator columns consumed (from IndicatorEngine):
  - bb_upper_20, bb_lower_20, bb_mid_20
  - zscore_20       : rolling Z-score of close (20-period)
  - rsi_14
  - adx_14
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


class MeanReversion(BaseStrategy):
    """Mean-reversion strategy using BB + Z-score + RSI in ranging markets.

    Entry conditions
    ~~~~~~~~~~~~~~~~
    - **Long**: price at/below lower BB  **and**  Z-score < -2  **and**
      RSI < 30  **and**  ADX < 20
    - **Short**: price at/above upper BB  **and**  Z-score > 2  **and**
      RSI > 70  **and**  ADX < 20

    Risk management
    ~~~~~~~~~~~~~~~
    - SL: 0.5 % beyond the touched Bollinger Band
    - TP (conservative): BB middle band
    - TP (aggressive): opposite BB band
    - The strategy uses conservative TP by default and switches to aggressive
      only when conditions are extremely stretched (|Z| > 2.5 and RSI beyond 25/75).

    Confidence scoring
    ~~~~~~~~~~~~~~~~~~
    Derived from Z-score extremity, RSI divergence, and volume drying up
    (a low-volume extreme is more likely to revert).
    """

    name: str = "MeanReversion"

    # -- Column names ------------------------------------------------------
    COL_BB_UPPER: str = "bb_upper"
    COL_BB_LOWER: str = "bb_lower"
    COL_BB_MID: str = "bb_middle"
    COL_ZSCORE: str = "zscore"
    COL_RSI: str = "rsi"
    COL_ADX: str = "adx"
    COL_ATR: str = "atr"
    COL_VOLUME: str = "volume"
    COL_CLOSE: str = "close"

    # -- Tunables ----------------------------------------------------------
    ADX_MAX: float = 20.0
    ZSCORE_THRESHOLD: float = 2.0
    ZSCORE_AGGRESSIVE: float = 2.5
    RSI_OVERSOLD: float = 30.0
    RSI_OVERBOUGHT: float = 70.0
    RSI_EXTREME_OVERSOLD: float = 25.0
    RSI_EXTREME_OVERBOUGHT: float = 75.0
    SL_BEYOND_BB_PCT: float = 0.005  # 0.5 %
    VOLUME_AVG_WINDOW: int = 20

    # ------------------------------------------------------------------
    # Strategy interface
    # ------------------------------------------------------------------

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        try:
            self._validate(df)
        except (KeyError, ValueError) as exc:
            self.logger.warning("MeanReversion validation failed: %s", exc)
            return self._no_signal(str(exc))

        close = self._safe_last(df[self.COL_CLOSE])
        bb_upper = self._safe_last(df[self.COL_BB_UPPER])
        bb_lower = self._safe_last(df[self.COL_BB_LOWER])
        bb_mid = self._safe_last(df[self.COL_BB_MID])
        zscore = self._safe_last(df[self.COL_ZSCORE])
        rsi = self._safe_last(df[self.COL_RSI])
        adx = self._safe_last(df[self.COL_ADX])
        atr = self._safe_last(df[self.COL_ATR])
        volume = self._safe_last(df[self.COL_VOLUME])

        for name, val in [
            ("close", close), ("bb_upper", bb_upper), ("bb_lower", bb_lower),
            ("bb_mid", bb_mid), ("zscore", zscore), ("rsi", rsi),
            ("adx", adx), ("atr", atr),
        ]:
            if np.isnan(val):
                return self._no_signal(f"Indicator '{name}' is NaN")

        # --- Gate: ranging market only ------------------------------------
        if adx > self.ADX_MAX:
            return self._no_signal(f"ADX {adx:.1f} > {self.ADX_MAX} — market trending, skip MR")

        # --- Direction detection ------------------------------------------
        direction = SignalDirection.NONE

        long_setup = (
            close <= bb_lower
            and zscore <= -self.ZSCORE_THRESHOLD
            and rsi <= self.RSI_OVERSOLD
        )
        short_setup = (
            close >= bb_upper
            and zscore >= self.ZSCORE_THRESHOLD
            and rsi >= self.RSI_OVERBOUGHT
        )

        if long_setup:
            direction = SignalDirection.LONG
        elif short_setup:
            direction = SignalDirection.SHORT
        else:
            return self._no_signal(
                f"No MR setup: close={close:.6f}, BB=[{bb_lower:.6f},{bb_upper:.6f}], "
                f"Z={zscore:.2f}, RSI={rsi:.1f}"
            )

        # --- Aggressive vs conservative target ----------------------------
        use_aggressive = self._is_extreme(zscore, rsi, direction)

        # --- Entry / SL / TP --------------------------------------------
        entry_price = close

        if direction == SignalDirection.LONG:
            stop_loss = bb_lower * (1.0 - self.SL_BEYOND_BB_PCT)
            take_profit = bb_upper if use_aggressive else bb_mid
        else:
            stop_loss = bb_upper * (1.0 + self.SL_BEYOND_BB_PCT)
            take_profit = bb_lower if use_aggressive else bb_mid

        if stop_loss <= 0 or take_profit <= 0:
            return self._no_signal("Computed SL or TP is non-positive")

        # R/R check — mean reversion may not always clear 2.0
        rr = calculate_rr_ratio(entry_price, stop_loss, take_profit)
        if rr < 2.0:
            # Attempt aggressive TP to improve R/R
            if direction == SignalDirection.LONG:
                take_profit = bb_upper
            else:
                take_profit = bb_lower
            rr = calculate_rr_ratio(entry_price, stop_loss, take_profit)
            if rr < 2.0:
                # Last resort: extend TP to ATR-based target
                tp_distance = atr * 3.0
                if direction == SignalDirection.LONG:
                    take_profit = entry_price + tp_distance
                else:
                    take_profit = entry_price - tp_distance
                rr = calculate_rr_ratio(entry_price, stop_loss, take_profit)
                if rr < 2.0:
                    return self._no_signal(
                        f"Cannot achieve 2.0 R/R even with extended TP (best: {rr:.2f})"
                    )

        # --- Confidence scoring ------------------------------------------
        confidence = self._compute_confidence(
            zscore=zscore,
            rsi=rsi,
            adx=adx,
            volume=volume,
            df=df,
            direction=direction,
        )

        # --- Reasoning ---------------------------------------------------
        tp_mode = "aggressive (opposite BB)" if use_aggressive else "conservative (middle BB)"
        reasoning = self._build_reasoning(
            direction, close, bb_upper, bb_lower, bb_mid, zscore, rsi, adx,
            stop_loss, take_profit, rr, confidence, tp_mode,
        )

        indicators_snapshot = {
            "bb_upper_20": round(bb_upper, 6),
            "bb_lower_20": round(bb_lower, 6),
            "bb_mid_20": round(bb_mid, 6),
            "zscore_20": round(zscore, 4),
            "rsi_14": round(rsi, 2),
            "adx_14": round(adx, 2),
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
            regime="ranging",
            indicators_used=indicators_snapshot,
            reasoning=reasoning,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _validate(self, df: pd.DataFrame) -> None:
        required = [
            self.COL_BB_UPPER, self.COL_BB_LOWER, self.COL_BB_MID,
            self.COL_ZSCORE, self.COL_RSI, self.COL_ADX,
            self.COL_ATR, self.COL_VOLUME, self.COL_CLOSE,
        ]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise KeyError(f"Missing columns: {missing}")
        if len(df) < 2:
            raise ValueError("Need at least 2 rows")

    def _is_extreme(self, zscore: float, rsi: float, direction: SignalDirection) -> bool:
        """True when the move is extreme enough to justify an aggressive TP."""
        if direction == SignalDirection.LONG:
            return abs(zscore) >= self.ZSCORE_AGGRESSIVE and rsi <= self.RSI_EXTREME_OVERSOLD
        return zscore >= self.ZSCORE_AGGRESSIVE and rsi >= self.RSI_EXTREME_OVERBOUGHT

    def _compute_confidence(
        self,
        *,
        zscore: float,
        rsi: float,
        adx: float,
        volume: float,
        df: pd.DataFrame,
        direction: SignalDirection,
    ) -> float:
        """Compute 0-100 confidence.

        Breakdown (max 100):
          Z-score extremity   : up to 35
          RSI extremity       : up to 25
          ADX flatness        : up to 15
          Volume drying up    : up to 15
          Multi-bar extreme   : up to 10
        """
        score = 0.0

        # 1. Z-score (|2| -> 15, |3| -> 35)
        z_abs = abs(zscore)
        z_score = min(35.0, max(0.0, (z_abs - self.ZSCORE_THRESHOLD) / 1.0) * 20.0 + 15.0)
        if z_abs < self.ZSCORE_THRESHOLD:
            z_score = 0.0
        score += z_score

        # 2. RSI extremity
        if direction == SignalDirection.LONG:
            rsi_ext = max(0.0, (self.RSI_OVERSOLD - rsi) / self.RSI_OVERSOLD) * 25.0
        else:
            rsi_ext = max(0.0, (rsi - self.RSI_OVERBOUGHT) / (100.0 - self.RSI_OVERBOUGHT)) * 25.0
        score += min(25.0, rsi_ext)

        # 3. ADX flatness (lower ADX = better for MR)
        adx_bonus = max(0.0, (self.ADX_MAX - adx) / self.ADX_MAX) * 15.0
        score += min(15.0, adx_bonus)

        # 4. Volume drying up (low volume at extreme = more likely reversion)
        vol_series = df[self.COL_VOLUME].dropna()
        if len(vol_series) >= self.VOLUME_AVG_WINDOW:
            vol_avg = vol_series.iloc[-self.VOLUME_AVG_WINDOW:].mean()
            if vol_avg > 0 and not np.isnan(volume):
                vol_ratio = volume / vol_avg
                # Lower volume = higher score (caps at ratio ~0.5)
                vol_bonus = max(0.0, (1.0 - vol_ratio) / 0.5) * 15.0
                score += min(15.0, max(0.0, vol_bonus))

        # 5. Multi-bar extreme (price has been outside BB for 2+ bars)
        close_series = df[self.COL_CLOSE].dropna()
        if len(close_series) >= 3:
            if direction == SignalDirection.LONG:
                bb_low = df[self.COL_BB_LOWER].dropna()
                if len(bb_low) >= 3:
                    bars_below = sum(
                        1 for i in range(-3, 0)
                        if close_series.iloc[i] <= bb_low.iloc[i]
                    )
                    score += min(10.0, bars_below * 5.0)
            else:
                bb_high = df[self.COL_BB_UPPER].dropna()
                if len(bb_high) >= 3:
                    bars_above = sum(
                        1 for i in range(-3, 0)
                        if close_series.iloc[i] >= bb_high.iloc[i]
                    )
                    score += min(10.0, bars_above * 5.0)

        return min(100.0, max(0.0, round(score, 2)))

    def _build_reasoning(
        self,
        direction: SignalDirection,
        close: float,
        bb_upper: float,
        bb_lower: float,
        bb_mid: float,
        zscore: float,
        rsi: float,
        adx: float,
        sl: float,
        tp: float,
        rr: float,
        confidence: float,
        tp_mode: str,
    ) -> str:
        dir_label = direction.value.upper()
        band = "lower" if direction == SignalDirection.LONG else "upper"
        parts = [
            f"{dir_label} mean-reversion signal at {close:.6f}.",
            f"Price at {band} Bollinger Band ({bb_lower:.6f} / {bb_upper:.6f}).",
            f"Z-score={zscore:.2f}, RSI={rsi:.1f} confirming extreme.",
            f"ADX={adx:.1f} confirms ranging market.",
            f"TP mode: {tp_mode}. BB mid={bb_mid:.6f}.",
            f"SL={sl:.6f}, TP={tp:.6f}, R/R={rr:.2f}.",
            f"Confidence: {confidence:.0f}%.",
        ]
        return " ".join(parts)
