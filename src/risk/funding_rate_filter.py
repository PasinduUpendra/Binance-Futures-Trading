"""
Funding rate filter for Binance Futures trades.

Prevents entering trades against extreme funding rates, which silently
erode P&L.  Extreme positive funding (>0.05%) signals crowded longs;
extreme negative funding (<-0.05%) signals crowded shorts.

References:
  - CLAUDE.md §3: Funding rate = 0.01% per 8h = ~0.03%/day = ~1%/month drag
  - CLAUDE.md §5: Futures Sentiment Data (proxy feeds)
  - Sprint 1.2 in docs/reports/2026-04-05-arxiv-research-synthesis.md
"""

from __future__ import annotations

import logging
from decimal import Decimal

from pydantic import BaseModel, Field

logger = logging.getLogger("claude_quant.risk.funding_rate_filter")


class FundingRateResult(BaseModel):
    """Result of funding rate filter evaluation."""

    should_trade: bool = Field(description="Whether to proceed with the trade")
    confidence_adjustment: float = Field(
        default=0.0,
        description="Adjustment to signal confidence (positive = bonus, negative = penalty)",
    )
    funding_rate: float = Field(description="The funding rate evaluated")
    reason: str = Field(default="", description="Human-readable explanation")

    model_config = {"frozen": True}


class FundingRateFilter:
    """Stateless filter that evaluates funding rate against trade direction.

    Thresholds (from research synthesis):
      - |rate| > 0.05% (0.0005): Extreme — reject trades INTO the crowd
      - |rate| > 0.03% (0.0003): Elevated — bonus for contrarian trades
      - |rate| <= 0.03%: Neutral — no adjustment
    """

    # Extreme thresholds: reject trades that pay funding at these levels
    EXTREME_RATE: float = 0.0005  # 0.05% per 8h

    # Elevated thresholds: apply confidence bonus for contrarian direction
    ELEVATED_RATE: float = 0.0003  # 0.03% per 8h

    # Confidence adjustments
    REJECT_ADJUSTMENT: float = -20.0
    CONTRARIAN_BONUS: float = 10.0

    @classmethod
    def evaluate(
        cls,
        funding_rate: float,
        signal_direction: str,
    ) -> FundingRateResult:
        """Evaluate whether a trade should proceed given the funding rate.

        Parameters
        ----------
        funding_rate : float
            Current funding rate.  Positive = longs pay shorts.
            Negative = shorts pay longs.
        signal_direction : str
            Trade direction: ``"long"`` or ``"short"``.

        Returns
        -------
        FundingRateResult
            Contains should_trade, confidence_adjustment, and reason.
        """
        direction = signal_direction.lower()
        rate = float(funding_rate)

        # Extreme positive funding: crowded long → reject longs
        if direction == "long" and rate > cls.EXTREME_RATE:
            logger.warning(
                "FUNDING_REJECT: rate=%.6f > %.4f%%, rejecting LONG (crowded)",
                rate,
                cls.EXTREME_RATE * 100,
            )
            return FundingRateResult(
                should_trade=False,
                confidence_adjustment=cls.REJECT_ADJUSTMENT,
                funding_rate=rate,
                reason=f"Extreme positive funding {rate*100:.4f}% — crowded long, reject",
            )

        # Extreme negative funding: crowded short → reject shorts
        if direction == "short" and rate < -cls.EXTREME_RATE:
            logger.warning(
                "FUNDING_REJECT: rate=%.6f < -%.4f%%, rejecting SHORT (crowded)",
                rate,
                cls.EXTREME_RATE * 100,
            )
            return FundingRateResult(
                should_trade=False,
                confidence_adjustment=cls.REJECT_ADJUSTMENT,
                funding_rate=rate,
                reason=f"Extreme negative funding {rate*100:.4f}% — crowded short, reject",
            )

        # Elevated negative funding: shorts paying longs → bonus for longs
        if direction == "long" and rate < -cls.ELEVATED_RATE:
            logger.info(
                "FUNDING_BONUS: rate=%.6f, shorts paying longs → LONG bonus +%d",
                rate,
                cls.CONTRARIAN_BONUS,
            )
            return FundingRateResult(
                should_trade=True,
                confidence_adjustment=cls.CONTRARIAN_BONUS,
                funding_rate=rate,
                reason=f"Negative funding {rate*100:.4f}% — shorts paying longs, bonus",
            )

        # Elevated positive funding: longs paying shorts → bonus for shorts
        if direction == "short" and rate > cls.ELEVATED_RATE:
            logger.info(
                "FUNDING_BONUS: rate=%.6f, longs paying shorts → SHORT bonus +%d",
                rate,
                cls.CONTRARIAN_BONUS,
            )
            return FundingRateResult(
                should_trade=True,
                confidence_adjustment=cls.CONTRARIAN_BONUS,
                funding_rate=rate,
                reason=f"Positive funding {rate*100:.4f}% — longs paying shorts, bonus",
            )

        # Neutral: no adjustment
        return FundingRateResult(
            should_trade=True,
            confidence_adjustment=0.0,
            funding_rate=rate,
            reason=f"Neutral funding {rate*100:.4f}% — no adjustment",
        )
