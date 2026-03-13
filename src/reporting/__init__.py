"""
Reporting layer for Claude Quant.

Provides daily P&L calculation, a live terminal dashboard, Markdown report
generation, and multi-channel alert notifications.
"""

from src.reporting.alert_system import AlertConfig, AlertLevel, AlertSystem
from src.reporting.daily_pnl import CumulativeStats, DailyPnL, DailyPnLCalculator
from src.reporting.dashboard import Dashboard
from src.reporting.report_generator import ReportGenerator

__all__ = [
    # daily_pnl
    "DailyPnLCalculator",
    "DailyPnL",
    "CumulativeStats",
    # dashboard
    "Dashboard",
    # report_generator
    "ReportGenerator",
    # alert_system
    "AlertSystem",
    "AlertConfig",
    "AlertLevel",
]
