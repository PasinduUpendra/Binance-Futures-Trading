"""
Risk management layer for Claude Quant.

Public API
----------
CircuitBreaker / CircuitBreakerLevel / CircuitBreakerState
    Hard safety limits — the single most critical component.

KellyCriterion
    Optimal and half-Kelly fraction calculations.

PositionSizer / PositionSize
    Translates Kelly fractions into concrete dollar amounts
    respecting circuit-breaker constraints.

LeverageManager / LeverageResult / LiquidationBuffer
    Maps (confidence, regime, CB level) to safe leverage.

DrawdownMonitor / DrawdownState
    Tracks peak balance and drawdown with disk persistence.

CorrelationMonitor / CorrelationCheckResult
    Prevents concentration risk in correlated assets.
"""

from src.risk.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConstraints,
    CircuitBreakerLevel,
    CircuitBreakerState,
    TradeResult,
)
from src.risk.correlation_monitor import (
    CorrelationCheckResult,
    CorrelationMonitor,
    Position,
)
from src.risk.drawdown_monitor import DrawdownMonitor, DrawdownState
from src.risk.kelly_criterion import KellyCriterion
from src.risk.leverage_manager import (
    LeverageManager,
    LeverageResult,
    LiquidationBuffer,
    MarketRegime,
)
from src.risk.position_sizer import PositionSize, PositionSizer

__all__ = [
    # Circuit breaker
    "CircuitBreaker",
    "CircuitBreakerConstraints",
    "CircuitBreakerLevel",
    "CircuitBreakerState",
    "TradeResult",
    # Kelly criterion
    "KellyCriterion",
    # Position sizing
    "PositionSizer",
    "PositionSize",
    # Leverage
    "LeverageManager",
    "LeverageResult",
    "LiquidationBuffer",
    "MarketRegime",
    # Drawdown
    "DrawdownMonitor",
    "DrawdownState",
    # Correlation
    "CorrelationMonitor",
    "CorrelationCheckResult",
    "Position",
]
