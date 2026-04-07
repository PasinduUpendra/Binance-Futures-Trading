"""
Strategy engine for the Claude Quant trading system.

Public API
~~~~~~~~~~
- ``AdaptiveStrategy``   – regime-aware strategy router (primary entry point)
- ``RegimeDetector``     – market regime classifier
- ``MarketRegime``       – regime enum (TRENDING, RANGING, VOLATILE, QUIET)
- ``RegimeState``        – Pydantic model for a classified regime snapshot
- ``BaseStrategy``       – abstract base class for all concrete strategies
- ``Signal``             – Pydantic model for a trading signal
- ``SignalDirection``    – direction enum (LONG, SHORT, NONE)
- ``AdaptiveTrend``      – momentum-based strategy for ranging markets
- ``TrendFollower``      – trend-following strategy (EMA cross + ADX + Supertrend)
- ``MeanReversion``      – mean-reversion strategy (BB + Z-score + RSI)
- ``BreakoutTrader``     – breakout strategy (S/R + volume + BB squeeze)
- ``Scalper``            – scalping strategy (RSI divergence + trend confirmation)
- ``SupertrendTrend``    – 4H Supertrend flip strategy (+94% backtest return)

Helpers
~~~~~~~
- ``calculate_atr_stop`` – compute an ATR-based stop distance
- ``calculate_rr_ratio`` – compute reward-to-risk ratio
"""

from src.strategies.adaptive_trend import AdaptiveTrend
from src.strategies.adaptive_strategy import AdaptiveStrategy
from src.strategies.base_strategy import (
    BaseStrategy,
    Signal,
    SignalDirection,
    calculate_atr_stop,
    calculate_rr_ratio,
)
from src.strategies.breakout_trader import BreakoutTrader
from src.strategies.mean_reversion import MeanReversion
from src.strategies.regime_detector import MarketRegime, RegimeDetector, RegimeState
from src.strategies.scalper import Scalper
from src.strategies.supertrend_trend import SupertrendTrend
from src.strategies.trend_follower import TrendFollower

__all__ = [
    # Router
    "AdaptiveStrategy",
    # Regime
    "RegimeDetector",
    "MarketRegime",
    "RegimeState",
    # Base
    "BaseStrategy",
    "Signal",
    "SignalDirection",
    # Concrete strategies
    "SupertrendTrend",
    "AdaptiveTrend",
    "TrendFollower",
    "MeanReversion",
    "BreakoutTrader",
    "Scalper",
    # Helpers
    "calculate_atr_stop",
    "calculate_rr_ratio",
]
