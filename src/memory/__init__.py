"""
Memory layer for Claude Quant.

Provides trade journaling, performance tracking, behavioral bias detection,
and a unified memory client with MCP protocol support.
"""

from src.memory.bias_detector import BiasDetector, BiasInstance, BiasReport
from src.memory.decision_logger import DecisionLogger
from src.memory.performance_tracker import (
    DailyPnL,
    PerformanceTracker,
    StrategyPerformance,
)
from src.memory.trade_journal import TradeEntry, TradeJournal
from src.memory.trade_memory_client import (
    BehavioralAnalysis,
    BehavioralInsight,
    MemoryQuery,
    TradeMemoryClient,
)

__all__ = [
    "BehavioralAnalysis",
    "BehavioralInsight",
    "BiasDetector",
    "BiasInstance",
    "BiasReport",
    "DailyPnL",
    "DecisionLogger",
    "MemoryQuery",
    "PerformanceTracker",
    "StrategyPerformance",
    "TradeEntry",
    "TradeJournal",
    "TradeMemoryClient",
]
