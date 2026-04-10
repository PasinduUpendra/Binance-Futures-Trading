"""
Cross-asset trend consensus module for the Claude Quant trading system.

Based on arXiv:2310.10500 (X-Trend) — simplified for production use.

When most crypto pairs trend in the same direction it confirms a
market-wide regime shift, making individual signals more reliable.
Conversely, when a single pair trends against the majority it may
be diverging for pair-specific reasons (delistings, hacks, etc.)
and the signal should be penalised.

This module does NOT generate signals — it produces a per-pair
confidence *adjustment* that the orchestrator applies after
``AdaptiveStrategy.get_signal_multi_tf()`` returns a signal.

Usage
~~~~~
::

    consensus = CrossAssetConsensus()
    adjustments = consensus.compute(pair_data_4h)
    # adjustments = {"BTC/USDT:USDT": 0.12, "ETH/USDT:USDT": 0.12, ...}
    # Positive = signal aligned with consensus (boost confidence)
    # Negative = signal against consensus (penalise)
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class CrossAssetConsensus:
    """Cross-asset trend consensus for confidence adjustment.

    Algorithm
    ~~~~~~~~~
    For each pair, compute directional alignment using fast/slow EMA:

    1. EMA_fast (span=8) vs EMA_slow (span=21) on close prices.
    2. Direction: +1 if fast > slow, -1 otherwise.
    3. Consensus score: sum(directions) / count(pairs) → range [-1, +1].
    4. Per-pair adjustment = direction * |consensus| * MAX_ADJUSTMENT.
       - If pair agrees with majority: positive (boost)
       - If pair disagrees: negative (penalty)

    Configuration
    ~~~~~~~~~~~~~
    - ``EMA_FAST_SPAN``: Fast EMA span (default 8)
    - ``EMA_SLOW_SPAN``: Slow EMA span (default 21)
    - ``MAX_ADJUSTMENT``: Maximum confidence adjustment points (default 10.0)
    - ``MIN_PAIRS``: Minimum pairs to compute consensus (default 3)
    - ``CONSENSUS_THRESHOLD``: Minimum |consensus| to apply any adjustment (default 0.3)
    """

    EMA_FAST_SPAN: int = 8
    EMA_SLOW_SPAN: int = 21

    # Max confidence adjustment in points (added to or subtracted from signal confidence)
    MAX_ADJUSTMENT: float = 10.0

    # Minimum number of valid pairs to compute consensus
    MIN_PAIRS: int = 3

    # Minimum |consensus| to apply adjustment — avoids noise when market is mixed
    CONSENSUS_THRESHOLD: float = 0.3

    # Column used for EMA calculation
    COL_CLOSE: str = "close"

    def __init__(self) -> None:
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    def _get_direction(self, df: pd.DataFrame) -> Optional[int]:
        """Compute directional signal for a single pair.

        Returns +1 (bullish), -1 (bearish), or 0 (weakening/neutral),
        or None (insufficient data).

        Uses EMA crossover for trend direction with a momentum
        confirmation: if the current close has fallen below the fast EMA
        (uptrend weakening) or risen above it (downtrend weakening),
        the direction is dampened to 0 so consensus reflects the
        deterioration before a full crossover occurs.
        """
        if self.COL_CLOSE not in df.columns:
            return None

        close = df[self.COL_CLOSE].dropna()
        if len(close) < self.EMA_SLOW_SPAN + 5:
            return None

        ema_fast = close.ewm(span=self.EMA_FAST_SPAN, adjust=False).mean().iloc[-1]
        ema_slow = close.ewm(span=self.EMA_SLOW_SPAN, adjust=False).mean().iloc[-1]

        if np.isnan(ema_fast) or np.isnan(ema_slow):
            return None

        current_close = float(close.iloc[-1])
        trend_dir = 1 if ema_fast > ema_slow else -1

        # Momentum confirmation: close vs fast EMA
        if trend_dir == 1 and current_close < float(ema_fast):
            return 0  # Uptrend weakening — price below fast EMA
        if trend_dir == -1 and current_close > float(ema_fast):
            return 0  # Downtrend weakening — price above fast EMA

        return trend_dir

    def compute(
        self,
        pair_data: dict[str, pd.DataFrame],
    ) -> dict[str, float]:
        """Compute per-pair confidence adjustments based on cross-asset consensus.

        Parameters
        ----------
        pair_data : dict[str, DataFrame]
            Mapping from symbol to indicator-enriched DataFrame (typically 4H).

        Returns
        -------
        dict[str, float]
            Per-pair confidence adjustment (positive=boost, negative=penalty).
            Pairs with insufficient data get 0.0 adjustment.
        """
        directions: dict[str, int] = {}

        for sym, df in pair_data.items():
            d = self._get_direction(df)
            if d is not None:
                directions[sym] = d

        # Not enough pairs for meaningful consensus
        if len(directions) < self.MIN_PAIRS:
            self.logger.info(
                "Insufficient pairs for consensus (%d/%d). No adjustment.",
                len(directions),
                self.MIN_PAIRS,
            )
            return {sym: 0.0 for sym in pair_data}

        # Consensus score: [-1, +1]
        consensus_score = sum(directions.values()) / len(directions)

        self.logger.info(
            "Cross-asset consensus: %.2f (%d bullish, %d neutral, %d bearish, %d pairs)",
            consensus_score,
            sum(1 for v in directions.values() if v > 0),
            sum(1 for v in directions.values() if v == 0),
            sum(1 for v in directions.values() if v < 0),
            len(directions),
        )

        # Below threshold — market is mixed, no adjustment
        if abs(consensus_score) < self.CONSENSUS_THRESHOLD:
            self.logger.info(
                "Consensus |%.2f| below threshold %.2f. No adjustment.",
                abs(consensus_score),
                self.CONSENSUS_THRESHOLD,
            )
            return {sym: 0.0 for sym in pair_data}

        # Compute per-pair adjustments
        adjustments: dict[str, float] = {}
        for sym in pair_data:
            if sym in directions:
                # Pair direction * |consensus| * max adjustment
                # If pair aligns with consensus: positive
                # If pair diverges from consensus: negative
                alignment = directions[sym] * np.sign(consensus_score)
                adj = alignment * abs(consensus_score) * self.MAX_ADJUSTMENT
                adjustments[sym] = round(adj, 2)
            else:
                adjustments[sym] = 0.0

        return adjustments
