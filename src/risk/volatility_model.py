"""
GARCH(1,1) volatility model for dynamic leverage scaling.

Predicts forward-looking volatility from historical returns.
When predicted volatility spikes above normal, leverage is reduced
to protect the account. When volatility is low, higher leverage
is permitted.

This is the single most impactful risk enhancement for a leveraged
account — it prevents blowups during vol spikes that rule-based
systems miss.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("claude_quant.risk.volatility_model")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum observations needed for GARCH fitting
_MIN_OBSERVATIONS = 100

# Rolling window for fitting (use recent data, not ancient history)
_FIT_WINDOW = 500

# Volatility multiplier thresholds for leverage adjustment
_VOL_VERY_HIGH = Decimal("2.0")   # 2x normal -> cut leverage by 75%
_VOL_HIGH = Decimal("1.5")        # 1.5x normal -> cut leverage by 50%
_VOL_ELEVATED = Decimal("1.2")    # 1.2x normal -> cut leverage by 25%
_VOL_LOW = Decimal("0.7")         # 0.7x normal -> allow slightly more
_VOL_VERY_LOW = Decimal("0.5")    # 0.5x normal -> allow max leverage boost

# Leverage scaling factors
_SCALE_VERY_HIGH = Decimal("0.25")   # 25% of requested leverage
_SCALE_HIGH = Decimal("0.50")        # 50% of requested leverage
_SCALE_ELEVATED = Decimal("0.75")    # 75% of requested leverage
_SCALE_NORMAL = Decimal("1.00")      # 100% (no adjustment)
_SCALE_LOW = Decimal("1.15")         # 115% boost (capped by CB)
_SCALE_VERY_LOW = Decimal("1.25")    # 125% boost (capped by CB)


class VolatilityState:
    """Immutable snapshot of current volatility assessment."""

    __slots__ = (
        "current_vol", "mean_vol", "vol_ratio", "leverage_scale",
        "forecast_horizon", "observations_used",
    )

    def __init__(
        self,
        current_vol: float,
        mean_vol: float,
        vol_ratio: Decimal,
        leverage_scale: Decimal,
        forecast_horizon: int,
        observations_used: int,
    ) -> None:
        self.current_vol = current_vol
        self.mean_vol = mean_vol
        self.vol_ratio = vol_ratio
        self.leverage_scale = leverage_scale
        self.forecast_horizon = forecast_horizon
        self.observations_used = observations_used

    def __repr__(self) -> str:
        return (
            f"VolatilityState(vol_ratio={self.vol_ratio}, "
            f"scale={self.leverage_scale}, "
            f"current={self.current_vol:.6f}, "
            f"mean={self.mean_vol:.6f})"
        )


class VolatilityModel:
    """GARCH(1,1)-based volatility forecaster for leverage adjustment.

    Usage
    -----
    >>> model = VolatilityModel()
    >>> state = model.forecast(df)  # df must have 'close' column
    >>> adjusted_leverage = model.adjust_leverage(requested_leverage=8, state=state)
    """

    def __init__(self, forecast_horizon: int = 1) -> None:
        """
        Parameters
        ----------
        forecast_horizon : int
            Number of periods ahead to forecast volatility (default 1).
        """
        self._forecast_horizon = forecast_horizon

    def forecast(self, df: pd.DataFrame) -> Optional[VolatilityState]:
        """Forecast volatility using GARCH(1,1).

        Parameters
        ----------
        df : pd.DataFrame
            Must have a 'close' column with at least _MIN_OBSERVATIONS rows.

        Returns
        -------
        VolatilityState or None
            Volatility assessment, or None if insufficient data.
        """
        if "close" not in df.columns:
            logger.warning("GARCH: DataFrame missing 'close' column")
            return None

        close = df["close"].dropna().astype(float)
        if len(close) < _MIN_OBSERVATIONS:
            logger.warning(
                "GARCH: Only %d observations, need %d",
                len(close), _MIN_OBSERVATIONS,
            )
            return None

        # Use recent window only
        close = close.tail(_FIT_WINDOW)

        # Calculate log returns (percentage scaled for GARCH stability)
        returns = np.log(close / close.shift(1)).dropna() * 100

        if len(returns) < _MIN_OBSERVATIONS:
            return None

        try:
            from arch import arch_model

            # Fit GARCH(1,1) with Student-t distribution (fat tails for crypto)
            model = arch_model(
                returns,
                vol="Garch",
                p=1,
                q=1,
                dist="t",
                mean="Zero",
                rescale=False,
            )
            result = model.fit(disp="off", show_warning=False)

            # Forecast conditional variance
            forecast = result.forecast(horizon=self._forecast_horizon)
            predicted_variance = forecast.variance.iloc[-1, -1]
            predicted_vol = np.sqrt(predicted_variance) / 100  # Back to decimal

            # Historical mean volatility (rolling std of returns)
            hist_vol = (returns.std() / 100)  # Back to decimal

            if hist_vol <= 0 or np.isnan(predicted_vol) or np.isnan(hist_vol):
                logger.warning("GARCH: Invalid volatility values")
                return None

            vol_ratio = Decimal(str(round(predicted_vol / hist_vol, 4)))
            leverage_scale = self._vol_ratio_to_scale(vol_ratio)

            state = VolatilityState(
                current_vol=float(predicted_vol),
                mean_vol=float(hist_vol),
                vol_ratio=vol_ratio,
                leverage_scale=leverage_scale,
                forecast_horizon=self._forecast_horizon,
                observations_used=len(returns),
            )

            logger.info(
                "GARCH forecast: predicted_vol=%.6f hist_vol=%.6f "
                "ratio=%s scale=%s obs=%d",
                predicted_vol, hist_vol,
                vol_ratio, leverage_scale, len(returns),
            )

            return state

        except Exception as e:
            logger.warning("GARCH fitting failed: %s", e)
            return None

    def forecast_simple(self, df: pd.DataFrame) -> Optional[VolatilityState]:
        """Fallback: EWMA volatility forecast (no GARCH dependency).

        Used when GARCH fitting fails or as a lightweight alternative.
        EWMA with lambda=0.94 (RiskMetrics standard).
        """
        if "close" not in df.columns:
            return None

        close = df["close"].dropna().astype(float)
        if len(close) < _MIN_OBSERVATIONS:
            return None

        close = close.tail(_FIT_WINDOW)
        returns = np.log(close / close.shift(1)).dropna()

        if len(returns) < _MIN_OBSERVATIONS:
            return None

        # EWMA variance (lambda = 0.94, RiskMetrics)
        lam = 0.94
        variance = returns.ewm(alpha=1 - lam, adjust=False).var()
        current_vol = float(np.sqrt(variance.iloc[-1]))
        hist_vol = float(returns.std())

        if hist_vol <= 0 or np.isnan(current_vol):
            return None

        vol_ratio = Decimal(str(round(current_vol / hist_vol, 4)))
        leverage_scale = self._vol_ratio_to_scale(vol_ratio)

        return VolatilityState(
            current_vol=current_vol,
            mean_vol=hist_vol,
            vol_ratio=vol_ratio,
            leverage_scale=leverage_scale,
            forecast_horizon=1,
            observations_used=len(returns),
        )

    @staticmethod
    def _vol_ratio_to_scale(vol_ratio: Decimal) -> Decimal:
        """Convert volatility ratio to leverage scaling factor.

        vol_ratio > 1 means current vol is ABOVE average -> reduce leverage.
        vol_ratio < 1 means current vol is BELOW average -> allow more leverage.
        """
        if vol_ratio >= _VOL_VERY_HIGH:
            return _SCALE_VERY_HIGH
        elif vol_ratio >= _VOL_HIGH:
            return _SCALE_HIGH
        elif vol_ratio >= _VOL_ELEVATED:
            return _SCALE_ELEVATED
        elif vol_ratio >= _VOL_LOW:
            return _SCALE_NORMAL
        elif vol_ratio >= _VOL_VERY_LOW:
            return _SCALE_LOW
        else:
            return _SCALE_VERY_LOW

    @staticmethod
    def adjust_leverage(
        requested_leverage: int,
        vol_state: Optional[VolatilityState],
        max_leverage: int = 10,
    ) -> int:
        """Apply volatility-based scaling to requested leverage.

        Parameters
        ----------
        requested_leverage : int
            Leverage from LeverageManager (confidence x regime).
        vol_state : VolatilityState or None
            Current volatility assessment. If None, returns requested unchanged.
        max_leverage : int
            Hard cap (from circuit breaker).

        Returns
        -------
        int
            Adjusted leverage (always >= 1, capped at max_leverage).
        """
        if vol_state is None:
            return min(requested_leverage, max_leverage)

        scaled = Decimal(str(requested_leverage)) * vol_state.leverage_scale
        adjusted = max(1, min(int(scaled), max_leverage))

        if adjusted != requested_leverage:
            logger.info(
                "GARCH leverage adjustment: %dx -> %dx (vol_ratio=%s, scale=%s)",
                requested_leverage, adjusted,
                vol_state.vol_ratio, vol_state.leverage_scale,
            )

        return adjusted
