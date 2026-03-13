"""
Live terminal dashboard using the ``rich`` library.

Renders a real-time overview of the trading system's status, positions,
recent trades, strategy performance, and market regime.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from src.reporting.daily_pnl import CumulativeStats, DailyPnL

logger = logging.getLogger("claude_quant.reporting.dashboard")


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------


def _pnl_color(value: Decimal) -> str:
    """Return 'green', 'red', or 'white' depending on sign."""
    if value > Decimal("0"):
        return "green"
    elif value < Decimal("0"):
        return "red"
    return "white"


def _circuit_breaker_color(level: str) -> str:
    """Map circuit breaker level to rich colour."""
    return {
        "GREEN": "green",
        "YELLOW": "yellow",
        "RED": "red",
        "DEAD": "bright_red",
    }.get(level.upper(), "white")


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


class Dashboard:
    """Terminal dashboard for Claude Quant.

    Parameters
    ----------
    console : Console | None
        Rich console to render to.  A new one is created if not provided.
    refresh_rate : int
        Frames per second for the live display.  Default 2.
    """

    def __init__(
        self,
        console: Console | None = None,
        refresh_rate: int = 2,
    ) -> None:
        self._console = console or Console()
        self._refresh_rate = refresh_rate

    # -- live display --------------------------------------------------------

    def display_live(
        self,
        state_provider: Any,
        stop_event: Any | None = None,
    ) -> None:
        """Render a continuously updating live dashboard.

        Parameters
        ----------
        state_provider
            An object (or callable) with the following attributes / methods
            that return the current system state:

            - ``get_balance()`` -> Decimal
            - ``get_daily_pnl()`` -> DailyPnL | None
            - ``get_circuit_breaker_level()`` -> str
            - ``get_open_positions()`` -> list[dict]
            - ``get_recent_trades(limit=10)`` -> list[dict]
            - ``get_strategy_performance()`` -> dict[str, dict]
            - ``get_regime()`` -> dict (keys: regime, confidence)

        stop_event
            An ``asyncio.Event`` or ``threading.Event`` whose ``is_set()``
            method signals the dashboard to exit.  If ``None`` the dashboard
            runs until KeyboardInterrupt.
        """
        try:
            with Live(
                self._build_layout(state_provider),
                console=self._console,
                refresh_per_second=self._refresh_rate,
                screen=True,
            ) as live:
                while True:
                    if stop_event is not None and stop_event.is_set():
                        break
                    live.update(self._build_layout(state_provider))
        except KeyboardInterrupt:
            self._console.print("[bold]Dashboard stopped.[/bold]")

    # -- one-shot summary ----------------------------------------------------

    def display_summary(self, daily_pnl: DailyPnL) -> None:
        """Print a one-time daily summary to the console."""
        self._console.print()
        self._console.rule(f"[bold]Daily Summary — {daily_pnl.date}[/bold]")
        self._console.print()

        # Balance row
        color = _pnl_color(daily_pnl.net_pnl)
        self._console.print(
            f"  Start Balance : [bold]{daily_pnl.start_balance}[/bold] USDT"
        )
        self._console.print(
            f"  End Balance   : [bold]{daily_pnl.end_balance}[/bold] USDT"
        )
        self._console.print(
            f"  Net P&L       : [{color}][bold]{daily_pnl.net_pnl:+}[/bold] "
            f"({daily_pnl.pnl_pct:+.2f}%)[/{color}]"
        )
        self._console.print(
            f"  Realised      : {daily_pnl.realized_pnl:+}  |  "
            f"Unrealised : {daily_pnl.unrealized_pnl:+}  |  "
            f"Fees : {daily_pnl.fees}"
        )
        self._console.print(
            f"  Trades        : {daily_pnl.trades_count}  "
            f"(W:{daily_pnl.wins} / L:{daily_pnl.losses})"
        )
        if daily_pnl.strategies_used:
            self._console.print(
                f"  Strategies    : {', '.join(daily_pnl.strategies_used)}"
            )
        self._console.print()
        self._console.rule()

    # -- layout builder ------------------------------------------------------

    def _build_layout(self, sp: Any) -> Layout:
        """Compose the full dashboard layout from current state."""
        layout = Layout()

        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=3),
        )

        layout["body"].split_row(
            Layout(name="left", ratio=1),
            Layout(name="right", ratio=2),
        )

        layout["right"].split_column(
            Layout(name="positions", ratio=1),
            Layout(name="trades", ratio=1),
        )

        # Header
        layout["header"].update(self._header_panel(sp))

        # Left: balance + regime + strategy performance
        layout["left"].update(self._status_panel(sp))

        # Positions table
        layout["positions"].update(self._positions_panel(sp))

        # Recent trades
        layout["trades"].update(self._trades_panel(sp))

        # Footer
        now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        layout["footer"].update(
            Panel(Text(f"Last refresh: {now}", justify="center"), style="dim")
        )

        return layout

    # -- individual panels ---------------------------------------------------

    @staticmethod
    def _header_panel(sp: Any) -> Panel:
        title = Text("CLAUDE QUANT", style="bold cyan", justify="center")
        subtitle = Text(
            "Autonomous AI Trading System", style="dim", justify="center"
        )
        content = Text.assemble(title, "\n", subtitle)
        return Panel(content, style="bright_blue")

    @staticmethod
    def _status_panel(sp: Any) -> Panel:
        """Balance, P&L, circuit breaker, regime."""
        rows: list[str] = []

        # Balance
        try:
            balance = sp.get_balance()
            rows.append(f"[bold]Balance:[/bold] {balance} USDT")
        except Exception:
            rows.append("[bold]Balance:[/bold] N/A")

        # Daily P&L
        try:
            daily = sp.get_daily_pnl()
            if daily is not None:
                color = _pnl_color(daily.net_pnl)
                rows.append(
                    f"[bold]Daily P&L:[/bold] [{color}]{daily.net_pnl:+} "
                    f"({daily.pnl_pct:+.2f}%)[/{color}]"
                )
                rows.append(f"[bold]Trades:[/bold] {daily.trades_count} (W:{daily.wins}/L:{daily.losses})")
        except Exception:
            pass

        # Circuit breaker
        try:
            cb_level = sp.get_circuit_breaker_level()
            cb_color = _circuit_breaker_color(cb_level)
            rows.append(f"[bold]Circuit Breaker:[/bold] [{cb_color}]{cb_level}[/{cb_color}]")
        except Exception:
            rows.append("[bold]Circuit Breaker:[/bold] UNKNOWN")

        # Regime
        try:
            regime_info = sp.get_regime()
            regime_name = regime_info.get("regime", "unknown")
            regime_conf = regime_info.get("confidence", 0)
            rows.append(f"[bold]Regime:[/bold] {regime_name} ({regime_conf:.0f}%)")
        except Exception:
            pass

        # Strategy performance
        try:
            strat_perf = sp.get_strategy_performance()
            if strat_perf:
                rows.append("")
                rows.append("[bold underline]Strategy Performance[/bold underline]")
                for name, perf in strat_perf.items():
                    wr = perf.get("win_rate", 0)
                    pnl = perf.get("pnl", Decimal("0"))
                    color = _pnl_color(Decimal(str(pnl)))
                    rows.append(
                        f"  {name}: WR={wr:.0f}% [{color}]PnL={pnl:+}[/{color}]"
                    )
        except Exception:
            pass

        return Panel("\n".join(rows), title="Status", border_style="cyan")

    @staticmethod
    def _positions_panel(sp: Any) -> Panel:
        """Open positions table."""
        table = Table(
            title="Open Positions",
            expand=True,
            show_lines=False,
        )
        table.add_column("Symbol", style="bold")
        table.add_column("Dir", justify="center")
        table.add_column("Size", justify="right")
        table.add_column("Entry", justify="right")
        table.add_column("Current", justify="right")
        table.add_column("uPnL", justify="right")
        table.add_column("Lev", justify="center")

        try:
            positions = sp.get_open_positions()
            for pos in positions:
                upnl = Decimal(str(pos.get("unrealized_pnl", 0)))
                color = _pnl_color(upnl)
                direction = str(pos.get("direction", "")).upper()
                dir_color = "green" if direction == "LONG" else "red"

                table.add_row(
                    str(pos.get("symbol", "")),
                    f"[{dir_color}]{direction}[/{dir_color}]",
                    str(pos.get("size", "")),
                    str(pos.get("entry_price", "")),
                    str(pos.get("current_price", "")),
                    f"[{color}]{upnl:+}[/{color}]",
                    f"{pos.get('leverage', '')}x",
                )
        except Exception:
            pass

        if table.row_count == 0:
            table.add_row("—", "—", "—", "—", "—", "—", "—")

        return Panel(table, border_style="blue")

    @staticmethod
    def _trades_panel(sp: Any) -> Panel:
        """Recent trades table."""
        table = Table(
            title="Recent Trades (last 10)",
            expand=True,
            show_lines=False,
        )
        table.add_column("Time", style="dim")
        table.add_column("Symbol", style="bold")
        table.add_column("Dir", justify="center")
        table.add_column("Entry", justify="right")
        table.add_column("Exit", justify="right")
        table.add_column("PnL", justify="right")
        table.add_column("Strategy")

        try:
            trades = sp.get_recent_trades(limit=10)
            for trade in trades:
                pnl = Decimal(str(trade.get("pnl", 0)))
                color = _pnl_color(pnl)
                direction = str(trade.get("direction", "")).upper()
                dir_color = "green" if direction == "LONG" else "red"

                ts = trade.get("timestamp", "")
                if isinstance(ts, datetime):
                    ts = ts.strftime("%H:%M:%S")

                table.add_row(
                    str(ts),
                    str(trade.get("symbol", "")),
                    f"[{dir_color}]{direction}[/{dir_color}]",
                    str(trade.get("entry_price", "")),
                    str(trade.get("exit_price", "")),
                    f"[{color}]{pnl:+}[/{color}]",
                    str(trade.get("strategy", "")),
                )
        except Exception:
            pass

        if table.row_count == 0:
            table.add_row("—", "—", "—", "—", "—", "—", "—")

        return Panel(table, border_style="blue")
