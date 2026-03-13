"""
Claude Quant MCP Tools — Model Context Protocol servers for trading operations.

Each module exposes a ``create_*_server()`` factory that returns a configured
``mcp.server.Server`` instance ready to be run over stdio or any MCP transport.

Servers
-------
- **binance_tools**: Binance Futures account, market data, and order management.
- **analysis_tools**: Technical analysis, regime detection, signals, S/R levels.
- **risk_tools**: Circuit breaker, position sizing, correlation, portfolio risk.
- **reporting_tools**: P&L tracking, performance summaries, reports, trade log.
"""

from src.mcp_tools.analysis_tools import create_analysis_server
from src.mcp_tools.binance_tools import create_binance_server
from src.mcp_tools.reporting_tools import create_reporting_server
from src.mcp_tools.risk_tools import create_risk_server

__all__ = [
    "create_binance_server",
    "create_analysis_server",
    "create_risk_server",
    "create_reporting_server",
]
