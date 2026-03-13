"""
Adaptive strategy selector for the Claude Quant trading system.

Sits at the top of the strategy hierarchy: detects the current market
regime, picks the best concrete strategy, generates a signal, and
applies a final confidence gate before returning it to the caller.

This is the primary entry point the orchestrator should use.
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from src.strategies.base_strategy import BaseStrategy, Signal, SignalDirection
from src.strategies.breakout_trader import BreakoutTrader
from src.strategies.mean_reversion import MeanReversion
from src.strategies.regime_detector import MarketRegime, RegimeDetector, RegimeState
from src.strategies.scalper import Scalper
from src.strategies.trend_follower import TrendFollower

logger = logging.getLogger(__name__)


class AdaptiveStrategy:
    """Regime-aware strategy router.

    Workflow
    ~~~~~~~~
    1. ``RegimeDetector.detect(df)``  ->  ``RegimeState``
    2. ``select_strategy(regime_state)``  ->  concrete ``BaseStrategy`` or None
    3. ``strategy.generate_signal(df)``  ->  ``Signal``
    4. Confidence gate: reject if < ``MIN_CONFIDENCE``

    Regime -> strategy mapping
    ~~~~~~~~~~~~~~~~~~~~~~~~~~
    - **TRENDING** + ADX > 25  : ``TrendFollower``
    - **RANGING**  + ADX < 20  : ``MeanReversion``
    - **VOLATILE** + BB expanding : ``BreakoutTrader``
    - **QUIET**               : **no trade** (returns ``None``)

    The ``Scalper`` is available as a secondary strategy when the regime
    is TRENDING or VOLATILE, but the adaptive router currently does not
    auto-select it.  Callers can instantiate it directly for scalping
    sessions.
    """

    MIN_CONFIDENCE: float = 40.0

    def __init__(self) -> None:
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

        # Instantiate regime detector
        self._regime_detector = RegimeDetector()

        # Pre-instantiate concrete strategies (stateless, safe to reuse)
        self._trend_follower = TrendFollower()
        self._mean_reversion = MeanReversion()
        self._breakout_trader = BreakoutTrader()
        self._scalper = Scalper()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def select_strategy(self, regime: RegimeState) -> Optional[BaseStrategy]:
        """Choose the most appropriate strategy for the given regime.

        Returns ``None`` when the regime is QUIET (no-trade zone) or
        when no strategy is suitable.
        """
        if regime.regime == MarketRegime.QUIET:
            self.logger.info(
                "Regime is QUIET (ADX=%.1f, BBw=%.2f). No strategy selected.",
                regime.adx,
                regime.bb_width_ratio,
            )
            return None

        if regime.regime == MarketRegime.TRENDING and regime.adx >= 25.0:
            self.logger.info(
                "Regime TRENDING with ADX=%.1f -> TrendFollower",
                regime.adx,
            )
            return self._trend_follower

        if regime.regime == MarketRegime.RANGING and regime.adx < 20.0:
            self.logger.info(
                "Regime RANGING with ADX=%.1f -> MeanReversion",
                regime.adx,
            )
            return self._mean_reversion

        if regime.regime == MarketRegime.VOLATILE and regime.bb_width_ratio >= 1.0:
            self.logger.info(
                "Regime VOLATILE with BB width ratio=%.2f -> BreakoutTrader",
                regime.bb_width_ratio,
            )
            return self._breakout_trader

        # Edge cases: regime detected but secondary conditions not met.
        # Fall back to the closest match.
        if regime.regime == MarketRegime.TRENDING:
            # ADX below 25 but still classified as trending — try trend follower
            # (it will gate on ADX internally and may return NONE signal)
            self.logger.info(
                "Regime TRENDING but ADX=%.1f < 25 — trying TrendFollower anyway",
                regime.adx,
            )
            return self._trend_follower

        if regime.regime == MarketRegime.RANGING:
            # ADX >= 20 but classified as ranging — try mean reversion
            self.logger.info(
                "Regime RANGING but ADX=%.1f >= 20 — trying MeanReversion anyway",
                regime.adx,
            )
            return self._mean_reversion

        if regime.regime == MarketRegime.VOLATILE:
            # BB not expanding but volatile — try breakout anyway
            self.logger.info(
                "Regime VOLATILE but BBw=%.2f — trying BreakoutTrader anyway",
                regime.bb_width_ratio,
            )
            return self._breakout_trader

        self.logger.warning("No strategy selected for regime=%s", regime.regime.value)
        return None

    def get_signal(self, df: pd.DataFrame) -> Optional[Signal]:
        """End-to-end pipeline: detect regime -> select strategy -> generate signal.

        Returns ``None`` when:
          - The regime is QUIET
          - No strategy is selected
          - The selected strategy returns a NONE-direction signal
          - The signal confidence is below ``MIN_CONFIDENCE``

        Parameters
        ----------
        df : DataFrame
            Indicator-enriched OHLCV data.

        Returns
        -------
        Signal or None
        """
        # 1. Detect regime
        try:
            regime = self._regime_detector.detect(df)
        except (KeyError, ValueError) as exc:
            self.logger.error("Regime detection failed: %s", exc)
            return None

        # 2. Select strategy
        strategy = self.select_strategy(regime)
        if strategy is None:
            self.logger.info("No strategy selected — returning no signal")
            return None

        # 3. Generate signal
        try:
            signal = strategy.generate_signal(df)
        except Exception:
            self.logger.exception(
                "Strategy %s raised an unexpected exception", strategy.name
            )
            return None

        # 4. Filter NONE signals
        if signal.direction == SignalDirection.NONE:
            self.logger.info(
                "Strategy %s returned NONE signal: %s",
                strategy.name,
                signal.reasoning,
            )
            return None

        # 5. Confidence gate
        if signal.confidence < self.MIN_CONFIDENCE:
            self.logger.info(
                "Signal confidence %.1f%% below threshold %.1f%% — rejected. "
                "Strategy=%s, Direction=%s",
                signal.confidence,
                self.MIN_CONFIDENCE,
                signal.strategy_name,
                signal.direction.value,
            )
            return None

        self.logger.info(
            "Adaptive signal APPROVED: %s %s @ %.6f (confidence=%.1f%%, regime=%s, strategy=%s)",
            signal.direction.value.upper(),
            signal.strategy_name,
            signal.entry_price,
            signal.confidence,
            regime.regime.value,
            strategy.name,
        )

        return signal

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    @property
    def regime_detector(self) -> RegimeDetector:
        """Expose the detector so callers can inspect regime independently."""
        return self._regime_detector

    @property
    def trend_follower(self) -> TrendFollower:
        return self._trend_follower

    @property
    def mean_reversion(self) -> MeanReversion:
        return self._mean_reversion

    @property
    def breakout_trader(self) -> BreakoutTrader:
        return self._breakout_trader

    @property
    def scalper(self) -> Scalper:
        return self._scalper
