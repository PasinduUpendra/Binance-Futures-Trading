"""
Base strategy abstraction for the Claude Quant trading system.

Provides the abstract interface all concrete strategies must implement,
plus shared Pydantic models for signals and common helper utilities
(ATR-based stops, risk/reward validation).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SignalDirection(str, Enum):
    """Possible trade directions emitted by a strategy."""

    LONG = "long"
    SHORT = "short"
    NONE = "none"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class Signal(BaseModel):
    """
    Immutable value object representing a trading signal produced by a strategy.

    Every signal carries its own risk-management levels (stop-loss / take-profit)
    so the execution layer can place orders without additional computation.
    """

    direction: SignalDirection
    confidence: float = Field(ge=0.0, le=100.0, description="Signal confidence 0-100")
    entry_price: float = Field(gt=0.0, description="Intended entry price")
    stop_loss: float = Field(gt=0.0, description="Stop-loss price")
    take_profit: float = Field(gt=0.0, description="Take-profit price")
    strategy_name: str = Field(min_length=1, description="Name of the originating strategy")
    regime: str = Field(default="unknown", description="Market regime at signal time")
    indicators_used: dict[str, Any] = Field(
        default_factory=dict,
        description="Snapshot of key indicator values that contributed to the signal",
    )
    reasoning: str = Field(
        default="",
        description="Human-readable explanation of why the signal was generated",
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp of signal creation",
    )

    model_config = {"frozen": True}

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("confidence")
    @classmethod
    def _round_confidence(cls, v: float) -> float:
        return round(v, 2)

    @model_validator(mode="after")
    def _validate_sl_tp_direction(self) -> "Signal":
        """Ensure SL/TP are on the correct side of entry for given direction."""
        if self.direction == SignalDirection.NONE:
            return self

        if self.direction == SignalDirection.LONG:
            if self.stop_loss >= self.entry_price:
                raise ValueError(
                    f"Long signal SL ({self.stop_loss}) must be below entry ({self.entry_price})"
                )
            if self.take_profit <= self.entry_price:
                raise ValueError(
                    f"Long signal TP ({self.take_profit}) must be above entry ({self.entry_price})"
                )
        elif self.direction == SignalDirection.SHORT:
            if self.stop_loss <= self.entry_price:
                raise ValueError(
                    f"Short signal SL ({self.stop_loss}) must be above entry ({self.entry_price})"
                )
            if self.take_profit >= self.entry_price:
                raise ValueError(
                    f"Short signal TP ({self.take_profit}) must be below entry ({self.entry_price})"
                )
        return self

    @model_validator(mode="after")
    def _validate_risk_reward(self) -> "Signal":
        """Reject signals whose risk/reward ratio is below the 2.0 minimum."""
        if self.direction == SignalDirection.NONE:
            return self

        rr = calculate_rr_ratio(self.entry_price, self.stop_loss, self.take_profit)
        if rr < 2.0:
            raise ValueError(
                f"Risk/reward ratio {rr:.2f} is below the minimum 2.0 "
                f"(entry={self.entry_price}, sl={self.stop_loss}, tp={self.take_profit})"
            )
        return self


# ---------------------------------------------------------------------------
# Free helper functions (usable without a strategy instance)
# ---------------------------------------------------------------------------


def calculate_rr_ratio(entry: float, sl: float, tp: float) -> float:
    """Return the reward-to-risk ratio.  Risk = |entry - sl|, Reward = |tp - entry|."""
    risk = abs(entry - sl)
    if risk == 0:
        return 0.0
    reward = abs(tp - entry)
    return round(reward / risk, 4)


def calculate_atr_stop(
    df: pd.DataFrame,
    multiplier: float = 1.5,
    atr_column: str = "atr",
) -> float:
    """Return an ATR-based stop distance using the last available ATR value.

    Parameters
    ----------
    df : DataFrame
        Must contain *atr_column*.
    multiplier : float
        How many ATRs away from entry to place the stop.
    atr_column : str
        Column name holding the ATR values.

    Returns
    -------
    float
        Absolute price distance for the stop-loss.
    """
    if atr_column not in df.columns:
        raise KeyError(f"DataFrame missing required ATR column '{atr_column}'")

    atr_value = df[atr_column].dropna().iloc[-1]
    if np.isnan(atr_value) or atr_value <= 0:
        raise ValueError(f"Invalid ATR value: {atr_value}")

    return float(atr_value * multiplier)


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------


class BaseStrategy(ABC):
    """Abstract base class that every concrete strategy must subclass.

    Subclasses implement ``generate_signal`` which receives a DataFrame that
    already has all indicator columns populated by the IndicatorEngine.
    """

    # Subclasses should set a descriptive class-level name.
    name: str = "BaseStrategy"

    def __init__(self) -> None:
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @abstractmethod
    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """Analyse the indicator-enriched *df* and return a ``Signal``.

        If no actionable setup is found the implementation **must** return a
        Signal with ``direction=SignalDirection.NONE``.
        """
        ...

    # ------------------------------------------------------------------
    # Shared helpers available to every strategy
    # ------------------------------------------------------------------

    def _atr_stop_distance(
        self,
        df: pd.DataFrame,
        multiplier: float = 1.5,
        atr_column: str = "atr",
    ) -> float:
        """Convenience wrapper around the module-level ``calculate_atr_stop``."""
        return calculate_atr_stop(df, multiplier=multiplier, atr_column=atr_column)

    def _rr_ratio(self, entry: float, sl: float, tp: float) -> float:
        """Convenience wrapper around the module-level ``calculate_rr_ratio``."""
        return calculate_rr_ratio(entry, sl, tp)

    def _no_signal(self, reason: str = "") -> Signal:
        """Return a neutral (NONE) signal.  Useful for early-exit paths."""
        return Signal(
            direction=SignalDirection.NONE,
            confidence=0.0,
            entry_price=1.0,  # placeholder – validators skip NONE direction
            stop_loss=0.5,
            take_profit=2.0,
            strategy_name=self.name,
            reasoning=reason or "No actionable setup found",
        )

    @staticmethod
    def _safe_last(series: pd.Series) -> float:
        """Return the last non-NaN value of *series*, or NaN if empty."""
        clean = series.dropna()
        if clean.empty:
            return float("nan")
        return float(clean.iloc[-1])

    @staticmethod
    def _safe_prev(series: pd.Series, offset: int = 1) -> float:
        """Return the value *offset* bars before the last non-NaN value."""
        clean = series.dropna()
        if len(clean) <= offset:
            return float("nan")
        return float(clean.iloc[-(offset + 1)])
