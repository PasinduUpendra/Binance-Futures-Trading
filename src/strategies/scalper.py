"""
Scalping strategy for the Claude Quant trading system.

Ultra-short-term strategy that exploits RSI divergence with tight
risk parameters and a maximum hold time of 15 minutes.

Only activates in strong-trend or volatile regimes where short-term
mean-reversion (divergence) plays resolve quickly.

Indicator columns consumed (from IndicatorEngine):
  - rsi_14
  - ema_9, ema_21, ema_50
  - adx_14
  - atr_14
  - volume
  - close, high, low
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

import numpy as np
import pandas as pd

from src.execution.fee_calculator import FeeCalculator
from src.strategies.base_strategy import (
    BaseStrategy,
    Signal,
    SignalDirection,
    calculate_rr_ratio,
)

logger = logging.getLogger(__name__)
_FEE_CALCULATOR = FeeCalculator(use_bnb_discount=False)


class Scalper(BaseStrategy):
    """Scalping strategy based on RSI divergence with trend confirmation.

    Entry conditions
    ~~~~~~~~~~~~~~~~
    - RSI divergence detected on the DataFrame (bullish or bearish)
    - Higher-timeframe trend confirmed via EMA alignment
    - Regime must be TRENDING (ADX > 25) or VOLATILE

    Risk management
    ~~~~~~~~~~~~~~~
    - Profit target: 0.3 - 0.5 % (leveraged)
    - Stop-loss: 0.2 - 0.3 %
    - Maximum hold time: 15 minutes (tracked via entry timestamp;
      the execution layer is responsible for enforcing this)

    Confidence scoring
    ~~~~~~~~~~~~~~~~~~
    Derived from divergence clarity, trend strength (ADX), and
    recent volatility (ATR).
    """

    name: str = "Scalper"

    # -- Column names ------------------------------------------------------
    COL_RSI: str = "rsi"
    COL_EMA9: str = "ema_9"
    COL_EMA21: str = "ema_21"
    COL_EMA50: str = "ema_50"
    COL_ADX: str = "adx"
    COL_ATR: str = "atr"
    COL_VOLUME: str = "volume"
    COL_CLOSE: str = "close"
    COL_HIGH: str = "high"
    COL_LOW: str = "low"

    # -- Tunables ----------------------------------------------------------
    # Profit targets (as fractions of entry price)
    TP_MIN_PCT: float = 0.003    # 0.3 %
    TP_MAX_PCT: float = 0.005    # 0.5 %
    # Stop-loss range (as fractions of entry price)
    SL_MIN_PCT: float = 0.002    # 0.2 %
    SL_MAX_PCT: float = 0.003    # 0.3 %
    # Regime gates
    ADX_TREND_MIN: float = 25.0
    # Divergence detection
    DIV_LOOKBACK: int = 14        # bars to scan for divergence
    DIV_RSI_DELTA_MIN: float = 3.0  # minimum RSI change to count
    # Max hold time (informational — execution layer enforces)
    MAX_HOLD_SECONDS: int = 900   # 15 minutes
    # Volume
    VOLUME_AVG_WINDOW: int = 20

    # ------------------------------------------------------------------
    # Strategy interface
    # ------------------------------------------------------------------

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        try:
            self._validate(df)
        except (KeyError, ValueError) as exc:
            self.logger.warning("Scalper validation failed: %s", exc)
            return self._no_signal(str(exc))

        close = self._safe_last(df[self.COL_CLOSE])
        rsi = self._safe_last(df[self.COL_RSI])
        adx = self._safe_last(df[self.COL_ADX])
        atr = self._safe_last(df[self.COL_ATR])
        ema9 = self._safe_last(df[self.COL_EMA9])
        ema21 = self._safe_last(df[self.COL_EMA21])
        ema50 = self._safe_last(df[self.COL_EMA50])
        volume = self._safe_last(df[self.COL_VOLUME])

        for name, val in [
            ("close", close), ("rsi", rsi), ("adx", adx),
            ("atr", atr), ("ema9", ema9), ("ema21", ema21),
        ]:
            if np.isnan(val):
                return self._no_signal(f"Indicator '{name}' is NaN")

        # --- Regime gate --------------------------------------------------
        # Scalper only operates in strong trend or volatile conditions.
        # Volatile is approximated by ATR being elevated relative to average.
        is_trending = adx >= self.ADX_TREND_MIN
        is_volatile = self._is_volatile(df, atr)

        if not (is_trending or is_volatile):
            return self._no_signal(
                f"Regime not suitable for scalping (ADX={adx:.1f}, volatile={is_volatile})"
            )

        # --- Divergence detection -----------------------------------------
        bullish_div, bullish_clarity = self._detect_bullish_divergence(df)
        bearish_div, bearish_clarity = self._detect_bearish_divergence(df)

        if not bullish_div and not bearish_div:
            return self._no_signal("No RSI divergence detected")

        # --- Higher-timeframe trend confirmation --------------------------
        trend_up = ema9 > ema21
        trend_down = ema9 < ema21

        direction = SignalDirection.NONE
        div_clarity = 0.0

        # Bullish divergence in an uptrend = scalp long
        if bullish_div and trend_up:
            direction = SignalDirection.LONG
            div_clarity = bullish_clarity
        # Bearish divergence in a downtrend = scalp short
        elif bearish_div and trend_down:
            direction = SignalDirection.SHORT
            div_clarity = bearish_clarity
        # Divergence against trend — skip (counter-trend scalps too risky)
        else:
            return self._no_signal(
                "Divergence direction conflicts with higher-TF trend"
            )

        # --- Compute dynamic SL / TP based on volatility ------------------
        # In higher volatility use wider targets within the allowed range
        vol_factor = self._volatility_factor(df, atr)

        sl_pct = self.SL_MIN_PCT + (self.SL_MAX_PCT - self.SL_MIN_PCT) * vol_factor
        tp_pct = self.TP_MIN_PCT + (self.TP_MAX_PCT - self.TP_MIN_PCT) * vol_factor

        entry_price = close

        if direction == SignalDirection.LONG:
            stop_loss = entry_price * (1.0 - sl_pct)
            take_profit = entry_price * (1.0 + tp_pct)
        else:
            stop_loss = entry_price * (1.0 + sl_pct)
            take_profit = entry_price * (1.0 - tp_pct)

        if stop_loss <= 0 or take_profit <= 0:
            return self._no_signal("Computed SL or TP is non-positive")

        rr = calculate_rr_ratio(entry_price, stop_loss, take_profit)
        if rr < 2.0:
            # Widen TP to force 2.0 R/R
            risk = abs(entry_price - stop_loss)
            needed_reward = risk * 2.0
            if direction == SignalDirection.LONG:
                take_profit = entry_price + needed_reward
            else:
                take_profit = entry_price - needed_reward
            rr = calculate_rr_ratio(entry_price, stop_loss, take_profit)
            if rr < 2.0 or take_profit <= 0:
                return self._no_signal(f"Cannot achieve 2.0 R/R for scalp (best: {rr:.2f})")

        take_profit = float(
            _FEE_CALCULATOR.adjust_tp_for_fees(
                entry_price=Decimal(str(entry_price)),
                raw_tp=Decimal(str(take_profit)),
                size=Decimal("1"),
                leverage=1,
                is_long=direction == SignalDirection.LONG,
            )
        )

        # --- Confidence scoring ------------------------------------------
        confidence = self._compute_confidence(
            div_clarity=div_clarity,
            adx=adx,
            atr=atr,
            volume=volume,
            df=df,
            is_trending=is_trending,
        )

        # --- Reasoning ---------------------------------------------------
        reasoning = self._build_reasoning(
            direction, close, rsi, adx, div_clarity,
            is_trending, is_volatile, sl_pct, tp_pct,
            stop_loss, take_profit, rr, confidence,
        )

        indicators_snapshot = {
            "rsi_14": round(rsi, 2),
            "adx_14": round(adx, 2),
            "atr_14": round(atr, 6),
            "ema_9": round(ema9, 6),
            "ema_21": round(ema21, 6),
            "divergence_clarity": round(div_clarity, 2),
            "max_hold_seconds": self.MAX_HOLD_SECONDS,
            "entry_timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "close": round(close, 6),
        }

        return Signal(
            direction=direction,
            confidence=confidence,
            entry_price=round(entry_price, 8),
            stop_loss=round(stop_loss, 8),
            take_profit=round(take_profit, 8),
            strategy_name=self.name,
            regime="trending" if is_trending else "volatile",
            indicators_used=indicators_snapshot,
            reasoning=reasoning,
        )

    # ------------------------------------------------------------------
    # Divergence detection
    # ------------------------------------------------------------------

    def _detect_bullish_divergence(
        self, df: pd.DataFrame
    ) -> tuple[bool, float]:
        """Bullish divergence: price makes lower low, RSI makes higher low.

        Returns (detected: bool, clarity: float 0-100).
        """
        return self._detect_divergence(df, bullish=True)

    def _detect_bearish_divergence(
        self, df: pd.DataFrame
    ) -> tuple[bool, float]:
        """Bearish divergence: price makes higher high, RSI makes lower high.

        Returns (detected: bool, clarity: float 0-100).
        """
        return self._detect_divergence(df, bullish=False)

    def _detect_divergence(
        self, df: pd.DataFrame, *, bullish: bool
    ) -> tuple[bool, float]:
        """Generic divergence detector.

        Scans the last ``DIV_LOOKBACK`` bars for price-vs-RSI divergence.

        For **bullish** divergence we look for two recent swing lows where:
          - price_low_2 < price_low_1  (lower low in price)
          - rsi_low_2 > rsi_low_1     (higher low in RSI)

        For **bearish** divergence we look for two recent swing highs where:
          - price_high_2 > price_high_1  (higher high in price)
          - rsi_high_2 < rsi_high_1      (lower high in RSI)

        Clarity is based on the magnitude of the RSI divergence.
        """
        lookback = df.iloc[-self.DIV_LOOKBACK:]
        rsi_vals = lookback[self.COL_RSI].dropna().values
        if len(rsi_vals) < 5:
            return False, 0.0

        if bullish:
            price_vals = lookback[self.COL_LOW].dropna().values
        else:
            price_vals = lookback[self.COL_HIGH].dropna().values

        if len(price_vals) < 5:
            return False, 0.0

        # Use length of shorter array
        n = min(len(price_vals), len(rsi_vals))
        price_vals = price_vals[-n:]
        rsi_vals = rsi_vals[-n:]

        # Find two most recent local extrema (minima for bullish, maxima for bearish)
        extrema_indices = self._find_local_extrema(
            price_vals, is_min=bullish, order=2
        )

        if len(extrema_indices) < 2:
            return False, 0.0

        # Take the two most recent
        idx1, idx2 = extrema_indices[-2], extrema_indices[-1]

        price1, price2 = price_vals[idx1], price_vals[idx2]
        rsi1, rsi2 = rsi_vals[idx1], rsi_vals[idx2]

        if bullish:
            # Price lower low, RSI higher low
            price_diverges = price2 < price1
            rsi_diverges = rsi2 > rsi1
        else:
            # Price higher high, RSI lower high
            price_diverges = price2 > price1
            rsi_diverges = rsi2 < rsi1

        if not (price_diverges and rsi_diverges):
            return False, 0.0

        rsi_delta = abs(rsi2 - rsi1)
        if rsi_delta < self.DIV_RSI_DELTA_MIN:
            return False, 0.0

        # Clarity: 0-100 based on RSI delta magnitude (3 -> 30, 10 -> 80, 15+ -> 100)
        clarity = min(100.0, max(0.0, (rsi_delta - self.DIV_RSI_DELTA_MIN) / 12.0) * 70.0 + 30.0)
        return True, clarity

    @staticmethod
    def _find_local_extrema(
        arr: np.ndarray, *, is_min: bool, order: int = 2
    ) -> list[int]:
        """Find indices of local minima (is_min=True) or maxima (is_min=False)."""
        indices: list[int] = []
        for i in range(order, len(arr) - order):
            window = arr[i - order : i + order + 1]
            if is_min:
                if arr[i] == window.min():
                    indices.append(i)
            else:
                if arr[i] == window.max():
                    indices.append(i)
        return indices

    # ------------------------------------------------------------------
    # Volatility helpers
    # ------------------------------------------------------------------

    def _is_volatile(self, df: pd.DataFrame, current_atr: float) -> bool:
        """True if current ATR is significantly above average (1.2x)."""
        atr_series = df[self.COL_ATR].dropna()
        if len(atr_series) < 50:
            return False
        avg_atr = atr_series.iloc[-100:].mean() if len(atr_series) >= 100 else atr_series.mean()
        if avg_atr <= 0:
            return False
        return (current_atr / avg_atr) >= 1.2

    def _volatility_factor(self, df: pd.DataFrame, current_atr: float) -> float:
        """Return 0.0 - 1.0 factor representing how elevated current vol is.

        0.0 = low vol (use minimum SL/TP), 1.0 = high vol (use maximum SL/TP).
        """
        atr_series = df[self.COL_ATR].dropna()
        if len(atr_series) < 20:
            return 0.5  # default mid
        avg_atr = atr_series.iloc[-100:].mean() if len(atr_series) >= 100 else atr_series.mean()
        if avg_atr <= 0:
            return 0.5
        ratio = current_atr / avg_atr
        # Map 0.8 - 1.5 to 0.0 - 1.0
        return min(1.0, max(0.0, (ratio - 0.8) / 0.7))

    # ------------------------------------------------------------------
    # Confidence scoring
    # ------------------------------------------------------------------

    def _compute_confidence(
        self,
        *,
        div_clarity: float,
        adx: float,
        atr: float,
        volume: float,
        df: pd.DataFrame,
        is_trending: bool,
    ) -> float:
        """Compute 0-100 confidence.

        Breakdown (max 100):
          Divergence clarity      : up to 35
          Trend strength (ADX)    : up to 25
          Recent volatility (ATR) : up to 20
          Volume confirmation     : up to 20
        """
        score = 0.0

        # 1. Divergence clarity (already 0-100, scale to 35)
        score += (div_clarity / 100.0) * 35.0

        # 2. ADX / trend strength
        if is_trending:
            adx_score = min(25.0, max(0.0, (adx - self.ADX_TREND_MIN) / 25.0) * 25.0)
            score += adx_score
        else:
            # Volatile but not trending — partial credit
            score += 10.0

        # 3. Volatility (moderate ATR is ideal for scalping — too high is risky)
        atr_series = df[self.COL_ATR].dropna()
        if len(atr_series) >= 20:
            avg_atr = atr_series.iloc[-100:].mean() if len(atr_series) >= 100 else atr_series.mean()
            if avg_atr > 0:
                ratio = atr / avg_atr
                # Ideal around 1.0 - 1.5; too high penalised
                if 0.8 <= ratio <= 1.5:
                    score += 20.0
                elif ratio < 0.8:
                    score += max(0.0, ratio / 0.8 * 15.0)
                else:
                    score += max(0.0, 20.0 - (ratio - 1.5) * 10.0)

        # 4. Volume (moderate to high is good for scalping)
        vol_series = df[self.COL_VOLUME].dropna()
        if len(vol_series) >= self.VOLUME_AVG_WINDOW and not np.isnan(volume):
            vol_avg = vol_series.iloc[-self.VOLUME_AVG_WINDOW:].mean()
            if vol_avg > 0:
                vol_ratio = volume / vol_avg
                vol_score = min(20.0, max(0.0, vol_ratio / 2.0) * 20.0)
                score += vol_score

        return min(100.0, max(0.0, round(score, 2)))

    # ------------------------------------------------------------------
    # Validation & formatting
    # ------------------------------------------------------------------

    def _validate(self, df: pd.DataFrame) -> None:
        required = [
            self.COL_RSI, self.COL_EMA9, self.COL_EMA21, self.COL_EMA50,
            self.COL_ADX, self.COL_ATR, self.COL_VOLUME,
            self.COL_CLOSE, self.COL_HIGH, self.COL_LOW,
        ]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise KeyError(f"Missing columns: {missing}")
        if len(df) < self.DIV_LOOKBACK:
            raise ValueError(
                f"Need at least {self.DIV_LOOKBACK} rows for divergence detection"
            )

    def _build_reasoning(
        self,
        direction: SignalDirection,
        close: float,
        rsi: float,
        adx: float,
        div_clarity: float,
        is_trending: bool,
        is_volatile: bool,
        sl_pct: float,
        tp_pct: float,
        sl: float,
        tp: float,
        rr: float,
        confidence: float,
    ) -> str:
        dir_label = direction.value.upper()
        div_type = "bullish" if direction == SignalDirection.LONG else "bearish"
        regime = "trending" if is_trending else "volatile"
        parts = [
            f"{dir_label} scalp signal at {close:.6f}.",
            f"Detected {div_type} RSI divergence (clarity={div_clarity:.0f}%).",
            f"RSI={rsi:.1f}, ADX={adx:.1f} ({regime} regime).",
            f"SL={sl:.6f} ({sl_pct*100:.1f}%), TP={tp:.6f} ({tp_pct*100:.1f}%).",
            f"R/R={rr:.2f}. Max hold: {self.MAX_HOLD_SECONDS}s.",
            f"Confidence: {confidence:.0f}%.",
        ]
        return " ".join(parts)
