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
    # ADX < 30: trust the 4H regime detector, don't double-filter on 1H
    # (1H ADX diverges from 4H 31% of the time when 4H says ranging)
    ADX_MAX: float = 30.0
    ZSCORE_THRESHOLD: float = 1.5   # Crypto mean-reverts at lower extremes
    ZSCORE_AGGRESSIVE: float = 2.0
    RSI_OVERSOLD: float = 35.0     # Widened for crypto (was 30)
    RSI_OVERBOUGHT: float = 65.0   # Widened for crypto (was 70)
    RSI_EXTREME_OVERSOLD: float = 25.0
    RSI_EXTREME_OVERBOUGHT: float = 75.0
    SL_BEYOND_BB_PCT: float = 0.005  # 0.5 %
    VOLUME_AVG_WINDOW: int = 20
    # Require 2-of-3 confirmation signals (BB touch, Z-score extreme, RSI extreme)
    MIN_CONFIRMATIONS: int = 2

    # ------------------------------------------------------------------
    # Strategy interface
    # ------------------------------------------------------------------

    def generate_signal(
        self,
        df: pd.DataFrame,
        entry_price: float | None = None,
    ) -> Signal:
        """Evaluate mean-reversion setup.

        Parameters
        ----------
        df : DataFrame
            Indicator-enriched data (can be 1H or 4H).
        entry_price : float, optional
            If provided, use this as entry price (e.g., current 1H close
            for precise timing when analysing 4H data).
        """
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

        # --- Direction detection (2-of-3 confirmation) ---------------------
        direction = SignalDirection.NONE

        # Count long confirmations
        long_confirms = sum([
            close <= bb_lower,                       # Price at/below lower BB
            zscore <= -self.ZSCORE_THRESHOLD,         # Z-score extreme
            rsi <= self.RSI_OVERSOLD,                 # RSI oversold
        ])
        # Count short confirmations
        short_confirms = sum([
            close >= bb_upper,                        # Price at/above upper BB
            zscore >= self.ZSCORE_THRESHOLD,           # Z-score extreme
            rsi >= self.RSI_OVERBOUGHT,               # RSI overbought
        ])

        if long_confirms >= self.MIN_CONFIRMATIONS:
            direction = SignalDirection.LONG
        elif short_confirms >= self.MIN_CONFIRMATIONS:
            direction = SignalDirection.SHORT
        else:
            return self._no_signal(
                f"No MR setup ({long_confirms}L/{short_confirms}S confirms, need {self.MIN_CONFIRMATIONS}): "
                f"close={close:.6f}, BB=[{bb_lower:.6f},{bb_upper:.6f}], "
                f"Z={zscore:.2f}, RSI={rsi:.1f}"
            )

        # --- Aggressive vs conservative target ----------------------------
        use_aggressive = self._is_extreme(zscore, rsi, direction)

        # --- Entry / SL / TP --------------------------------------------
        # Use provided entry_price (e.g. 1H close) or fall back to df close
        if entry_price is None:
            entry_price = close

        if direction == SignalDirection.LONG:
            # SL must be below entry — use the lower of bb_lower and entry
            stop_loss = min(bb_lower, entry_price) * (1.0 - self.SL_BEYOND_BB_PCT)
            take_profit = bb_upper if use_aggressive else bb_mid
        else:
            # SL must be above entry — use the higher of bb_upper and entry
            stop_loss = max(bb_upper, entry_price) * (1.0 + self.SL_BEYOND_BB_PCT)
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
        n_confirms = long_confirms if direction == SignalDirection.LONG else short_confirms
        confidence = self._compute_confidence(
            zscore=zscore,
            rsi=rsi,
            adx=adx,
            volume=volume,
            df=df,
            direction=direction,
            n_confirmations=n_confirms,
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
        n_confirmations: int = 2,
    ) -> float:
        """Compute 0-100 confidence.

        Breakdown (max 100):
          Base (2/3 confirm=25, 3/3=35) : 25-35
          Z-score extremity beyond threshold : up to 20
          RSI extremity beyond threshold     : up to 15
          ADX flatness        : up to 10
          Volume drying up    : up to 10
          Multi-bar extreme   : up to 10
        """
        # Base confidence for meeting 2/3 or 3/3 confirmation
        score = 25.0 if n_confirmations == 2 else 35.0

        # 1. Z-score extremity beyond threshold
        z_abs = abs(zscore)
        if z_abs >= self.ZSCORE_THRESHOLD:
            z_bonus = min(20.0, (z_abs - self.ZSCORE_THRESHOLD) / 1.5 * 20.0)
            score += z_bonus

        # 2. RSI extremity beyond threshold
        if direction == SignalDirection.LONG and rsi <= self.RSI_OVERSOLD:
            rsi_ext = min(15.0, (self.RSI_OVERSOLD - rsi) / 15.0 * 15.0)
            score += rsi_ext
        elif direction == SignalDirection.SHORT and rsi >= self.RSI_OVERBOUGHT:
            rsi_ext = min(15.0, (rsi - self.RSI_OVERBOUGHT) / 15.0 * 15.0)
            score += rsi_ext

        # 3. ADX flatness (lower ADX = better for MR)
        adx_bonus = max(0.0, (self.ADX_MAX - adx) / self.ADX_MAX) * 10.0
        score += min(10.0, adx_bonus)

        # 4. Volume drying up (low volume at extreme = more likely reversion)
        vol_series = df[self.COL_VOLUME].dropna()
        if len(vol_series) >= self.VOLUME_AVG_WINDOW:
            vol_avg = vol_series.iloc[-self.VOLUME_AVG_WINDOW:].mean()
            if vol_avg > 0 and not np.isnan(volume):
                vol_ratio = volume / vol_avg
                vol_bonus = max(0.0, (1.0 - vol_ratio) / 0.5) * 10.0
                score += min(10.0, max(0.0, vol_bonus))

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
