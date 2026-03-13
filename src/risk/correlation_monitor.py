"""
Correlation Monitor — prevents concentration risk by rejecting new
positions that would push correlated-asset exposure above 25 %.

Uses a rolling 30-day correlation matrix built from close-price returns.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

logger = logging.getLogger("claude_quant.risk.correlation_monitor")

_CORRELATION_THRESHOLD: Decimal = Decimal("0.7")
_MAX_CORRELATED_EXPOSURE: Decimal = Decimal("0.25")  # 25 % of balance
_ROLLING_WINDOW_DAYS: int = 30


class Position(BaseModel):
    """Minimal representation of an open position."""

    symbol: str
    notional_usd: Decimal = Field(description="Position notional in USD.")


class CorrelationCheckResult(BaseModel):
    """Outcome of a correlation safety check."""

    model_config = {"frozen": True}

    safe_to_add: bool
    correlated_symbols: list[str] = Field(
        default_factory=list,
        description="Existing positions that are correlated with the new symbol.",
    )
    correlated_exposure_usd: Decimal = Field(
        default=Decimal("0"),
        description="Total notional in correlated positions (USD).",
    )
    correlated_exposure_pct: Decimal = Field(
        default=Decimal("0"),
        description="Correlated exposure as a fraction of balance (0-1).",
    )
    reason: str = ""


class CorrelationMonitor:
    """
    Evaluates whether adding a new symbol would breach the correlated-asset
    exposure limit.

    Usage
    -----
    >>> monitor = CorrelationMonitor()
    >>> result = monitor.check_correlation(
    ...     positions=[Position(symbol="BTCUSDT", notional_usd=Decimal("20"))],
    ...     new_symbol="ETHUSDT",
    ...     price_data=price_df,
    ...     balance=Decimal("100"),
    ...     new_notional=Decimal("15"),
    ... )
    >>> result.safe_to_add
    True
    """

    def __init__(
        self,
        correlation_threshold: Decimal | None = None,
        max_correlated_exposure: Decimal | None = None,
        rolling_window_days: int | None = None,
    ) -> None:
        # Allow overrides for backtesting but default to the production values.
        self._threshold = (
            Decimal(str(correlation_threshold))
            if correlation_threshold is not None
            else _CORRELATION_THRESHOLD
        )
        self._max_exposure = (
            Decimal(str(max_correlated_exposure))
            if max_correlated_exposure is not None
            else _MAX_CORRELATED_EXPOSURE
        )
        self._window = rolling_window_days or _ROLLING_WINDOW_DAYS

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def check_correlation(
        self,
        positions: list[Position],
        new_symbol: str,
        price_data: pd.DataFrame,
        balance: Decimal | float | str,
        new_notional: Decimal | float | str = Decimal("0"),
    ) -> CorrelationCheckResult:
        """
        Determine whether it is safe to open a position in *new_symbol*.

        Parameters
        ----------
        positions:
            Currently open positions.
        new_symbol:
            The symbol the strategy wants to open.
        price_data:
            DataFrame with a DatetimeIndex and one column per symbol
            containing **close prices**.  Must include ``new_symbol``
            and every symbol in ``positions``.
        balance:
            Current account balance in USD.
        new_notional:
            The notional value of the proposed new position.

        Returns
        -------
        CorrelationCheckResult
        """
        balance = Decimal(str(balance))
        new_notional = Decimal(str(new_notional))

        if not positions:
            return CorrelationCheckResult(
                safe_to_add=True,
                reason="No existing positions — no correlation risk.",
            )

        # Build the correlation matrix from returns over the rolling window.
        corr_matrix = self._build_correlation_matrix(price_data)

        if corr_matrix is None or corr_matrix.empty:
            logger.warning(
                "Could not build correlation matrix. Allowing trade cautiously."
            )
            return CorrelationCheckResult(
                safe_to_add=True,
                reason="Insufficient price data for correlation check; proceeding cautiously.",
            )

        # Identify which existing positions are correlated with new_symbol.
        correlated_symbols: list[str] = []
        correlated_exposure = Decimal("0")

        for pos in positions:
            corr = self._get_pair_correlation(corr_matrix, pos.symbol, new_symbol)
            if corr is not None and Decimal(str(corr)) > self._threshold:
                correlated_symbols.append(pos.symbol)
                correlated_exposure += pos.notional_usd

        # Include the new position's notional in the correlated bucket.
        if correlated_symbols:
            total_correlated = correlated_exposure + new_notional
        else:
            total_correlated = Decimal("0")

        exposure_pct = (
            (total_correlated / balance).quantize(Decimal("0.0001"))
            if balance > 0
            else Decimal("0")
        )

        safe = exposure_pct <= self._max_exposure

        reason_parts: list[str] = []
        if correlated_symbols:
            reason_parts.append(
                f"Correlated with {', '.join(correlated_symbols)} "
                f"(threshold >{self._threshold})."
            )
            reason_parts.append(
                f"Combined correlated exposure: ${total_correlated} "
                f"({exposure_pct:.2%} of ${balance})."
            )
            if not safe:
                reason_parts.append(
                    f"BLOCKED: would exceed {self._max_exposure:.0%} correlated limit."
                )
        else:
            reason_parts.append("No correlated positions found.")

        result = CorrelationCheckResult(
            safe_to_add=safe,
            correlated_symbols=correlated_symbols,
            correlated_exposure_usd=total_correlated,
            correlated_exposure_pct=exposure_pct,
            reason=" ".join(reason_parts),
        )

        if not safe:
            logger.warning(
                "Correlation check FAILED for %s: %s",
                new_symbol,
                result.reason,
            )
        else:
            logger.info("Correlation check passed for %s.", new_symbol)

        return result

    # ------------------------------------------------------------------
    # Correlation matrix
    # ------------------------------------------------------------------
    def _build_correlation_matrix(
        self, price_data: pd.DataFrame
    ) -> pd.DataFrame | None:
        """
        Build a Pearson correlation matrix from the trailing
        ``self._window`` days of log returns.
        """
        if price_data.empty or len(price_data.columns) < 2:
            return None

        # Use the last N trading days.
        tail = price_data.tail(self._window)
        if len(tail) < 5:
            logger.warning(
                "Only %d rows available for correlation (need >= 5). "
                "Skipping correlation check.",
                len(tail),
            )
            return None

        # Log returns to normalise across different price scales.
        returns = np.log(tail / tail.shift(1)).dropna()
        if returns.empty:
            return None

        corr = returns.corr(method="pearson")
        return corr

    @staticmethod
    def _get_pair_correlation(
        corr_matrix: pd.DataFrame,
        symbol_a: str,
        symbol_b: str,
    ) -> float | None:
        """Safely retrieve a single correlation coefficient."""
        if symbol_a not in corr_matrix.columns or symbol_b not in corr_matrix.columns:
            logger.debug(
                "Symbol(s) missing from correlation matrix: %s, %s",
                symbol_a,
                symbol_b,
            )
            return None
        val = corr_matrix.loc[symbol_a, symbol_b]
        if pd.isna(val):
            return None
        return float(val)
