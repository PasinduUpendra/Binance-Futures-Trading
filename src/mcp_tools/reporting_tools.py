"""
MCP server for reporting and trade journaling.

Provides P&L tracking, performance summaries, daily reports, and
a searchable trade log. Reads from agent state files and trade
result history.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
from pydantic import BaseModel, Field

logger = logging.getLogger("claude_quant.mcp_tools.reporting")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
_STATE_DIR = os.path.join(_PROJECT_ROOT, "user_data", "agent_state")
_REPORTS_DIR = os.path.join(_PROJECT_ROOT, "docs", "reports")
_LOGS_DIR = os.path.join(_PROJECT_ROOT, "user_data", "logs")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_decimal(value: Any) -> Decimal:
    """Safe conversion to Decimal."""
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _load_json(path: str) -> dict[str, Any] | list[Any]:
    """Load a JSON file, returning empty dict on failure."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_json(path: str, data: Any) -> None:
    """Save data to a JSON file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _load_trade_log() -> list[dict[str, Any]]:
    """Load the full trade log from state."""
    path = os.path.join(_STATE_DIR, "trade_log.json")
    data = _load_json(path)
    if isinstance(data, list):
        return data
    return data.get("trades", [])


def _load_daily_state() -> dict[str, Any]:
    """Load the daily state (start-of-day balance, etc.)."""
    return _load_json(os.path.join(_STATE_DIR, "daily_state.json"))


def _load_trade_results() -> list[dict[str, Any]]:
    """Load trade results for win/loss tracking."""
    data = _load_json(os.path.join(_STATE_DIR, "trade_results.json"))
    if isinstance(data, dict):
        return data.get("trades", [])
    return data if isinstance(data, list) else []


def _load_balance_history() -> list[dict[str, Any]]:
    """Load balance snapshots over time."""
    data = _load_json(os.path.join(_STATE_DIR, "balance_history.json"))
    if isinstance(data, dict):
        return data.get("snapshots", [])
    return data if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# Pydantic input schemas
# ---------------------------------------------------------------------------


class PerformanceSummaryInput(BaseModel):
    """Input for performance summary."""

    days: int = Field(default=7, ge=1, le=365, description="Number of days to include in the summary")


class GenerateReportInput(BaseModel):
    """Input for full report generation."""

    date: str = Field(
        default="",
        description="Date in YYYY-MM-DD format. Defaults to today (UTC).",
    )


class TradeLogInput(BaseModel):
    """Input for trade log retrieval."""

    limit: int = Field(default=20, ge=1, le=500, description="Number of recent trades to return")


# ---------------------------------------------------------------------------
# Core reporting logic
# ---------------------------------------------------------------------------


def _compute_daily_pnl() -> dict[str, Any]:
    """Compute today's P&L from daily state and balance history."""
    daily_state = _load_daily_state()
    start_balance = _to_decimal(daily_state.get("start_of_day_balance", 0))

    # Try to get current balance from latest snapshot
    balance_history = _load_balance_history()
    if balance_history:
        latest = balance_history[-1]
        current_balance = _to_decimal(latest.get("balance", 0))
    else:
        current_balance = start_balance

    # Compute P&L
    if start_balance > 0:
        daily_pnl = current_balance - start_balance
        daily_pnl_pct = (daily_pnl / start_balance) * 100
    else:
        daily_pnl = Decimal("0")
        daily_pnl_pct = Decimal("0")

    # Today's trades
    today_str = _utc_now().strftime("%Y-%m-%d")
    trade_log = _load_trade_log()
    today_trades = [
        t for t in trade_log
        if t.get("closed_at", t.get("opened_at", "")).startswith(today_str)
    ]

    # Win/loss count
    wins = sum(1 for t in today_trades if _to_decimal(t.get("pnl", 0)) > 0)
    losses = sum(1 for t in today_trades if _to_decimal(t.get("pnl", 0)) < 0)
    breakeven = len(today_trades) - wins - losses

    # Total realized PnL from trades
    realised_pnl = sum(_to_decimal(t.get("pnl", 0)) for t in today_trades)
    fees = sum(_to_decimal(t.get("fees", 0)) for t in today_trades)

    return {
        "date": today_str,
        "start_balance": str(start_balance),
        "current_balance": str(current_balance),
        "daily_pnl": str(daily_pnl),
        "daily_pnl_pct": str(round(daily_pnl_pct, 4)),
        "realised_pnl": str(realised_pnl),
        "total_fees": str(fees),
        "net_realised_pnl": str(realised_pnl - fees),
        "trades_today": len(today_trades),
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "win_rate": round(wins / len(today_trades), 4) if today_trades else 0.0,
    }


def _compute_performance_summary(days: int) -> dict[str, Any]:
    """Compute performance summary for a period."""
    cutoff = _utc_now() - timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    trade_log = _load_trade_log()
    trade_results = _load_trade_results()
    balance_history = _load_balance_history()

    # Filter trades in period
    period_trades = []
    for t in trade_log:
        trade_date = t.get("closed_at", t.get("opened_at", ""))
        if trade_date >= cutoff_str:
            period_trades.append(t)

    # Compute aggregate stats
    total_pnl = sum(_to_decimal(t.get("pnl", 0)) for t in period_trades)
    total_fees = sum(_to_decimal(t.get("fees", 0)) for t in period_trades)
    net_pnl = total_pnl - total_fees

    wins = sum(1 for t in period_trades if _to_decimal(t.get("pnl", 0)) > 0)
    losses = sum(1 for t in period_trades if _to_decimal(t.get("pnl", 0)) < 0)
    win_rate = wins / len(period_trades) if period_trades else 0.0

    # Average win and loss
    win_amounts = [_to_decimal(t.get("pnl", 0)) for t in period_trades if _to_decimal(t.get("pnl", 0)) > 0]
    loss_amounts = [_to_decimal(t.get("pnl", 0)) for t in period_trades if _to_decimal(t.get("pnl", 0)) < 0]

    avg_win = sum(win_amounts) / len(win_amounts) if win_amounts else Decimal("0")
    avg_loss = sum(loss_amounts) / len(loss_amounts) if loss_amounts else Decimal("0")

    # Profit factor
    gross_profit = sum(win_amounts)
    gross_loss = abs(sum(loss_amounts))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else Decimal("inf")

    # Expectancy: (win_rate * avg_win) + (loss_rate * avg_loss)
    loss_rate = 1 - win_rate
    expectancy = (Decimal(str(win_rate)) * avg_win) + (Decimal(str(loss_rate)) * avg_loss)

    # Max drawdown from balance history
    max_drawdown = Decimal("0")
    max_drawdown_pct = Decimal("0")
    peak = Decimal("0")

    period_balances = [
        b for b in balance_history
        if b.get("timestamp", "") >= cutoff_str
    ]

    for snap in period_balances:
        bal = _to_decimal(snap.get("balance", 0))
        if bal > peak:
            peak = bal
        drawdown = peak - bal
        if drawdown > max_drawdown:
            max_drawdown = drawdown
            max_drawdown_pct = (drawdown / peak * 100) if peak > 0 else Decimal("0")

    # Best and worst trades
    sorted_by_pnl = sorted(period_trades, key=lambda t: float(_to_decimal(t.get("pnl", 0))))
    worst_trade = sorted_by_pnl[0] if sorted_by_pnl else None
    best_trade = sorted_by_pnl[-1] if sorted_by_pnl else None

    # Strategy breakdown
    strategy_stats: dict[str, dict[str, Any]] = {}
    for t in period_trades:
        strat = t.get("strategy", "unknown")
        if strat not in strategy_stats:
            strategy_stats[strat] = {"trades": 0, "wins": 0, "total_pnl": Decimal("0")}
        strategy_stats[strat]["trades"] += 1
        pnl = _to_decimal(t.get("pnl", 0))
        strategy_stats[strat]["total_pnl"] += pnl
        if pnl > 0:
            strategy_stats[strat]["wins"] += 1

    strategy_breakdown = []
    for strat, stats in strategy_stats.items():
        strategy_breakdown.append(
            {
                "strategy": strat,
                "trades": stats["trades"],
                "wins": stats["wins"],
                "win_rate": round(stats["wins"] / stats["trades"], 4) if stats["trades"] else 0,
                "total_pnl": str(stats["total_pnl"]),
            }
        )

    # Daily returns for Sharpe-like metric
    daily_returns: dict[str, Decimal] = {}
    for t in period_trades:
        trade_date = t.get("closed_at", t.get("opened_at", ""))[:10]
        if trade_date:
            daily_returns.setdefault(trade_date, Decimal("0"))
            daily_returns[trade_date] += _to_decimal(t.get("pnl", 0))

    return {
        "period_days": days,
        "period_start": cutoff_str,
        "period_end": _utc_now().strftime("%Y-%m-%d"),
        "total_trades": len(period_trades),
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 4),
        "total_pnl": str(total_pnl),
        "total_fees": str(total_fees),
        "net_pnl": str(net_pnl),
        "avg_win": str(avg_win),
        "avg_loss": str(avg_loss),
        "profit_factor": str(round(profit_factor, 4)) if profit_factor != Decimal("inf") else "inf",
        "expectancy": str(round(expectancy, 4)),
        "max_drawdown": str(max_drawdown),
        "max_drawdown_pct": str(round(max_drawdown_pct, 2)),
        "best_trade": {
            "symbol": best_trade.get("symbol", ""),
            "pnl": str(_to_decimal(best_trade.get("pnl", 0))),
            "strategy": best_trade.get("strategy", ""),
        }
        if best_trade
        else None,
        "worst_trade": {
            "symbol": worst_trade.get("symbol", ""),
            "pnl": str(_to_decimal(worst_trade.get("pnl", 0))),
            "strategy": worst_trade.get("strategy", ""),
        }
        if worst_trade
        else None,
        "strategy_breakdown": strategy_breakdown,
        "trading_days": len(daily_returns),
    }


def _generate_full_report(date_str: str) -> dict[str, Any]:
    """Generate a full daily report and persist it to the reports directory."""
    if not date_str:
        date_str = _utc_now().strftime("%Y-%m-%d")

    # Gather all components
    daily_pnl = _compute_daily_pnl()
    performance_7d = _compute_performance_summary(7)
    performance_30d = _compute_performance_summary(30)

    # Trade log for the date
    trade_log = _load_trade_log()
    day_trades = [
        t for t in trade_log
        if t.get("closed_at", t.get("opened_at", "")).startswith(date_str)
    ]

    # Circuit breaker history for the day
    daily_state = _load_daily_state()

    report = {
        "report_date": date_str,
        "generated_at": _utc_now_iso(),
        "daily_pnl": daily_pnl,
        "performance_7d": performance_7d,
        "performance_30d": performance_30d,
        "day_trades": day_trades,
        "trade_count": len(day_trades),
        "circuit_breaker_level": daily_state.get("circuit_breaker_level", "unknown"),
        "notes": daily_state.get("notes", ""),
    }

    # Build human-readable summary
    summary_lines = [
        f"=== Claude Quant Daily Report: {date_str} ===",
        "",
        f"Daily P&L: ${daily_pnl['daily_pnl']} ({daily_pnl['daily_pnl_pct']}%)",
        f"Balance: ${daily_pnl['current_balance']} (start: ${daily_pnl['start_balance']})",
        f"Trades today: {daily_pnl['trades_today']} (W:{daily_pnl['wins']} / L:{daily_pnl['losses']})",
        f"Win rate: {daily_pnl['win_rate']:.0%}" if daily_pnl['trades_today'] > 0 else "Win rate: N/A",
        "",
        "--- 7-Day Performance ---",
        f"Net P&L: ${performance_7d['net_pnl']}",
        f"Trades: {performance_7d['total_trades']} (Win rate: {performance_7d['win_rate']:.0%})",
        f"Profit factor: {performance_7d['profit_factor']}",
        f"Max drawdown: ${performance_7d['max_drawdown']} ({performance_7d['max_drawdown_pct']}%)",
        "",
        "--- 30-Day Performance ---",
        f"Net P&L: ${performance_30d['net_pnl']}",
        f"Trades: {performance_30d['total_trades']} (Win rate: {performance_30d['win_rate']:.0%})",
        f"Profit factor: {performance_30d['profit_factor']}",
        f"Max drawdown: ${performance_30d['max_drawdown']} ({performance_30d['max_drawdown_pct']}%)",
    ]

    # Strategy breakdown
    if performance_7d["strategy_breakdown"]:
        summary_lines.append("")
        summary_lines.append("--- Strategy Breakdown (7d) ---")
        for sb in performance_7d["strategy_breakdown"]:
            summary_lines.append(
                f"  {sb['strategy']}: {sb['trades']} trades, "
                f"WR={sb['win_rate']:.0%}, P&L=${sb['total_pnl']}"
            )

    report["summary_text"] = "\n".join(summary_lines)

    # Persist report
    report_path = os.path.join(_REPORTS_DIR, f"daily_report_{date_str}.json")
    _save_json(report_path, report)
    report["saved_to"] = report_path

    return report


# ---------------------------------------------------------------------------
# MCP tool definitions
# ---------------------------------------------------------------------------

_TOOL_DEFINITIONS: list[Tool] = [
    Tool(
        name="get_daily_pnl",
        description=(
            "Get today's P&L including start balance, current balance, realised P&L, "
            "fees, trade count, and win/loss breakdown."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="get_performance_summary",
        description=(
            "Get a performance summary over a specified number of days including "
            "win rate, profit factor, expectancy, max drawdown, and strategy breakdown."
        ),
        inputSchema=PerformanceSummaryInput.model_json_schema(),
    ),
    Tool(
        name="generate_report",
        description=(
            "Generate a comprehensive daily report with P&L, performance metrics, "
            "trade journal, and strategy analysis. Saves the report to docs/reports/."
        ),
        inputSchema=GenerateReportInput.model_json_schema(),
    ),
    Tool(
        name="get_trade_log",
        description=(
            "Retrieve recent entries from the trade journal with full details: "
            "symbol, strategy, entry/exit prices, P&L, duration, and notes."
        ),
        inputSchema=TradeLogInput.model_json_schema(),
    ),
]


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------


def create_reporting_server() -> Server:
    """Create and configure the Reporting MCP server."""
    server = Server("reporting-tools")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return _TOOL_DEFINITIONS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        try:
            result = await _dispatch_tool(name, arguments)
            return [TextContent(type="text", text=str(result))]
        except Exception as exc:
            logger.exception("Tool %s failed", name)
            error_msg = f"Error in {name}: {type(exc).__name__}: {exc}"
            return [TextContent(type="text", text=error_msg)]

    return server


async def _dispatch_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Route tool calls."""
    handlers = {
        "get_daily_pnl": _handle_get_daily_pnl,
        "get_performance_summary": _handle_get_performance_summary,
        "generate_report": _handle_generate_report,
        "get_trade_log": _handle_get_trade_log,
    }
    handler = handlers.get(name)
    if handler is None:
        raise ValueError(f"Unknown tool: {name}")
    return await handler(arguments)


async def _handle_get_daily_pnl(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handle get_daily_pnl tool call."""
    result = _compute_daily_pnl()
    result["timestamp"] = _utc_now_iso()
    logger.info("Daily P&L: %s (%s%%)", result["daily_pnl"], result["daily_pnl_pct"])
    return result


async def _handle_get_performance_summary(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handle get_performance_summary tool call."""
    params = PerformanceSummaryInput(**arguments)
    result = _compute_performance_summary(params.days)
    result["timestamp"] = _utc_now_iso()
    logger.info("Performance summary (%dd): %s trades, net P&L %s", params.days, result["total_trades"], result["net_pnl"])
    return result


async def _handle_generate_report(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handle generate_report tool call."""
    params = GenerateReportInput(**arguments)
    result = _generate_full_report(params.date)
    logger.info("Report generated for %s, saved to %s", result["report_date"], result.get("saved_to", "N/A"))
    return result


async def _handle_get_trade_log(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handle get_trade_log tool call."""
    params = TradeLogInput(**arguments)
    all_trades = _load_trade_log()

    # Return most recent trades
    recent = all_trades[-params.limit:] if len(all_trades) > params.limit else all_trades

    # Enrich each trade entry
    enriched = []
    for t in recent:
        entry_price = _to_decimal(t.get("entry_price", 0))
        exit_price = _to_decimal(t.get("exit_price", 0))
        pnl = _to_decimal(t.get("pnl", 0))

        # Calculate hold duration if timestamps available
        duration_str = ""
        opened = t.get("opened_at", "")
        closed = t.get("closed_at", "")
        if opened and closed:
            try:
                open_dt = datetime.fromisoformat(opened)
                close_dt = datetime.fromisoformat(closed)
                delta = close_dt - open_dt
                hours = delta.total_seconds() / 3600
                if hours < 1:
                    duration_str = f"{delta.total_seconds() / 60:.0f}m"
                elif hours < 24:
                    duration_str = f"{hours:.1f}h"
                else:
                    duration_str = f"{delta.days}d {hours % 24:.0f}h"
            except (ValueError, TypeError):
                pass

        enriched.append(
            {
                "symbol": t.get("symbol", ""),
                "strategy": t.get("strategy", ""),
                "direction": t.get("direction", t.get("side", "")),
                "entry_price": str(entry_price),
                "exit_price": str(exit_price),
                "amount": str(_to_decimal(t.get("amount", 0))),
                "leverage": t.get("leverage", 1),
                "pnl": str(pnl),
                "pnl_pct": str(
                    round(pnl / (entry_price * _to_decimal(t.get("amount", 1))) * 100, 2)
                    if entry_price > 0 and _to_decimal(t.get("amount", 0)) > 0
                    else Decimal("0")
                ),
                "fees": str(_to_decimal(t.get("fees", 0))),
                "opened_at": opened,
                "closed_at": closed,
                "duration": duration_str,
                "is_win": pnl > 0,
                "notes": t.get("notes", ""),
                "circuit_breaker_level": t.get("circuit_breaker_level", ""),
            }
        )

    result = {
        "trades": enriched,
        "count": len(enriched),
        "total_in_log": len(all_trades),
        "timestamp": _utc_now_iso(),
    }
    logger.info("Trade log: returning %d of %d trades", len(enriched), len(all_trades))
    return result


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    """Run the Reporting MCP server over stdio."""
    server = create_reporting_server()
    logger.info("Starting Reporting MCP server...")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    asyncio.run(main())
