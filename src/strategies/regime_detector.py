"""
Market regime detection for the Claude Quant trading system.

Classifies the current market environment into one of four regimes
(TRENDING, RANGING, VOLATILE, QUIET) so the AdaptiveStrategy can
route to the most appropriate concrete strategy.

Indicator columns consumed (produced by IndicatorEngine):
  - adx_14        : Average Directional Index (14-period)
  - bb_upper_20   : Bollinger Band upper (20, 2std)
  - bb_lower_20   : Bollinger Band lower (20, 2std)
  - bb_mid_20     : Bollinger Band middle (20-SMA)
  - atr_14        : Average True Range (14-period)
  - volume        : Raw volume column
  - close         : Close price
"""

from __future__ import annotations

import logging
from enum import Enum

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums & models
# ---------------------------------------------------------------------------


class MarketRegime(str, Enum):
    """Discrete market-regime classifications."""

    TRENDING = "trending"
    RANGING = "ranging"
    VOLATILE = "volatile"
    QUIET = "quiet"


class RegimeState(BaseModel):
    """Snapshot of the detected regime together with the raw metrics that
    produced the classification.  Immutable after creation."""

    regime: MarketRegime
    confidence: float = Field(ge=0.0, le=100.0, description="Classification confidence 0-100")
    adx: float = Field(description="Current ADX reading")
    bb_width_ratio: float = Field(
        description="Current BB width divided by its rolling average"
    )
    atr_ratio: float = Field(
        description="Current ATR divided by its rolling average"
    )
    volume_ratio: float = Field(
        description="Current volume divided by its rolling average"
    )

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


class RegimeDetector:
    """Stateless regime classifier.

    All heavy lifting is done inside ``detect()`` which expects a DataFrame
    whose indicator columns have already been populated by the IndicatorEngine.

    The classification thresholds mirror ``config/regime/regime_params.yaml``.
    """

    # --- Column names (must match IndicatorEngine output) -----------------
    COL_ADX: str = "adx"
    COL_BB_UPPER: str = "bb_upper"
    COL_BB_LOWER: str = "bb_lower"
    COL_BB_MID: str = "bb_middle"
    COL_ATR: str = "atr"
    COL_VOLUME: str = "volume"
    COL_CLOSE: str = "close"

    # --- Lookback windows (in candles) ------------------------------------
    BB_WIDTH_AVG_WINDOW: int = 100
    ATR_AVG_WINDOW: int = 100
    VOLUME_AVG_WINDOW: int = 20

    # --- Thresholds -------------------------------------------------------
    ADX_TRENDING_MIN: float = 25.0
    ADX_RANGING_MAX: float = 20.0
    ADX_VOLATILE_MIN: float = 15.0
    ADX_VOLATILE_MAX: float = 30.0
    ADX_QUIET_MAX: float = 15.0

    BB_WIDTH_VOLATILE_MULT: float = 1.5   # BB width > 1.5x avg -> wide
    BB_WIDTH_NARROW_MULT: float = 0.8     # BB width < 0.8x avg -> narrow
    BB_WIDTH_VERY_NARROW_MULT: float = 0.5  # BB width < 0.5x avg -> very narrow

    ATR_HIGH_MULT: float = 1.2            # ATR > 1.2x avg -> high
    ATR_LOW_MULT: float = 0.8             # ATR < 0.8x avg -> low
    ATR_VERY_LOW_MULT: float = 0.5        # ATR < 0.5x avg -> very low

    VOLUME_LOW_MULT: float = 0.7
    VOLUME_VERY_LOW_MULT: float = 0.5
    VOLUME_SPIKE_MULT: float = 1.5

    def __init__(self) -> None:
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect(self, df: pd.DataFrame) -> RegimeState:
        """Classify the current market regime from an indicator-enriched DataFrame.

        Parameters
        ----------
        df : DataFrame
            Must contain at minimum the columns listed in the class-level
            ``COL_*`` attributes.

        Returns
        -------
        RegimeState
            The detected regime with supporting metrics.

        Raises
        ------
        KeyError
            If any required indicator column is missing.
        ValueError
            If there is insufficient data to compute rolling averages.
        """
        self._validate_columns(df)

        adx = self._last(df, self.COL_ADX)
        bb_width_ratio = self._bb_width_ratio(df)
        atr_ratio = self._atr_ratio(df)
        volume_ratio = self._volume_ratio(df)

        regime, confidence = self._classify(adx, bb_width_ratio, atr_ratio, volume_ratio)

        state = RegimeState(
            regime=regime,
            confidence=round(confidence, 2),
            adx=round(adx, 4),
            bb_width_ratio=round(bb_width_ratio, 4),
            atr_ratio=round(atr_ratio, 4),
            volume_ratio=round(volume_ratio, 4),
        )

        self.logger.info(
            "Regime detected: %s (confidence=%.1f%%, ADX=%.2f, BBw=%.2f, ATRr=%.2f, Vr=%.2f)",
            state.regime.value,
            state.confidence,
            state.adx,
            state.bb_width_ratio,
            state.atr_ratio,
            state.volume_ratio,
        )

        return state

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_columns(self, df: pd.DataFrame) -> None:
        required = [
            self.COL_ADX,
            self.COL_BB_UPPER,
            self.COL_BB_LOWER,
            self.COL_BB_MID,
            self.COL_ATR,
            self.COL_VOLUME,
            self.COL_CLOSE,
        ]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise KeyError(f"DataFrame missing required columns: {missing}")
        if len(df) < max(self.BB_WIDTH_AVG_WINDOW, self.ATR_AVG_WINDOW, self.VOLUME_AVG_WINDOW):
            self.logger.warning(
                "DataFrame length (%d) is shorter than the longest lookback window (%d). "
                "Regime detection accuracy may be reduced.",
                len(df),
                max(self.BB_WIDTH_AVG_WINDOW, self.ATR_AVG_WINDOW, self.VOLUME_AVG_WINDOW),
            )

    @staticmethod
    def _last(df: pd.DataFrame, col: str) -> float:
        series = df[col].dropna()
        if series.empty:
            raise ValueError(f"Column '{col}' contains no non-NaN values")
        return float(series.iloc[-1])

    def _bb_width_ratio(self, df: pd.DataFrame) -> float:
        """Ratio of current BB width to its rolling average."""
        bb_width = df[self.COL_BB_UPPER] - df[self.COL_BB_LOWER]
        # Normalise by middle band to make it price-independent
        mid = df[self.COL_BB_MID].replace(0, np.nan)
        bb_pct_width = bb_width / mid

        current = bb_pct_width.dropna().iloc[-1]
        avg = bb_pct_width.rolling(window=self.BB_WIDTH_AVG_WINDOW, min_periods=20).mean()
        avg_val = avg.dropna().iloc[-1]

        if avg_val == 0:
            return 1.0
        return float(current / avg_val)

    def _atr_ratio(self, df: pd.DataFrame) -> float:
        """Ratio of current ATR to its rolling average."""
        atr = df[self.COL_ATR]
        current = atr.dropna().iloc[-1]
        avg = atr.rolling(window=self.ATR_AVG_WINDOW, min_periods=20).mean()
        avg_val = avg.dropna().iloc[-1]

        if avg_val == 0:
            return 1.0
        return float(current / avg_val)

    def _volume_ratio(self, df: pd.DataFrame) -> float:
        """Ratio of current volume to its rolling average."""
        vol = df[self.COL_VOLUME]
        current = vol.dropna().iloc[-1]
        avg = vol.rolling(window=self.VOLUME_AVG_WINDOW, min_periods=5).mean()
        avg_val = avg.dropna().iloc[-1]

        if avg_val == 0:
            return 1.0
        return float(current / avg_val)

    # ------------------------------------------------------------------

    def _classify(
        self,
        adx: float,
        bb_width_ratio: float,
        atr_ratio: float,
        volume_ratio: float,
    ) -> tuple[MarketRegime, float]:
        """Score each regime and return the best fit with a confidence level."""

        scores: dict[MarketRegime, float] = {
            MarketRegime.TRENDING: self._score_trending(adx, bb_width_ratio, atr_ratio, volume_ratio),
            MarketRegime.RANGING: self._score_ranging(adx, bb_width_ratio, atr_ratio, volume_ratio),
            MarketRegime.VOLATILE: self._score_volatile(adx, bb_width_ratio, atr_ratio, volume_ratio),
            MarketRegime.QUIET: self._score_quiet(adx, bb_width_ratio, atr_ratio, volume_ratio),
        }

        best_regime = max(scores, key=scores.get)  # type: ignore[arg-type]
        best_score = scores[best_regime]

        # Normalise score to 0-100 confidence.  Raw scores are designed to
        # peak around 4.0 (sum of four 0-1 sub-scores).
        confidence = min(100.0, max(0.0, (best_score / 4.0) * 100.0))

        return best_regime, confidence

    # --- Per-regime scoring (each sub-score in 0.0 .. 1.0) ----------------

    def _score_trending(
        self, adx: float, bbr: float, atrr: float, volr: float
    ) -> float:
        score = 0.0
        # ADX > 25 is the primary signal
        if adx >= self.ADX_TRENDING_MIN:
            score += 1.0 + min(0.5, (adx - self.ADX_TRENDING_MIN) / 25.0)
        elif adx >= 20:
            score += 0.4
        # BB width roughly normal (0.8 - 1.5)
        if 0.8 <= bbr <= 1.5:
            score += 0.8
        elif bbr > 1.5:
            score += 0.3  # trending can widen BB a bit
        # ATR roughly normal to slightly elevated
        if 0.8 <= atrr <= 1.4:
            score += 0.8
        elif atrr > 1.4:
            score += 0.3
        # Volume normal or rising
        if volr >= 0.7:
            score += 0.6 + min(0.4, (volr - 0.7) / 2.0)
        return score

    def _score_ranging(
        self, adx: float, bbr: float, atrr: float, volr: float
    ) -> float:
        score = 0.0
        # ADX < 20
        if adx <= self.ADX_RANGING_MAX:
            score += 1.0 + min(0.5, (self.ADX_RANGING_MAX - adx) / 20.0)
        elif adx <= 25:
            score += 0.3
        # Narrow BB
        if bbr <= self.BB_WIDTH_NARROW_MULT:
            score += 1.0
        elif bbr <= 1.0:
            score += 0.5
        # Low ATR
        if atrr <= self.ATR_LOW_MULT:
            score += 1.0
        elif atrr <= 1.0:
            score += 0.4
        # Low volume
        if volr <= self.VOLUME_LOW_MULT:
            score += 0.8
        elif volr <= 1.0:
            score += 0.3
        return score

    def _score_volatile(
        self, adx: float, bbr: float, atrr: float, volr: float
    ) -> float:
        score = 0.0
        # ADX 15 - 30
        if self.ADX_VOLATILE_MIN <= adx <= self.ADX_VOLATILE_MAX:
            score += 0.8
        elif adx > self.ADX_VOLATILE_MAX:
            score += 0.3
        # Wide BB
        if bbr >= self.BB_WIDTH_VOLATILE_MULT:
            score += 1.2 + min(0.3, (bbr - self.BB_WIDTH_VOLATILE_MULT) / 2.0)
        elif bbr >= 1.2:
            score += 0.5
        # High ATR
        if atrr >= self.ATR_HIGH_MULT:
            score += 1.0 + min(0.3, (atrr - self.ATR_HIGH_MULT) / 1.0)
        elif atrr >= 1.0:
            score += 0.4
        # Volume spike
        if volr >= self.VOLUME_SPIKE_MULT:
            score += 1.0
        elif volr >= 1.0:
            score += 0.3
        return score

    def _score_quiet(
        self, adx: float, bbr: float, atrr: float, volr: float
    ) -> float:
        score = 0.0
        # ADX < 15
        if adx <= self.ADX_QUIET_MAX:
            score += 1.2 + min(0.3, (self.ADX_QUIET_MAX - adx) / 15.0)
        elif adx <= 20:
            score += 0.3
        # Very narrow BB
        if bbr <= self.BB_WIDTH_VERY_NARROW_MULT:
            score += 1.2
        elif bbr <= self.BB_WIDTH_NARROW_MULT:
            score += 0.6
        # Very low ATR
        if atrr <= self.ATR_VERY_LOW_MULT:
            score += 1.0
        elif atrr <= self.ATR_LOW_MULT:
            score += 0.5
        # Very low volume
        if volr <= self.VOLUME_VERY_LOW_MULT:
            score += 1.0
        elif volr <= self.VOLUME_LOW_MULT:
            score += 0.4
        return score
