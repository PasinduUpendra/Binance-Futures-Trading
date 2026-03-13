"""
Technical-indicator calculation engine.

Wraps TA-Lib to enrich a pandas OHLCV DataFrame with a full suite of
indicators used by the Claude Quant strategy layer.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd
import talib

logger = logging.getLogger("claude_quant.data.indicator_engine")


class IndicatorEngine:
    """Compute technical indicators on OHLCV DataFrames.

    The input DataFrame **must** contain the columns:
    ``open``, ``high``, ``low``, ``close``, ``volume``
    (case-insensitive -- the engine normalises on entry).

    Every ``calculate_*`` method returns the DataFrame **in-place** with new
    columns appended.  ``calculate_all`` runs the full suite in one call.
    """

    # -- column normalisation -----------------------------------------------

    @staticmethod
    def _normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
        """Ensure required OHLCV columns exist (case-insensitive)."""
        df.columns = [c.lower() for c in df.columns]
        required = {"open", "high", "low", "close", "volume"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"DataFrame missing required OHLCV columns: {missing}")
        return df

    @staticmethod
    def _to_float64(series: pd.Series) -> np.ndarray:  # type: ignore[type-arg]
        """Convert a Series to float64 ndarray (required by TA-Lib)."""
        return series.astype("float64").values

    # -- EMA ----------------------------------------------------------------

    def calculate_ema(
        self, df: pd.DataFrame, periods: list[int] | None = None
    ) -> pd.DataFrame:
        """Add EMA columns (e.g. ``ema_9``, ``ema_21``)."""
        df = self._normalise_columns(df)
        if periods is None:
            periods = [9, 21, 50, 200]

        close = self._to_float64(df["close"])
        for period in periods:
            col_name = f"ema_{period}"
            df[col_name] = talib.EMA(close, timeperiod=period)
            logger.debug("Calculated %s", col_name)
        return df

    # -- RSI ----------------------------------------------------------------

    def calculate_rsi(
        self, df: pd.DataFrame, period: int = 14
    ) -> pd.DataFrame:
        """Add ``rsi`` column."""
        df = self._normalise_columns(df)
        close = self._to_float64(df["close"])
        df["rsi"] = talib.RSI(close, timeperiod=period)
        logger.debug("Calculated RSI(%d)", period)
        return df

    # -- MACD ---------------------------------------------------------------

    def calculate_macd(
        self,
        df: pd.DataFrame,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> pd.DataFrame:
        """Add ``macd``, ``macd_signal``, ``macd_hist`` columns."""
        df = self._normalise_columns(df)
        close = self._to_float64(df["close"])
        macd_line, macd_signal, macd_hist = talib.MACD(
            close, fastperiod=fast, slowperiod=slow, signalperiod=signal
        )
        df["macd"] = macd_line
        df["macd_signal"] = macd_signal
        df["macd_hist"] = macd_hist
        logger.debug("Calculated MACD(%d,%d,%d)", fast, slow, signal)
        return df

    # -- ADX ----------------------------------------------------------------

    def calculate_adx(
        self, df: pd.DataFrame, period: int = 14
    ) -> pd.DataFrame:
        """Add ``adx``, ``plus_di``, ``minus_di`` columns."""
        df = self._normalise_columns(df)
        high = self._to_float64(df["high"])
        low = self._to_float64(df["low"])
        close = self._to_float64(df["close"])

        df["adx"] = talib.ADX(high, low, close, timeperiod=period)
        df["plus_di"] = talib.PLUS_DI(high, low, close, timeperiod=period)
        df["minus_di"] = talib.MINUS_DI(high, low, close, timeperiod=period)
        logger.debug("Calculated ADX(%d)", period)
        return df

    # -- Supertrend ---------------------------------------------------------

    def calculate_supertrend(
        self, df: pd.DataFrame, period: int = 10, multiplier: float = 3.0
    ) -> pd.DataFrame:
        """Add ``supertrend`` and ``supertrend_direction`` columns.

        ``supertrend_direction`` is ``1`` for bullish and ``-1`` for bearish.

        TA-Lib does not include Supertrend natively, so this is implemented
        using ATR from TA-Lib plus the classic Supertrend algorithm.
        """
        df = self._normalise_columns(df)
        high = self._to_float64(df["high"])
        low = self._to_float64(df["low"])
        close = self._to_float64(df["close"])

        atr = talib.ATR(high, low, close, timeperiod=period)

        hl2 = (high + low) / 2.0
        upper_band = hl2 + multiplier * atr
        lower_band = hl2 - multiplier * atr

        n = len(df)
        supertrend = np.empty(n, dtype=np.float64)
        direction = np.ones(n, dtype=np.float64)

        supertrend[0] = upper_band[0] if np.isfinite(upper_band[0]) else 0.0

        for i in range(1, n):
            if np.isnan(atr[i]):
                supertrend[i] = np.nan
                direction[i] = np.nan
                continue

            # Adjust bands based on previous values
            if lower_band[i] > 0 and not np.isnan(lower_band[i - 1]):
                if lower_band[i] < lower_band[i - 1] and close[i - 1] > lower_band[i - 1]:
                    lower_band[i] = lower_band[i - 1]

            if upper_band[i] > 0 and not np.isnan(upper_band[i - 1]):
                if upper_band[i] > upper_band[i - 1] and close[i - 1] < upper_band[i - 1]:
                    upper_band[i] = upper_band[i - 1]

            if direction[i - 1] == 1:  # previous was bullish
                if close[i] < lower_band[i]:
                    direction[i] = -1
                    supertrend[i] = upper_band[i]
                else:
                    direction[i] = 1
                    supertrend[i] = lower_band[i]
            else:  # previous was bearish
                if close[i] > upper_band[i]:
                    direction[i] = 1
                    supertrend[i] = lower_band[i]
                else:
                    direction[i] = -1
                    supertrend[i] = upper_band[i]

        df["supertrend"] = supertrend
        df["supertrend_direction"] = direction
        logger.debug("Calculated Supertrend(%d, %.1f)", period, multiplier)
        return df

    # -- Bollinger Bands ----------------------------------------------------

    def calculate_bollinger_bands(
        self,
        df: pd.DataFrame,
        period: int = 20,
        nbdev: float = 2.0,
    ) -> pd.DataFrame:
        """Add ``bb_upper``, ``bb_middle``, ``bb_lower``, ``bb_width`` columns."""
        df = self._normalise_columns(df)
        close = self._to_float64(df["close"])

        upper, middle, lower = talib.BBANDS(
            close, timeperiod=period, nbdevup=nbdev, nbdevdn=nbdev, matype=0
        )
        df["bb_upper"] = upper
        df["bb_middle"] = middle
        df["bb_lower"] = lower
        # Bandwidth as a ratio – useful for squeeze detection
        with np.errstate(divide="ignore", invalid="ignore"):
            df["bb_width"] = np.where(middle != 0, (upper - lower) / middle, np.nan)
        logger.debug("Calculated Bollinger Bands(%d, %.1f)", period, nbdev)
        return df

    # -- ATR ----------------------------------------------------------------

    def calculate_atr(
        self, df: pd.DataFrame, period: int = 14
    ) -> pd.DataFrame:
        """Add ``atr`` column."""
        df = self._normalise_columns(df)
        high = self._to_float64(df["high"])
        low = self._to_float64(df["low"])
        close = self._to_float64(df["close"])

        df["atr"] = talib.ATR(high, low, close, timeperiod=period)
        logger.debug("Calculated ATR(%d)", period)
        return df

    # -- Volume SMA ---------------------------------------------------------

    def calculate_volume_sma(
        self, df: pd.DataFrame, period: int = 20
    ) -> pd.DataFrame:
        """Add ``volume_sma`` column."""
        df = self._normalise_columns(df)
        volume = self._to_float64(df["volume"])
        df["volume_sma"] = talib.SMA(volume, timeperiod=period)
        logger.debug("Calculated Volume SMA(%d)", period)
        return df

    def calculate_zscore(
        self, df: pd.DataFrame, period: int = 20
    ) -> pd.DataFrame:
        """Add ``zscore`` column (rolling Z-score of close price)."""
        df = self._normalise_columns(df)
        close = df["close"].astype("float64")
        rolling_mean = close.rolling(window=period).mean()
        rolling_std = close.rolling(window=period).std()
        df["zscore"] = np.where(rolling_std != 0, (close - rolling_mean) / rolling_std, 0.0)
        logger.debug("Calculated Z-Score(%d)", period)
        return df

    # -- calculate_all ------------------------------------------------------

    def calculate_all(
        self,
        df: pd.DataFrame,
        *,
        ema_periods: list[int] | None = None,
        rsi_period: int = 14,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        adx_period: int = 14,
        supertrend_period: int = 10,
        supertrend_multiplier: float = 3.0,
        bb_period: int = 20,
        bb_nbdev: float = 2.0,
        atr_period: int = 14,
        volume_sma_period: int = 20,
    ) -> pd.DataFrame:
        """Run **all** indicator calculations and return the enriched DataFrame.

        Parameters mirror each individual ``calculate_*`` method for full
        override capability; defaults match the Claude Quant strategy spec.
        """
        df = self._normalise_columns(df)

        self.calculate_ema(df, periods=ema_periods)
        self.calculate_rsi(df, period=rsi_period)
        self.calculate_macd(df, fast=macd_fast, slow=macd_slow, signal=macd_signal)
        self.calculate_adx(df, period=adx_period)
        self.calculate_supertrend(df, period=supertrend_period, multiplier=supertrend_multiplier)
        self.calculate_bollinger_bands(df, period=bb_period, nbdev=bb_nbdev)
        self.calculate_atr(df, period=atr_period)
        self.calculate_volume_sma(df, period=volume_sma_period)
        self.calculate_zscore(df, period=bb_period)

        logger.info(
            "All indicators calculated. DataFrame shape: %s, columns: %s",
            df.shape,
            list(df.columns),
        )
        return df
