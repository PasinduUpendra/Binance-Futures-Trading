"""
Adaptive strategy selector for the Claude Quant trading system.

Sits at the top of the strategy hierarchy: detects the current market
regime, picks the best concrete strategy, generates a signal, and
applies a final confidence gate before returning it to the caller.

This is the primary entry point the orchestrator should use.

Multi-timeframe support
~~~~~~~~~~~~~~~~~~~~~~~
The ``get_signal_multi_tf`` method is the preferred entry point.
It accepts **both** 4H and 1H DataFrames so that:

- Regime detection runs on 4H data (macro context).
- SupertrendTrend and MeanReversion receive 4H indicator data with
  the 1H close used as ``entry_price`` for precise timing.
- BreakoutTrader / TrendFollower (fallback) receive 1H data.
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from src.strategies.base_strategy import BaseStrategy, Signal, SignalDirection
from src.strategies.breakout_trader import BreakoutTrader
from src.strategies.mean_reversion import MeanReversion
from src.strategies.regime_detector import MarketRegime, RegimeDetector, RegimeState
from src.strategies.supertrend_trend import SupertrendTrend
from src.strategies.trend_follower import TrendFollower

logger = logging.getLogger(__name__)

# Strategies that operate on 4H data and accept an entry_price kwarg.
_4H_STRATEGIES = (SupertrendTrend, MeanReversion)


class AdaptiveStrategy:
    """Regime-aware strategy router.

    Workflow (multi-timeframe)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~
    1. ``RegimeDetector.detect(df_4h)``  ->  ``RegimeState``
    2. ``select_strategy(regime_state)``  ->  concrete ``BaseStrategy`` or None
    3. Strategy-appropriate signal generation:
       - SupertrendTrend / MeanReversion: ``strategy.generate_signal(df_4h, entry_price=close_1h)``
       - BreakoutTrader / TrendFollower:  ``strategy.generate_signal(df_1h)``
    4. Confidence gate: reject if < ``MIN_CONFIDENCE``

    Regime -> strategy mapping (updated v3)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    - **TRENDING** + ADX >= 18 : ``SupertrendTrend`` (4H Supertrend flips)
    - **RANGING**  + ADX < 25  : ``MeanReversion`` (4H BB extremes)
    - **VOLATILE** + BB expanding : ``BreakoutTrader`` (1H breakouts)
    - **QUIET**               : **no trade** (returns ``None``)

    TrendFollower is kept as fallback for TRENDING when ADX < 18.
    """

    # Lowered from 40→35→25. Each strategy has its own quality gates
    # (2/3 confirmation for MR, volume/squeeze for BO). The confidence
    # score measures "how extreme" the setup is, not "if" there's a setup.
    MIN_CONFIDENCE: float = 25.0

    def __init__(self) -> None:
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

        # Instantiate regime detector
        self._regime_detector = RegimeDetector()

        # Pre-instantiate concrete strategies (stateless, safe to reuse)
        self._supertrend_trend = SupertrendTrend()
        self._trend_follower = TrendFollower()
        self._mean_reversion = MeanReversion()
        self._breakout_trader = BreakoutTrader()

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

        if regime.regime == MarketRegime.TRENDING:
            if regime.adx >= 18.0:
                self.logger.info(
                    "Regime TRENDING with ADX=%.1f -> SupertrendTrend (4H)",
                    regime.adx,
                )
                return self._supertrend_trend
            else:
                self.logger.info(
                    "Regime TRENDING but ADX=%.1f < 18 -> NO TRADE (weak trend)",
                    regime.adx,
                )
                return None

        if regime.regime == MarketRegime.RANGING:
            # ADX 18-19.99: regime detector says RANGING but ADX is strong
            # enough for SupertrendTrend (gate is 18.0). Route to it instead
            # of discarding — closes the dead zone between detectors.
            if regime.adx >= 18.0:
                self.logger.info(
                    "Regime RANGING but ADX=%.1f >= 18 -> SupertrendTrend "
                    "(dead zone bridge)",
                    regime.adx,
                )
                return self._supertrend_trend
            self.logger.info(
                "Regime RANGING (ADX=%.1f) -> NO TRADE "
                "(MeanReversion disabled: 25%% WR in paper trading, 5.3%% WR historical)",
                regime.adx,
            )
            return None

        if regime.regime == MarketRegime.VOLATILE:
            # Re-enabled with ADX gate. BreakoutTrader's own volume surge
            # check (VOLUME_SURGE_MULT) provides the second filter.
            # v6.10 fixed the volume scoring formula (MED-3).
            if regime.adx >= 15.0:
                self.logger.info(
                    "Regime VOLATILE (BBw=%.2f, ADX=%.1f) -> BreakoutTrader (1H)",
                    regime.bb_width_ratio,
                    regime.adx,
                )
                return self._breakout_trader
            self.logger.info(
                "Regime VOLATILE (BBw=%.2f) but ADX=%.1f < 15 -> NO TRADE",
                regime.bb_width_ratio,
                regime.adx,
            )
            return None

        self.logger.warning("No strategy selected for regime=%s", regime.regime.value)
        return None

    def get_signal_multi_tf(
        self,
        df_4h: pd.DataFrame,
        df_1h: pd.DataFrame,
    ) -> Optional[Signal]:
        """Multi-timeframe signal generation (preferred entry point).

        Parameters
        ----------
        df_4h : DataFrame
            4H indicator-enriched data for regime detection and
            SupertrendTrend / MeanReversion signal generation.
        df_1h : DataFrame
            1H indicator-enriched data for entry price timing and
            BreakoutTrader / TrendFollower signal generation.
        """
        # 1. Detect regime on 4H
        try:
            regime = self._regime_detector.detect(df_4h)
        except (KeyError, ValueError) as exc:
            self.logger.error("Regime detection failed: %s", exc)
            return None

        # 2. Select strategy
        strategy = self.select_strategy(regime)
        if strategy is None:
            return None

        # 3. Generate signal with correct timeframe data
        try:
            if isinstance(strategy, _4H_STRATEGIES):
                # 4H strategies: use 4H data, 1H close as entry_price
                close_1h = float(df_1h["close"].dropna().iloc[-1])
                signal = strategy.generate_signal(df_4h, entry_price=close_1h)

                # If 4H flip returned NONE and strategy is SupertrendTrend,
                # try 1H continuation entry in established 4H trend.
                if (
                    signal.direction == SignalDirection.NONE
                    and isinstance(strategy, SupertrendTrend)
                ):
                    signal = strategy.generate_continuation_signal(df_4h, df_1h)
            else:
                # 1H strategies (TrendFollower, BreakoutTrader)
                signal = strategy.generate_signal(df_1h)
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
            "Adaptive signal APPROVED: %s %s @ %.6f "
            "(confidence=%.1f%%, regime=%s, strategy=%s)",
            signal.direction.value.upper(),
            signal.strategy_name,
            signal.entry_price,
            signal.confidence,
            regime.regime.value,
            strategy.name,
        )

        return signal

    def get_signal(self, df: pd.DataFrame) -> Optional[Signal]:
        """Single-timeframe pipeline (legacy, kept for backward compat).

        For new code, prefer ``get_signal_multi_tf``.
        """
        try:
            regime = self._regime_detector.detect(df)
        except (KeyError, ValueError) as exc:
            self.logger.error("Regime detection failed: %s", exc)
            return None

        strategy = self.select_strategy(regime)
        if strategy is None:
            return None

        try:
            signal = strategy.generate_signal(df)
        except Exception:
            self.logger.exception(
                "Strategy %s raised an unexpected exception", strategy.name
            )
            return None

        if signal.direction == SignalDirection.NONE:
            self.logger.info(
                "Strategy %s returned NONE signal: %s",
                strategy.name,
                signal.reasoning,
            )
            return None

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
            "Adaptive signal APPROVED: %s %s @ %.6f "
            "(confidence=%.1f%%, regime=%s, strategy=%s)",
            signal.direction.value.upper(),
            signal.strategy_name,
            signal.entry_price,
            signal.confidence,
            regime.regime.value,
            strategy.name,
        )

        return signal

    # ------------------------------------------------------------------
    # Supertrend reversal exit check
    # ------------------------------------------------------------------

    def check_supertrend_reversal(
        self,
        df_4h: pd.DataFrame,
        position_direction: str,
    ) -> bool:
        """Check if 4H Supertrend has flipped against an open position.

        Parameters
        ----------
        df_4h : DataFrame
            4H indicator-enriched data (must have supertrend_direction).
        position_direction : str
            'long' or 'short'.

        Returns
        -------
        bool
            True if Supertrend has reversed and position should be closed.
        """
        col = self._supertrend_trend.COL_SUPERTREND_DIR
        if col not in df_4h.columns:
            return False

        st_dir = BaseStrategy._safe_last(df_4h[col])
        if st_dir != st_dir:  # NaN check
            return False

        if position_direction == "long" and st_dir < 0:
            self.logger.info(
                "Supertrend reversal EXIT: position is LONG but 4H Supertrend "
                "flipped BEARISH (dir=%.0f)", st_dir,
            )
            return True
        if position_direction == "short" and st_dir > 0:
            self.logger.info(
                "Supertrend reversal EXIT: position is SHORT but 4H Supertrend "
                "flipped BULLISH (dir=%.0f)", st_dir,
            )
            return True

        return False

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    @property
    def regime_detector(self) -> RegimeDetector:
        """Expose the detector so callers can inspect regime independently."""
        return self._regime_detector

    @property
    def supertrend_trend(self) -> SupertrendTrend:
        return self._supertrend_trend

    @property
    def trend_follower(self) -> TrendFollower:
        return self._trend_follower

    @property
    def mean_reversion(self) -> MeanReversion:
        return self._mean_reversion

    @property
    def breakout_trader(self) -> BreakoutTrader:
        return self._breakout_trader
