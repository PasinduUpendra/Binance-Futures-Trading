"""
Bias detector for Claude Quant trading system.

Analyzes trade history to detect common psychological trading biases
such as revenge trading, overtrading, anchoring, and recency bias.
Requires a minimum of 20 trades for meaningful analysis.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from src.memory.trade_journal import TradeEntry

logger = logging.getLogger("claude_quant.memory.bias_detector")

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

MIN_TRADES_FOR_ANALYSIS = 20
REVENGE_TRADE_WINDOW_SECONDS = 1800  # 30 minutes
REVENGE_SIZE_INCREASE_THRESHOLD = Decimal("1.3")  # 30% size increase after loss
OVERTRADING_WINDOW_HOURS = 4
OVERTRADING_MAX_TRADES = 8  # Max reasonable trades in the window
ANCHORING_DURATION_RATIO = 2.0  # losers held 2x+ longer than winners
RECENCY_WINDOW_TRADES = 5  # last N trades for recency analysis
RECENCY_WEIGHT_THRESHOLD = 0.7  # if recent win rate differs > 70% from overall


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class BiasInstance(BaseModel):
    """A single detected instance of a trading bias."""

    bias_type: str
    timestamp: datetime
    description: str
    trade_ids: list[str] = Field(default_factory=list)
    severity: str = Field(
        default="moderate",
        description="'low', 'moderate', or 'high'",
    )
    metrics: dict[str, Any] = Field(default_factory=dict)


class BiasReport(BaseModel):
    """Complete bias analysis report."""

    total_trades_analyzed: int = 0
    analysis_period_start: datetime | None = None
    analysis_period_end: datetime | None = None
    sufficient_data: bool = False

    # Per-bias summaries
    revenge_trading: list[BiasInstance] = Field(default_factory=list)
    overtrading: list[BiasInstance] = Field(default_factory=list)
    anchoring: list[BiasInstance] = Field(default_factory=list)
    recency_bias: list[BiasInstance] = Field(default_factory=list)

    # Aggregate scores (0.0 = no bias, 1.0 = severe)
    revenge_score: float = 0.0
    overtrading_score: float = 0.0
    anchoring_score: float = 0.0
    recency_score: float = 0.0
    overall_bias_score: float = 0.0

    summary: str = ""
    recommendations: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# BiasDetector
# ---------------------------------------------------------------------------


class BiasDetector:
    """Detects common psychological trading biases from trade history.

    Analyzes a list of ``TradeEntry`` records to identify:

    - **Revenge trading**: Increased position sizes immediately after losses
    - **Overtrading**: Too many trades in a short time period
    - **Anchoring**: Holding losing trades much longer than winners
    - **Recency bias**: Over-weighting recent results in decision making

    Requires a minimum of 20 trades for reliable analysis.
    """

    def analyze(self, trades: list[TradeEntry]) -> BiasReport:
        """Run full bias analysis on a list of trades.

        Parameters
        ----------
        trades : list[TradeEntry]
            Trade history to analyze. Should be ordered by timestamp
            (any order is accepted; will be sorted internally).

        Returns
        -------
        BiasReport
            Complete bias analysis with instances, scores, and recommendations.
        """
        if len(trades) < MIN_TRADES_FOR_ANALYSIS:
            logger.info(
                "BIAS_ANALYSIS insufficient data: %d trades (minimum %d)",
                len(trades),
                MIN_TRADES_FOR_ANALYSIS,
            )
            return BiasReport(
                total_trades_analyzed=len(trades),
                sufficient_data=False,
                summary=(
                    f"Insufficient data for bias analysis. "
                    f"Need at least {MIN_TRADES_FOR_ANALYSIS} trades, "
                    f"have {len(trades)}."
                ),
                recommendations=[
                    f"Continue trading to accumulate at least "
                    f"{MIN_TRADES_FOR_ANALYSIS} trades for bias analysis."
                ],
            )

        # Sort by timestamp ascending
        sorted_trades = sorted(trades, key=lambda t: t.timestamp)

        # Run individual bias detectors
        revenge_instances = self._detect_revenge_trading(sorted_trades)
        overtrading_instances = self._detect_overtrading(sorted_trades)
        anchoring_instances = self._detect_anchoring(sorted_trades)
        recency_instances = self._detect_recency_bias(sorted_trades)

        # Calculate scores
        revenge_score = self._score_revenge(revenge_instances, len(sorted_trades))
        overtrading_score = self._score_overtrading(overtrading_instances, len(sorted_trades))
        anchoring_score = self._score_anchoring(anchoring_instances)
        recency_score = self._score_recency(recency_instances)

        overall = (revenge_score + overtrading_score + anchoring_score + recency_score) / 4.0

        # Build recommendations
        recommendations = self._build_recommendations(
            revenge_score, overtrading_score, anchoring_score, recency_score
        )

        # Build summary
        summary = self._build_summary(
            revenge_score, overtrading_score, anchoring_score, recency_score, overall
        )

        report = BiasReport(
            total_trades_analyzed=len(sorted_trades),
            analysis_period_start=sorted_trades[0].timestamp,
            analysis_period_end=sorted_trades[-1].timestamp,
            sufficient_data=True,
            revenge_trading=revenge_instances,
            overtrading=overtrading_instances,
            anchoring=anchoring_instances,
            recency_bias=recency_instances,
            revenge_score=round(revenge_score, 4),
            overtrading_score=round(overtrading_score, 4),
            anchoring_score=round(anchoring_score, 4),
            recency_score=round(recency_score, 4),
            overall_bias_score=round(overall, 4),
            summary=summary,
            recommendations=recommendations,
        )

        logger.info(
            "BIAS_ANALYSIS trades=%d revenge=%.2f overtrading=%.2f "
            "anchoring=%.2f recency=%.2f overall=%.2f",
            len(sorted_trades),
            revenge_score,
            overtrading_score,
            anchoring_score,
            recency_score,
            overall,
        )

        return report

    # -- revenge trading detection -------------------------------------------

    def _detect_revenge_trading(
        self, trades: list[TradeEntry]
    ) -> list[BiasInstance]:
        """Detect revenge trading: increased size after losses.

        Looks for patterns where a trader increases position size within
        a short window after a losing trade.
        """
        instances: list[BiasInstance] = []
        trades_with_pnl = [t for t in trades if t.pnl is not None]

        for i in range(1, len(trades_with_pnl)):
            prev = trades_with_pnl[i - 1]
            curr = trades_with_pnl[i]

            if prev.pnl is None or prev.pnl >= 0:
                continue

            # Check time window
            time_diff = (curr.timestamp - prev.timestamp).total_seconds()
            if time_diff > REVENGE_TRADE_WINDOW_SECONDS:
                continue

            # Check if size increased significantly
            if prev.size > 0 and curr.size > prev.size * REVENGE_SIZE_INCREASE_THRESHOLD:
                size_increase_pct = float(
                    (curr.size - prev.size) / prev.size * 100
                )
                severity = "high" if size_increase_pct > 50 else "moderate"

                instances.append(
                    BiasInstance(
                        bias_type="revenge_trading",
                        timestamp=curr.timestamp,
                        description=(
                            f"Position size increased {size_increase_pct:.1f}% "
                            f"within {time_diff:.0f}s after a loss of {prev.pnl}"
                        ),
                        trade_ids=[prev.trade_id, curr.trade_id],
                        severity=severity,
                        metrics={
                            "prev_size": str(prev.size),
                            "curr_size": str(curr.size),
                            "size_increase_pct": round(size_increase_pct, 2),
                            "time_after_loss_seconds": round(time_diff, 0),
                            "loss_amount": str(prev.pnl),
                        },
                    )
                )

        # Also check for consecutive size increases after multiple losses
        loss_streak_start: int | None = None
        for i, t in enumerate(trades_with_pnl):
            if t.pnl is not None and t.pnl < 0:
                if loss_streak_start is None:
                    loss_streak_start = i
            else:
                if loss_streak_start is not None:
                    streak_len = i - loss_streak_start
                    if streak_len >= 3:
                        # Check if sizes increased during the streak
                        streak_trades = trades_with_pnl[loss_streak_start:i]
                        sizes = [t.size for t in streak_trades]
                        if len(sizes) >= 3 and all(
                            sizes[j] < sizes[j + 1] for j in range(len(sizes) - 1)
                        ):
                            instances.append(
                                BiasInstance(
                                    bias_type="revenge_trading",
                                    timestamp=streak_trades[-1].timestamp,
                                    description=(
                                        f"Consistently increasing size during a "
                                        f"{streak_len}-trade losing streak"
                                    ),
                                    trade_ids=[t.trade_id for t in streak_trades],
                                    severity="high",
                                    metrics={
                                        "streak_length": streak_len,
                                        "sizes": [str(s) for s in sizes],
                                    },
                                )
                            )
                    loss_streak_start = None

        return instances

    # -- overtrading detection -----------------------------------------------

    def _detect_overtrading(
        self, trades: list[TradeEntry]
    ) -> list[BiasInstance]:
        """Detect overtrading: too many trades in a short time period.

        Uses a sliding window approach to find clusters of excessive trading.
        """
        instances: list[BiasInstance] = []
        window = timedelta(hours=OVERTRADING_WINDOW_HOURS)

        i = 0
        while i < len(trades):
            window_end = trades[i].timestamp + window
            window_trades = [
                t for t in trades[i:]
                if t.timestamp <= window_end
            ]

            if len(window_trades) > OVERTRADING_MAX_TRADES:
                # Check if many of these are losses (stronger signal)
                losses_in_window = sum(
                    1 for t in window_trades
                    if t.pnl is not None and t.pnl < 0
                )
                loss_pct = losses_in_window / len(window_trades) if window_trades else 0

                severity = "high" if loss_pct > 0.6 else "moderate"

                instances.append(
                    BiasInstance(
                        bias_type="overtrading",
                        timestamp=trades[i].timestamp,
                        description=(
                            f"{len(window_trades)} trades in "
                            f"{OVERTRADING_WINDOW_HOURS}h window "
                            f"(max recommended: {OVERTRADING_MAX_TRADES}). "
                            f"{losses_in_window} were losses ({loss_pct:.0%})"
                        ),
                        trade_ids=[t.trade_id for t in window_trades],
                        severity=severity,
                        metrics={
                            "trade_count": len(window_trades),
                            "window_hours": OVERTRADING_WINDOW_HOURS,
                            "losses": losses_in_window,
                            "loss_percentage": round(loss_pct, 4),
                        },
                    )
                )
                # Skip past this window to avoid duplicate detection
                i += len(window_trades)
            else:
                i += 1

        return instances

    # -- anchoring detection -------------------------------------------------

    def _detect_anchoring(
        self, trades: list[TradeEntry]
    ) -> list[BiasInstance]:
        """Detect anchoring bias: holding losers too long vs cutting winners short.

        Compares average duration of winning vs losing trades.
        """
        instances: list[BiasInstance] = []

        winners = [
            t for t in trades
            if t.pnl is not None and t.pnl > 0 and t.duration is not None
        ]
        losers = [
            t for t in trades
            if t.pnl is not None and t.pnl < 0 and t.duration is not None
        ]

        if not winners or not losers:
            return instances

        avg_winner_duration = sum(t.duration for t in winners if t.duration is not None) / len(winners)
        avg_loser_duration = sum(t.duration for t in losers if t.duration is not None) / len(losers)

        if avg_winner_duration > 0 and avg_loser_duration / avg_winner_duration > ANCHORING_DURATION_RATIO:
            ratio = avg_loser_duration / avg_winner_duration

            severity = "high" if ratio > 3.0 else "moderate"

            instances.append(
                BiasInstance(
                    bias_type="anchoring",
                    timestamp=trades[-1].timestamp,
                    description=(
                        f"Losing trades held {ratio:.1f}x longer than winners. "
                        f"Avg winner: {avg_winner_duration:.0f}s, "
                        f"avg loser: {avg_loser_duration:.0f}s"
                    ),
                    trade_ids=[],
                    severity=severity,
                    metrics={
                        "avg_winner_duration_s": round(avg_winner_duration, 1),
                        "avg_loser_duration_s": round(avg_loser_duration, 1),
                        "duration_ratio": round(ratio, 2),
                        "winner_count": len(winners),
                        "loser_count": len(losers),
                    },
                )
            )

        # Also check for individual extreme outliers — losers held exceptionally long
        if avg_winner_duration > 0:
            extreme_threshold = avg_winner_duration * 5  # 5x+ is extreme
            extreme_losers = [
                t for t in losers
                if t.duration is not None and t.duration > extreme_threshold
            ]

            for t in extreme_losers:
                assert t.duration is not None
                instances.append(
                    BiasInstance(
                        bias_type="anchoring",
                        timestamp=t.timestamp,
                        description=(
                            f"Losing trade on {t.symbol} held for "
                            f"{t.duration:.0f}s ({t.duration / avg_winner_duration:.1f}x "
                            f"avg winner duration) with PnL of {t.pnl}"
                        ),
                        trade_ids=[t.trade_id],
                        severity="high",
                        metrics={
                            "duration_s": round(t.duration, 1),
                            "vs_avg_winner_ratio": round(
                                t.duration / avg_winner_duration, 2
                            ),
                            "pnl": str(t.pnl),
                        },
                    )
                )

        return instances

    # -- recency bias detection ----------------------------------------------

    def _detect_recency_bias(
        self, trades: list[TradeEntry]
    ) -> list[BiasInstance]:
        """Detect recency bias: over-weighting recent results.

        Compares recent performance metrics against overall history
        to identify when recent trades are disproportionately influencing
        behavior (e.g., dramatically different sizing after a short streak).
        """
        instances: list[BiasInstance] = []
        trades_with_pnl = [t for t in trades if t.pnl is not None]

        if len(trades_with_pnl) < RECENCY_WINDOW_TRADES * 2:
            return instances

        # Compare recent win rate vs overall
        overall_wins = sum(1 for t in trades_with_pnl if t.pnl is not None and t.pnl > 0)
        overall_rate = overall_wins / len(trades_with_pnl)

        recent = trades_with_pnl[-RECENCY_WINDOW_TRADES:]
        recent_wins = sum(1 for t in recent if t.pnl is not None and t.pnl > 0)
        recent_rate = recent_wins / RECENCY_WINDOW_TRADES

        # Check if recent sizing has deviated significantly
        all_sizes = [float(t.size) for t in trades_with_pnl]
        recent_sizes = [float(t.size) for t in recent]

        avg_overall_size = sum(all_sizes) / len(all_sizes)
        avg_recent_size = sum(recent_sizes) / len(recent_sizes)

        # Detect size increase after winning streak
        if (
            recent_rate > overall_rate + 0.3
            and avg_recent_size > avg_overall_size * 1.3
        ):
            instances.append(
                BiasInstance(
                    bias_type="recency_bias",
                    timestamp=recent[-1].timestamp,
                    description=(
                        f"Recent winning streak (win rate {recent_rate:.0%} vs "
                        f"overall {overall_rate:.0%}) appears to be influencing "
                        f"position sizing (avg size up {((avg_recent_size / avg_overall_size) - 1) * 100:.0f}%)"
                    ),
                    trade_ids=[t.trade_id for t in recent],
                    severity="moderate",
                    metrics={
                        "recent_win_rate": round(recent_rate, 4),
                        "overall_win_rate": round(overall_rate, 4),
                        "avg_recent_size": round(avg_recent_size, 4),
                        "avg_overall_size": round(avg_overall_size, 4),
                        "size_change_pct": round(
                            ((avg_recent_size / avg_overall_size) - 1) * 100, 2
                        ),
                    },
                )
            )

        # Detect size decrease after losing streak
        if (
            recent_rate < overall_rate - 0.3
            and avg_recent_size < avg_overall_size * 0.7
        ):
            instances.append(
                BiasInstance(
                    bias_type="recency_bias",
                    timestamp=recent[-1].timestamp,
                    description=(
                        f"Recent losing streak (win rate {recent_rate:.0%} vs "
                        f"overall {overall_rate:.0%}) appears to be causing "
                        f"excessive size reduction (avg size down "
                        f"{(1 - (avg_recent_size / avg_overall_size)) * 100:.0f}%)"
                    ),
                    trade_ids=[t.trade_id for t in recent],
                    severity="moderate",
                    metrics={
                        "recent_win_rate": round(recent_rate, 4),
                        "overall_win_rate": round(overall_rate, 4),
                        "avg_recent_size": round(avg_recent_size, 4),
                        "avg_overall_size": round(avg_overall_size, 4),
                        "size_change_pct": round(
                            (1 - (avg_recent_size / avg_overall_size)) * 100, 2
                        ),
                    },
                )
            )

        # Check confidence divergence from actual performance
        recent_with_conf = [t for t in recent if t.confidence > 0]
        if recent_with_conf:
            avg_recent_confidence = float(
                sum(t.confidence for t in recent_with_conf)
                / len(recent_with_conf)
            )
            if recent_rate < 0.3 and avg_recent_confidence > 70:
                instances.append(
                    BiasInstance(
                        bias_type="recency_bias",
                        timestamp=recent[-1].timestamp,
                        description=(
                            f"Confidence ({avg_recent_confidence:.0f}%) significantly "
                            f"diverges from actual recent win rate ({recent_rate:.0%}). "
                            f"May be anchored to past performance."
                        ),
                        trade_ids=[t.trade_id for t in recent_with_conf],
                        severity="high",
                        metrics={
                            "avg_confidence": round(avg_recent_confidence, 2),
                            "actual_win_rate": round(recent_rate, 4),
                            "divergence": round(
                                avg_recent_confidence / 100 - recent_rate, 4
                            ),
                        },
                    )
                )

        return instances

    # -- scoring -------------------------------------------------------------

    @staticmethod
    def _score_revenge(
        instances: list[BiasInstance], total_trades: int
    ) -> float:
        """Score revenge trading bias (0.0 to 1.0)."""
        if not instances:
            return 0.0
        high_count = sum(1 for i in instances if i.severity == "high")
        moderate_count = sum(1 for i in instances if i.severity == "moderate")
        raw = (high_count * 2 + moderate_count) / max(total_trades / 10, 1)
        return min(raw, 1.0)

    @staticmethod
    def _score_overtrading(
        instances: list[BiasInstance], total_trades: int
    ) -> float:
        """Score overtrading bias (0.0 to 1.0)."""
        if not instances:
            return 0.0
        # Count total trades involved in overtrading windows
        affected = sum(
            i.metrics.get("trade_count", 0) for i in instances
        )
        raw = affected / max(total_trades, 1)
        return min(raw, 1.0)

    @staticmethod
    def _score_anchoring(instances: list[BiasInstance]) -> float:
        """Score anchoring bias (0.0 to 1.0)."""
        if not instances:
            return 0.0
        # Use the worst duration ratio
        max_ratio = max(
            (i.metrics.get("duration_ratio", 0) for i in instances),
            default=0,
        )
        # Ratio of 2.0 = score 0.25, 4.0 = 0.5, 8.0+ = 1.0
        if isinstance(max_ratio, (int, float)):
            raw = (max_ratio - 1.0) / 7.0  # scale 1-8 to 0-1
            return min(max(raw, 0.0), 1.0)
        return 0.0

    @staticmethod
    def _score_recency(instances: list[BiasInstance]) -> float:
        """Score recency bias (0.0 to 1.0)."""
        if not instances:
            return 0.0
        high_count = sum(1 for i in instances if i.severity == "high")
        moderate_count = sum(1 for i in instances if i.severity == "moderate")
        raw = (high_count * 0.5 + moderate_count * 0.25)
        return min(raw, 1.0)

    # -- recommendations -----------------------------------------------------

    @staticmethod
    def _build_recommendations(
        revenge: float,
        overtrading: float,
        anchoring: float,
        recency: float,
    ) -> list[str]:
        """Build actionable recommendations based on bias scores."""
        recs: list[str] = []

        if revenge > 0.3:
            recs.append(
                "Implement a mandatory cooldown period after losing trades "
                "before placing new orders."
            )
            recs.append(
                "Enforce fixed position sizing rules regardless of recent "
                "trade outcomes."
            )

        if overtrading > 0.3:
            recs.append(
                f"Limit trades to {OVERTRADING_MAX_TRADES} per "
                f"{OVERTRADING_WINDOW_HOURS}-hour window."
            )
            recs.append(
                "Focus on higher-quality setups rather than increasing frequency."
            )

        if anchoring > 0.3:
            recs.append(
                "Review and tighten stop-loss discipline. Consider "
                "time-based stops for trades that stagnate."
            )
            recs.append(
                "Let winning trades run longer. Consider trailing stops "
                "instead of fixed take-profits."
            )

        if recency > 0.3:
            recs.append(
                "Base confidence and sizing on statistical edge over the "
                "full trade history, not just recent results."
            )
            recs.append(
                "Regularly review overall strategy performance metrics "
                "rather than fixating on the last few trades."
            )

        if not recs:
            recs.append("No significant biases detected. Continue monitoring.")

        return recs

    @staticmethod
    def _build_summary(
        revenge: float,
        overtrading: float,
        anchoring: float,
        recency: float,
        overall: float,
    ) -> str:
        """Build a human-readable summary of the bias analysis."""
        if overall < 0.15:
            return "Trading behavior appears disciplined with no significant biases detected."

        biases_detected: list[str] = []
        if revenge > 0.3:
            biases_detected.append(
                f"revenge trading (score: {revenge:.0%})"
            )
        if overtrading > 0.3:
            biases_detected.append(
                f"overtrading (score: {overtrading:.0%})"
            )
        if anchoring > 0.3:
            biases_detected.append(
                f"anchoring (score: {anchoring:.0%})"
            )
        if recency > 0.3:
            biases_detected.append(
                f"recency bias (score: {recency:.0%})"
            )

        if not biases_detected:
            return (
                f"Minor bias indicators detected (overall score: {overall:.0%}) "
                f"but none at concerning levels."
            )

        bias_list = ", ".join(biases_detected)
        severity = "significant" if overall > 0.5 else "moderate"

        return (
            f"Analysis detected {severity} bias indicators: {bias_list}. "
            f"Overall bias score: {overall:.0%}. "
            f"See recommendations for corrective actions."
        )
