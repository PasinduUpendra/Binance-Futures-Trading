"""
Markdown report generator for daily trading summaries.

Produces a human-readable Markdown document that captures P&L, trade log,
strategy breakdown, risk metrics, and doubling progress.  Reports are
persisted to ``docs/reports/YYYY-MM-DD.md``.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.reporting.daily_pnl import CumulativeStats, DailyPnL

logger = logging.getLogger("claude_quant.reporting.report_generator")

# Default reports directory — relative to project root.
_DEFAULT_REPORTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "docs", "reports"
)


# ---------------------------------------------------------------------------
# ReportGenerator
# ---------------------------------------------------------------------------


class ReportGenerator:
    """Generates and persists daily Markdown trading reports.

    Parameters
    ----------
    reports_dir : str | None
        Directory to save reports.  Defaults to ``docs/reports/`` in the
        project root.
    initial_capital : Decimal
        Starting capital for doubling-progress calculation.  Default $75.
    """

    def __init__(
        self,
        reports_dir: str | None = None,
        initial_capital: Decimal = Decimal("75"),
    ) -> None:
        self._reports_dir = reports_dir or _DEFAULT_REPORTS_DIR
        self._initial_capital = initial_capital

    # -- public API ----------------------------------------------------------

    def generate_daily_report(
        self,
        daily_pnl: DailyPnL,
        trades: list[dict[str, Any]],
        strategy_perf: dict[str, dict[str, Any]],
        cumulative: CumulativeStats | None = None,
        circuit_breaker_level: str = "UNKNOWN",
        regime: str = "unknown",
    ) -> str:
        """Generate a complete daily Markdown report.

        Returns the report as a string.
        """
        lines: list[str] = []

        # Title
        lines.append(f"# Claude Quant Daily Report — {daily_pnl.date}")
        lines.append("")
        lines.append(
            f"*Generated {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}*"
        )
        lines.append("")

        # P&L summary
        lines.append("## P&L Summary")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        lines.append(f"| Start Balance | {daily_pnl.start_balance} USDT |")
        lines.append(f"| End Balance | {daily_pnl.end_balance} USDT |")
        lines.append(
            f"| Net P&L | {daily_pnl.net_pnl:+} USDT ({daily_pnl.pnl_pct:+.2f}%) |"
        )
        lines.append(f"| Realised P&L | {daily_pnl.realized_pnl:+} USDT |")
        lines.append(f"| Unrealised P&L | {daily_pnl.unrealized_pnl:+} USDT |")
        lines.append(f"| Fees | {daily_pnl.fees} USDT |")
        lines.append(f"| Trades | {daily_pnl.trades_count} |")
        lines.append(f"| Wins / Losses | {daily_pnl.wins} / {daily_pnl.losses} |")

        if daily_pnl.trades_count > 0:
            wr = (
                Decimal(str(daily_pnl.wins))
                / Decimal(str(daily_pnl.trades_count))
                * Decimal("100")
            )
            lines.append(f"| Win Rate | {wr:.1f}% |")

        lines.append(f"| Circuit Breaker | {circuit_breaker_level} |")
        lines.append(f"| Market Regime | {regime} |")
        lines.append("")

        # Trade log
        lines.append("## Trade Log")
        lines.append("")

        if trades:
            lines.append(
                "| # | Time | Symbol | Direction | Entry | Exit | PnL | Strategy |"
            )
            lines.append(
                "|---|------|--------|-----------|-------|------|-----|----------|"
            )
            for i, trade in enumerate(trades, 1):
                ts = trade.get("timestamp", "")
                if isinstance(ts, datetime):
                    ts = ts.strftime("%H:%M:%S")
                pnl = Decimal(str(trade.get("pnl", 0)))
                pnl_str = f"{pnl:+}"
                lines.append(
                    f"| {i} | {ts} | {trade.get('symbol', '')} | "
                    f"{trade.get('direction', '')} | {trade.get('entry_price', '')} | "
                    f"{trade.get('exit_price', '')} | {pnl_str} | "
                    f"{trade.get('strategy', '')} |"
                )
        else:
            lines.append("*No trades today.*")
        lines.append("")

        # Strategy breakdown
        lines.append("## Strategy Breakdown")
        lines.append("")

        if strategy_perf:
            lines.append(
                "| Strategy | Trades | Wins | Losses | Win Rate | PnL | Avg PnL |"
            )
            lines.append(
                "|----------|--------|------|--------|----------|-----|---------|"
            )
            for name, perf in sorted(strategy_perf.items()):
                s_trades = perf.get("trades", 0)
                s_wins = perf.get("wins", 0)
                s_losses = perf.get("losses", 0)
                s_wr = perf.get("win_rate", 0)
                s_pnl = Decimal(str(perf.get("pnl", 0)))
                s_avg = (
                    s_pnl / Decimal(str(s_trades))
                    if s_trades > 0
                    else Decimal("0")
                )
                lines.append(
                    f"| {name} | {s_trades} | {s_wins} | {s_losses} | "
                    f"{s_wr:.0f}% | {s_pnl:+} | {s_avg:+.2f} |"
                )
        else:
            lines.append("*No strategy data available.*")
        lines.append("")

        # Risk metrics
        lines.append("## Risk Metrics")
        lines.append("")

        if cumulative is not None:
            lines.append("| Metric | Value |")
            lines.append("|--------|-------|")
            lines.append(f"| Total P&L | {cumulative.total_pnl:+} USDT ({cumulative.total_pnl_pct:+.2f}%) |")
            lines.append(f"| Max Drawdown | {cumulative.max_drawdown:.2f}% |")
            lines.append(f"| Sharpe Ratio | {cumulative.sharpe_ratio:.2f} |")
            lines.append(f"| Overall Win Rate | {cumulative.win_rate:.1f}% |")
            lines.append(f"| Average Daily P&L | {cumulative.avg_daily_pnl:+.2f} USDT |")
            lines.append(f"| Best Day | {cumulative.best_day:+} USDT |")
            lines.append(f"| Worst Day | {cumulative.worst_day:+} USDT |")
            lines.append(f"| Profitable Days | {cumulative.profitable_days} |")
            lines.append(f"| Losing Days | {cumulative.losing_days} |")
        else:
            lines.append("*Cumulative stats not available (first day).*")
        lines.append("")

        # Doubling progress
        lines.append("## Doubling Progress")
        lines.append("")
        target = self._initial_capital * Decimal("2")
        current = daily_pnl.end_balance
        progress = Decimal("0")
        if target > self._initial_capital:
            progress = (
                (current - self._initial_capital)
                / (target - self._initial_capital)
            ) * Decimal("100")
            progress = max(Decimal("0"), min(progress, Decimal("100")))

        bar_filled = int(float(progress) / 5)  # 20-char bar
        bar_empty = 20 - bar_filled
        bar = "[" + "#" * bar_filled + "." * bar_empty + "]"

        lines.append(f"Initial: **{self._initial_capital} USDT** | Target: **{target} USDT**")
        lines.append("")
        lines.append(f"Current: **{current} USDT** | Progress: **{progress:.1f}%**")
        lines.append("")
        lines.append(f"```")
        lines.append(f"{bar} {progress:.1f}%")
        lines.append(f"```")
        lines.append("")

        # Footer
        lines.append("---")
        lines.append(
            "*Report generated by Claude Quant Daily Reporter. "
            "All monetary values in USDT. Timestamps UTC.*"
        )
        lines.append("")

        report = "\n".join(lines)
        logger.info("Generated daily report for %s (%d chars)", daily_pnl.date, len(report))
        return report

    def save_report(
        self,
        report: str,
        report_date: date | None = None,
    ) -> str:
        """Save *report* to ``docs/reports/YYYY-MM-DD.md``.

        Parameters
        ----------
        report : str
            The Markdown content.
        report_date : date | None
            Date to use in the filename.  Defaults to today (UTC).

        Returns
        -------
        str
            Absolute path to the saved file.
        """
        if report_date is None:
            report_date = datetime.now(tz=timezone.utc).date()

        Path(self._reports_dir).mkdir(parents=True, exist_ok=True)

        filename = f"{report_date.isoformat()}.md"
        filepath = os.path.join(self._reports_dir, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(report)

        abs_path = os.path.abspath(filepath)
        logger.info("Saved daily report to %s", abs_path)
        return abs_path
