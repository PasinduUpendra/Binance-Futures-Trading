"""
Execution layer for Claude Quant.

Provides order management, position tracking, fee calculation,
and slippage estimation for Binance Futures trading.
"""

from src.execution.fee_calculator import FeeCalculator
from src.execution.order_manager import (
    OrderManager,
    OrderResult,
    OrderSide,
    OrderState,
    OrderStatus,
    OrderType,
)
from src.execution.position_tracker import Position, PositionTracker
from src.execution.slippage_estimator import (
    OrderBookLevel,
    SlippageEstimate,
    SlippageEstimator,
)

__all__ = [
    "FeeCalculator",
    "OrderBookLevel",
    "OrderManager",
    "OrderResult",
    "OrderSide",
    "OrderState",
    "OrderStatus",
    "OrderType",
    "Position",
    "PositionTracker",
    "SlippageEstimate",
    "SlippageEstimator",
]
