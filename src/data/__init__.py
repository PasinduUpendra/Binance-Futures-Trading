"""
Claude Quant -- Data Layer
==========================

Public API surface for market data fetching, indicator calculation,
validation, and candle persistence.
"""

from src.data.candle_store import CandleRow, CandleStore
from src.data.data_validator import (
    DataValidator,
    ValidationDetail,
    ValidationResult,
    ValidationStatus,
)
from src.data.indicator_engine import IndicatorEngine
from src.data.market_data import (
    MarketDataClient,
    OrderBookData,
    OrderBookEntry,
    TickerData,
)

__all__ = [
    # market_data
    "MarketDataClient",
    "TickerData",
    "OrderBookData",
    "OrderBookEntry",
    # indicator_engine
    "IndicatorEngine",
    # data_validator
    "DataValidator",
    "ValidationResult",
    "ValidationDetail",
    "ValidationStatus",
    # candle_store
    "CandleStore",
    "CandleRow",
]
