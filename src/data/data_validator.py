"""
Data validation / anti-hallucination layer.

Every piece of market data flowing through Claude Quant passes through these
checks before it is trusted.  Violations produce structured ``ValidationResult``
objects so callers can decide whether to proceed, retry, or halt.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

logger = logging.getLogger("claude_quant.data.data_validator")


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class ValidationStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"


class ValidationDetail(BaseModel):
    """A single check outcome."""

    check: str
    status: ValidationStatus
    message: str


class ValidationResult(BaseModel):
    """Aggregated outcome of one or more validation checks."""

    passed: bool = True
    status: ValidationStatus = ValidationStatus.PASS
    details: list[ValidationDetail] = Field(default_factory=list)

    def add(self, check: str, status: ValidationStatus, message: str) -> None:
        self.details.append(ValidationDetail(check=check, status=status, message=message))
        if status == ValidationStatus.FAIL:
            self.passed = False
            self.status = ValidationStatus.FAIL
        elif status == ValidationStatus.WARN and self.status != ValidationStatus.FAIL:
            self.status = ValidationStatus.WARN

    @property
    def reasons(self) -> list[str]:
        """Human-readable list of non-pass reasons."""
        return [
            f"[{d.status.value.upper()}] {d.check}: {d.message}"
            for d in self.details
            if d.status != ValidationStatus.PASS
        ]


# ---------------------------------------------------------------------------
# DataValidator
# ---------------------------------------------------------------------------


class DataValidator:
    """Stateless validator with anti-hallucination checks for market data."""

    # -- OHLCV structural checks -------------------------------------------

    @staticmethod
    def validate_ohlcv(df: pd.DataFrame) -> ValidationResult:
        """Validate OHLCV candle integrity.

        Rules
        -----
        * ``low <= open``  and ``low <= close``  for every candle
        * ``high >= open`` and ``high >= close`` for every candle
        * ``low <= high``
        * No NaN in OHLCV columns
        * Volume >= 0
        * DataFrame is not empty
        """
        result = ValidationResult()

        # --- empty check ---
        if df.empty:
            result.add("ohlcv_not_empty", ValidationStatus.FAIL, "DataFrame is empty")
            return result
        result.add("ohlcv_not_empty", ValidationStatus.PASS, f"{len(df)} candles present")

        # --- required columns ---
        cols = {c.lower() for c in df.columns}
        required = {"open", "high", "low", "close", "volume"}
        missing = required - cols
        if missing:
            result.add(
                "ohlcv_columns",
                ValidationStatus.FAIL,
                f"Missing columns: {missing}",
            )
            return result
        result.add("ohlcv_columns", ValidationStatus.PASS, "All OHLCV columns present")

        # Work with lowercase columns
        work = df.copy()
        work.columns = [c.lower() for c in work.columns]

        # Convert to float for comparison (handles Decimal columns)
        for col in ("open", "high", "low", "close", "volume"):
            work[col] = pd.to_numeric(work[col], errors="coerce")

        # --- NaN check ---
        nan_counts: dict[str, int] = {}
        for col in ("open", "high", "low", "close", "volume"):
            n = int(work[col].isna().sum())
            if n > 0:
                nan_counts[col] = n
        if nan_counts:
            result.add(
                "ohlcv_no_nan",
                ValidationStatus.FAIL,
                f"NaN values found: {nan_counts}",
            )
        else:
            result.add("ohlcv_no_nan", ValidationStatus.PASS, "No NaN values")

        # --- low <= high ---
        bad_lh = work[work["low"] > work["high"]]
        if not bad_lh.empty:
            result.add(
                "ohlcv_low_le_high",
                ValidationStatus.FAIL,
                f"{len(bad_lh)} candles with low > high",
            )
        else:
            result.add("ohlcv_low_le_high", ValidationStatus.PASS, "low <= high for all candles")

        # --- low <= open and low <= close ---
        bad_lo = work[(work["low"] > work["open"]) | (work["low"] > work["close"])]
        if not bad_lo.empty:
            result.add(
                "ohlcv_low_le_oc",
                ValidationStatus.FAIL,
                f"{len(bad_lo)} candles with low > open or low > close",
            )
        else:
            result.add(
                "ohlcv_low_le_oc",
                ValidationStatus.PASS,
                "low <= open and low <= close for all candles",
            )

        # --- high >= open and high >= close ---
        bad_ho = work[(work["high"] < work["open"]) | (work["high"] < work["close"])]
        if not bad_ho.empty:
            result.add(
                "ohlcv_high_ge_oc",
                ValidationStatus.FAIL,
                f"{len(bad_ho)} candles with high < open or high < close",
            )
        else:
            result.add(
                "ohlcv_high_ge_oc",
                ValidationStatus.PASS,
                "high >= open and high >= close for all candles",
            )

        # --- volume >= 0 ---
        neg_vol = work[work["volume"] < 0]
        if not neg_vol.empty:
            result.add(
                "ohlcv_volume_positive",
                ValidationStatus.FAIL,
                f"{len(neg_vol)} candles with negative volume",
            )
        else:
            result.add("ohlcv_volume_positive", ValidationStatus.PASS, "All volumes >= 0")

        return result

    # -- Timestamp freshness ------------------------------------------------

    @staticmethod
    def validate_timestamp_freshness(
        df: pd.DataFrame,
        max_age_candles: int = 2,
        timeframe: str = "5m",
    ) -> ValidationResult:
        """Check that the most recent candle is not stale.

        Parameters
        ----------
        df : DataFrame
            Must contain a ``timestamp`` column (datetime-like).
        max_age_candles : int
            Maximum acceptable gap measured in number of *timeframe*-candles.
        timeframe : str
            Candle period string, e.g. ``"1m"``, ``"5m"``, ``"15m"``, ``"1h"``, ``"4h"``, ``"1d"``.
        """
        result = ValidationResult()

        if df.empty:
            result.add("freshness", ValidationStatus.FAIL, "DataFrame is empty")
            return result

        # Resolve column name (case-insensitive)
        ts_col: str | None = None
        for c in df.columns:
            if c.lower() == "timestamp":
                ts_col = c
                break
        if ts_col is None:
            result.add("freshness", ValidationStatus.FAIL, "No 'timestamp' column found")
            return result

        latest_ts = pd.Timestamp(df[ts_col].max())
        if latest_ts.tzinfo is None:
            latest_ts = latest_ts.tz_localize("UTC")

        now = pd.Timestamp(datetime.now(tz=timezone.utc))
        age = now - latest_ts

        # Parse timeframe to timedelta
        tf_seconds = _parse_timeframe_seconds(timeframe)
        max_age_td = pd.Timedelta(seconds=tf_seconds * max_age_candles)

        if age > max_age_td:
            result.add(
                "freshness",
                ValidationStatus.FAIL,
                f"Latest candle is {age} old (max allowed {max_age_td})",
            )
        elif age > max_age_td * 0.75:
            result.add(
                "freshness",
                ValidationStatus.WARN,
                f"Latest candle is {age} old (approaching max {max_age_td})",
            )
        else:
            result.add(
                "freshness",
                ValidationStatus.PASS,
                f"Latest candle is {age} old (within {max_age_td})",
            )

        return result

    # -- Price sanity -------------------------------------------------------

    @staticmethod
    def validate_price_sanity(
        price: Decimal,
        symbol: str,
        daily_high: Decimal,
        daily_low: Decimal,
    ) -> ValidationResult:
        """Verify that *price* falls within the daily high/low for *symbol*.

        The caller must supply ``daily_high`` and ``daily_low`` obtained from
        a trusted ticker source (e.g. ``MarketDataClient.fetch_ticker``).
        """
        result = ValidationResult()

        if price <= 0:
            result.add(
                "price_positive",
                ValidationStatus.FAIL,
                f"Price {price} is non-positive",
            )
            return result
        result.add("price_positive", ValidationStatus.PASS, f"Price {price} > 0")

        if daily_low <= 0 or daily_high <= 0:
            result.add(
                "daily_range_valid",
                ValidationStatus.FAIL,
                f"Invalid daily range: low={daily_low}, high={daily_high}",
            )
            return result

        if daily_low > daily_high:
            result.add(
                "daily_range_valid",
                ValidationStatus.FAIL,
                f"daily_low ({daily_low}) > daily_high ({daily_high})",
            )
            return result
        result.add("daily_range_valid", ValidationStatus.PASS, "Daily range is valid")

        # Allow a small margin (0.5%) outside the daily range for edge ticks
        margin = Decimal("0.005")
        effective_low = daily_low * (1 - margin)
        effective_high = daily_high * (1 + margin)

        if price < effective_low or price > effective_high:
            result.add(
                "price_in_range",
                ValidationStatus.FAIL,
                (
                    f"Price {price} outside daily range "
                    f"[{daily_low} .. {daily_high}] (with 0.5% margin) for {symbol}"
                ),
            )
        else:
            result.add(
                "price_in_range",
                ValidationStatus.PASS,
                f"Price {price} within daily range [{daily_low} .. {daily_high}] for {symbol}",
            )

        return result

    # -- Cross-validation ---------------------------------------------------

    @staticmethod
    def cross_validate_prices(
        price1: Decimal,
        price2: Decimal,
        tolerance: Decimal = Decimal("0.001"),
        label1: str = "source_1",
        label2: str = "source_2",
    ) -> ValidationResult:
        """Check that two independently fetched prices agree within *tolerance*.

        Parameters
        ----------
        tolerance : Decimal
            Maximum relative difference allowed (default 0.001 = 0.1%).
        """
        result = ValidationResult()

        if price1 <= 0 or price2 <= 0:
            result.add(
                "cross_validate_positive",
                ValidationStatus.FAIL,
                f"One or both prices non-positive: {label1}={price1}, {label2}={price2}",
            )
            return result

        avg = (price1 + price2) / 2
        diff = abs(price1 - price2)
        rel_diff = diff / avg

        if rel_diff > tolerance:
            result.add(
                "cross_validate",
                ValidationStatus.FAIL,
                (
                    f"Prices diverge by {rel_diff:.6f} (tolerance {tolerance}): "
                    f"{label1}={price1}, {label2}={price2}"
                ),
            )
        else:
            result.add(
                "cross_validate",
                ValidationStatus.PASS,
                (
                    f"Prices agree within tolerance ({rel_diff:.6f} <= {tolerance}): "
                    f"{label1}={price1}, {label2}={price2}"
                ),
            )

        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TIMEFRAME_MAP: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
    "1w": 604800,
}


def _parse_timeframe_seconds(timeframe: str) -> int:
    """Convert a timeframe string like ``'5m'`` to seconds."""
    tf = timeframe.strip().lower()
    if tf in _TIMEFRAME_MAP:
        return _TIMEFRAME_MAP[tf]

    # Fallback: parse suffix
    unit = tf[-1]
    try:
        value = int(tf[:-1])
    except ValueError as exc:
        raise ValueError(f"Unrecognised timeframe: {timeframe!r}") from exc

    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    if unit not in multipliers:
        raise ValueError(f"Unrecognised timeframe unit '{unit}' in {timeframe!r}")
    return value * multipliers[unit]
