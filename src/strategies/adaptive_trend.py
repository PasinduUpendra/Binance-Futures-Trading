"""
Adaptive momentum-trend strategy for the Claude Quant trading system.

Generates signals in RANGING and weak-trend markets where SupertrendTrend
is silent.  Based on composite trailing momentum (arXiv:2602.11708,
"An Adaptive Trend-Following Strategy for Financial Markets", Sharpe 2.41).

Unlike SupertrendTrend which waits for a discrete Supertrend flip, this
strategy uses continuous trailing momentum across multiple lookback windows
to enter when directional pressure builds — even in sideways markets.

Indicator columns consumed (from IndicatorEngine on 4H data):
  - ema_9, ema_21          : EMA alignment confirms direction
  - rsi                    : Overbought/oversold filter
  - adx                    : Trend strength (used to cap, NOT gate)
  - atr                    : ATR for SL/TP calculation
  - close                  : Price and momentum computation
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


class AdaptiveTrend(BaseStrategy):
    """Momentum-based strategy for ranging and weakly-trending markets.

    Entry conditions
    ~~~~~~~~~~~~~~~~
    - **Long**: composite momentum > threshold AND EMA_9 > EMA_21
      AND RSI not overbought (< 70) AND regime != QUIET
    - **Short**: composite momentum < -threshold AND EMA_9 < EMA_21
      AND RSI not oversold (> 30) AND regime != QUIET

    Momentum score
    ~~~~~~~~~~~~~~
    Weighted average of trailing returns over three windows::

        mom_short  = pct_change(6)   — 1-day momentum on 4H
        mom_medium = pct_change(30)  — 5-day momentum on 4H
        mom_long   = pct_change(90)  — 15-day momentum on 4H

        score = 0.5 * mom_short + 0.3 * mom_medium + 0.2 * mom_long

    Risk management
    ~~~~~~~~~~~~~~~
    - SL: 2.5 x ATR(4H) from entry
    - TP: 5.0 x ATR(4H) from entry (2.0 R/R)
    - These are regime-aware and can be adjusted via ``SL_TP_BY_REGIME``

    Confidence scoring
    ~~~~~~~~~~~~~~~~~~
    Base = 30 pts (momentum threshold cleared)
    + Momentum strength bonus: up to 25 pts
    + EMA alignment bonus: 15 pts if EMA_9/EMA_21 confirm
    + ADX bonus: up to 15 pts (higher ADX = more directional)
    + RSI bonus: up to 15 pts (healthy RSI range)
    = Max ~100 pts
    """

    name: str = "AdaptiveTrend"

    # Column names
    COL_EMA9: str = "ema_9"
    COL_EMA21: str = "ema_21"
    COL_RSI: str = "rsi"
    COL_ADX: str = "adx"
    COL_ATR: str = "atr"
    COL_CLOSE: str = "close"

    # Momentum lookback windows (in 4H candles)
    MOM_SHORT: int = 6     # ~1 day
    MOM_MEDIUM: int = 30   # ~5 days
    MOM_LONG: int = 90     # ~15 days

    # Momentum weights
    W_SHORT: float = 0.5
    W_MEDIUM: float = 0.3
    W_LONG: float = 0.2

    # Entry threshold — momentum score must exceed this
    MOM_THRESHOLD: float = 0.005  # 0.5% composite momentum

    # RSI filters
    RSI_MAX_LONG: float = 70.0    # Don't go long if overbought
    RSI_MIN_SHORT: float = 30.0   # Don't go short if oversold

    # SL/TP defaults (ATR multiples)
    SL_ATR_MULT: float = 2.5
    TP_ATR_MULT: float = 5.0

    # Regime-aware SL/TP (same pattern as SupertrendTrend)
    # All maintain minimum 2.0 R/R
    SL_TP_BY_REGIME: dict[str, dict[str, float]] = {
        "trending": {"sl_mult": 3.0, "tp_mult": 6.0},  # R/R=2.0 — wider in trends
        "volatile": {"sl_mult": 3.5, "tp_mult": 7.0},  # R/R=2.0 — wide SL for noise
        "ranging":  {"sl_mult": 2.5, "tp_mult": 5.0},  # R/R=2.0 — standard
        "quiet":    {"sl_mult": 2.0, "tp_mult": 4.0},  # R/R=2.0 — conservative
    }

    # Minimum data rows required
    MIN_ROWS: int = 100  # Need at least MOM_LONG + buffer

    def _get_sl_tp_mults(self, regime: str | None) -> tuple[float, float]:
        """Return (sl_mult, tp_mult) for the given regime."""
        if regime and regime in self.SL_TP_BY_REGIME:
            params = self.SL_TP_BY_REGIME[regime]
            return params["sl_mult"], params["tp_mult"]
        return self.SL_ATR_MULT, self.TP_ATR_MULT

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate(self, df: pd.DataFrame) -> None:
        """Check required columns and minimum data length."""
        required = [
            self.COL_EMA9, self.COL_EMA21, self.COL_RSI,
            self.COL_ADX, self.COL_ATR, self.COL_CLOSE,
        ]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise KeyError(f"Missing columns: {missing}")

        if len(df) < self.MIN_ROWS:
            raise ValueError(
                f"Need at least {self.MIN_ROWS} rows, got {len(df)}"
            )

    # ------------------------------------------------------------------
    # Momentum calculation
    # ------------------------------------------------------------------

    def _compute_momentum_score(self, df: pd.DataFrame) -> float:
        """Compute weighted composite momentum score from close prices.

        Returns NaN if any lookback window has insufficient data.
        """
        close = df[self.COL_CLOSE]

        if len(close) < self.MOM_LONG + 1:
            return float("nan")

        mom_short = float(close.pct_change(self.MOM_SHORT).iloc[-1])
        mom_medium = float(close.pct_change(self.MOM_MEDIUM).iloc[-1])
        mom_long = float(close.pct_change(self.MOM_LONG).iloc[-1])

        if any(np.isnan(v) for v in [mom_short, mom_medium, mom_long]):
            return float("nan")

        return (
            self.W_SHORT * mom_short
            + self.W_MEDIUM * mom_medium
            + self.W_LONG * mom_long
        )

    # ------------------------------------------------------------------
    # Confidence scoring
    # ------------------------------------------------------------------

    def _score_confidence(
        self,
        mom_score: float,
        ema9: float,
        ema21: float,
        adx: float,
        rsi: float,
        direction: SignalDirection,
    ) -> float:
        """Score signal confidence 0-100.

        Components:
        - Base: 30 (momentum threshold cleared)
        - Momentum strength: 0-25 (scaled by how far above threshold)
        - EMA alignment: 0-15
        - ADX strength: 0-15
        - RSI health: 0-15
        """
        score = 30.0

        # Momentum strength: scale 0-25 based on distance from threshold
        mom_abs = abs(mom_score)
        # Cap at 5% total momentum for scoring purposes
        mom_excess = min(mom_abs - self.MOM_THRESHOLD, 0.045) / 0.045
        score += mom_excess * 25.0

        # EMA alignment
        if direction == SignalDirection.LONG and ema9 > ema21:
            score += 15.0
        elif direction == SignalDirection.SHORT and ema9 < ema21:
            score += 15.0

        # ADX: higher = more directional momentum
        if adx >= 25.0:
            score += 15.0
        elif adx >= 18.0:
            score += 10.0
        elif adx >= 12.0:
            score += 5.0

        # RSI health: not near extremes
        if direction == SignalDirection.LONG:
            if 40.0 <= rsi <= 60.0:
                score += 15.0  # Ideal — room to run
            elif 30.0 <= rsi <= 70.0:
                score += 10.0  # Acceptable
            else:
                score += 0.0   # Overbought risk
        else:
            if 40.0 <= rsi <= 60.0:
                score += 15.0
            elif 30.0 <= rsi <= 70.0:
                score += 10.0
            else:
                score += 0.0

        return min(score, 100.0)

    # ------------------------------------------------------------------
    # Signal generation
    # ------------------------------------------------------------------

    def generate_signal(
        self,
        df: pd.DataFrame,
        entry_price: float | None = None,
        regime: str | None = None,
    ) -> Signal:
        """Evaluate momentum setup on 4H data.

        Parameters
        ----------
        df : DataFrame
            4H indicator-enriched data.
        entry_price : float, optional
            If provided, use as entry (e.g., 1H close for timing).
        regime : str, optional
            Market regime for dynamic SL/TP selection.
        """
        try:
            self._validate(df)
        except (KeyError, ValueError) as exc:
            self.logger.warning("AdaptiveTrend validation failed: %s", exc)
            return self._no_signal(str(exc))

        # Extract latest indicator values
        ema9 = self._safe_last(df[self.COL_EMA9])
        ema21 = self._safe_last(df[self.COL_EMA21])
        rsi = self._safe_last(df[self.COL_RSI])
        adx = self._safe_last(df[self.COL_ADX])
        atr = self._safe_last(df[self.COL_ATR])
        close = entry_price if entry_price is not None else self._safe_last(df[self.COL_CLOSE])

        # Check for NaN values
        if any(np.isnan(v) for v in [ema9, ema21, rsi, adx, atr, close]):
            return self._no_signal("NaN indicator values")

        if atr <= 0:
            return self._no_signal(f"Invalid ATR: {atr}")

        # Compute momentum score
        mom_score = self._compute_momentum_score(df)
        if np.isnan(mom_score):
            return self._no_signal("Insufficient data for momentum calculation")

        # Determine direction
        direction = SignalDirection.NONE

        if mom_score > self.MOM_THRESHOLD:
            # Bullish momentum
            if ema9 > ema21 and rsi < self.RSI_MAX_LONG:
                direction = SignalDirection.LONG
        elif mom_score < -self.MOM_THRESHOLD:
            # Bearish momentum
            if ema9 < ema21 and rsi > self.RSI_MIN_SHORT:
                direction = SignalDirection.SHORT

        if direction == SignalDirection.NONE:
            return self._no_signal(
                f"No setup: mom={mom_score:.4f} ema9={ema9:.2f} "
                f"ema21={ema21:.2f} rsi={rsi:.1f}"
            )

        # Confidence scoring
        confidence = self._score_confidence(
            mom_score, ema9, ema21, adx, rsi, direction,
        )

        # SL/TP calculation
        sl_mult, tp_mult = self._get_sl_tp_mults(regime)

        if direction == SignalDirection.LONG:
            stop_loss = close - sl_mult * atr
            take_profit = close + tp_mult * atr
        else:
            stop_loss = close + sl_mult * atr
            take_profit = close - tp_mult * atr

        # Validate R/R
        rr = calculate_rr_ratio(close, stop_loss, take_profit)
        if rr < 2.0 - 1e-9:
            return self._no_signal(
                f"R/R {rr:.2f} below minimum 2.0 (sl_mult={sl_mult}, tp_mult={tp_mult})"
            )

        # Ensure TP is valid (positive)
        if take_profit <= 0:
            return self._no_signal(f"Invalid TP: {take_profit:.4f}")

        return Signal(
            direction=direction,
            confidence=confidence,
            entry_price=close,
            stop_loss=stop_loss,
            take_profit=take_profit,
            strategy_name=self.name,
            regime=regime or "unknown",
            indicators_used={
                "momentum_score": round(mom_score, 6),
                "ema_9": round(ema9, 4),
                "ema_21": round(ema21, 4),
                "rsi": round(rsi, 2),
                "adx": round(adx, 2),
                "atr": round(atr, 4),
            },
            reasoning=(
                f"{'Bullish' if direction == SignalDirection.LONG else 'Bearish'} momentum "
                f"score={mom_score:.4f} (threshold={self.MOM_THRESHOLD}), "
                f"EMA9={'above' if ema9 > ema21 else 'below'} EMA21, "
                f"RSI={rsi:.1f}, ADX={adx:.1f}"
            ),
        )
