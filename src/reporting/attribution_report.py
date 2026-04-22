"""
Daily attribution report generator — Phase 1C.

Generates ``docs/reports/YYYY-MM-DD-attribution.md`` from live database
data.  Each section maps to one or more of the 12 canonical forensic
queries defined in ``forensic_queries.py``.

The report is designed to be read by a human and also fed into the
``council-of-five`` agent as its weekly evidence bundle.

Usage (programmatic)
--------------------
    from datetime import date
    from pathlib import Path
    from src.data.database import DatabaseManager
    from src.reporting.attribution_report import AttributionReporter

    db = DatabaseManager()
    reporter = AttributionReporter(db)
    path = reporter.generate(date.today())
    print(f"Report written to {path}")

Usage (CLI)
-----------
    .venv/bin/python scripts/gen_attribution_report.py
    .venv/bin/python scripts/gen_attribution_report.py --date 2026-04-22
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.reporting.forensic_queries import ForensicQueries

if TYPE_CHECKING:
    from src.data.database import DatabaseManager

logger = logging.getLogger("claude_quant.reporting.attribution_report")

_DEFAULT_REPORTS_DIR = Path(__file__).parent.parent.parent / "docs" / "reports"


# ---------------------------------------------------------------------------
# Markdown helpers
# ---------------------------------------------------------------------------


def _fmt_float(val: Any, decimals: int = 4) -> str:
    """Format a float/None safely."""
    if val is None:
        return "n/a"
    try:
        return f"{float(val):.{decimals}f}"
    except (TypeError, ValueError):
        return str(val)


def _fmt_pct(val: Any, decimals: int = 1) -> str:
    """Format a 0–1 ratio as a percentage string."""
    if val is None:
        return "n/a"
    try:
        return f"{float(val) * 100:.{decimals}f}%"
    except (TypeError, ValueError):
        return str(val)


def _maker_label(val: Any) -> str:
    """Convert maker_entry integer to human-readable label."""
    try:
        return "maker" if int(val) == 1 else "taker"
    except (TypeError, ValueError):
        return str(val)


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a Markdown table."""
    if not rows:
        return "*(no data yet — requires instrumented live trades)*\n"
    separator = ["-" * max(len(h), 5) for h in headers]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(separator) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(lines) + "\n"


def _section(title: str, level: int = 2) -> str:
    return "#" * level + " " + title + "\n"


# ---------------------------------------------------------------------------
# AttributionReporter
# ---------------------------------------------------------------------------


class AttributionReporter:
    """Generates the daily attribution Markdown report.

    Parameters
    ----------
    db : DatabaseManager
        Canonical database (read-only access inside this class).
    reports_dir : Path | None
        Directory to write reports into.  Defaults to ``docs/reports/``.
    """

    def __init__(
        self,
        db: "DatabaseManager",
        reports_dir: Path | None = None,
    ) -> None:
        self._db = db
        self._fq = ForensicQueries(db)
        self._reports_dir = reports_dir or _DEFAULT_REPORTS_DIR

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(
        self,
        report_date: date | None = None,
        write: bool = True,
    ) -> Path:
        """Generate the attribution report for *report_date*.

        Parameters
        ----------
        report_date : date | None
            Date for the report.  Defaults to today (UTC).
        write : bool
            If True (default), write the file to *reports_dir*.

        Returns
        -------
        Path
            Path to the written (or would-be) report file.
        """
        if report_date is None:
            report_date = datetime.now(timezone.utc).date()

        content = self._build_report(report_date)
        filename = f"{report_date.isoformat()}-attribution.md"
        output_path = Path(self._reports_dir) / filename

        if write:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(content, encoding="utf-8")
            logger.info("Attribution report written to %s", output_path)

        return output_path

    def build_content(self, report_date: date | None = None) -> str:
        """Return the report markdown as a string without writing to disk."""
        if report_date is None:
            report_date = datetime.now(timezone.utc).date()
        return self._build_report(report_date)

    # ------------------------------------------------------------------
    # Internal build methods
    # ------------------------------------------------------------------

    def _build_report(self, report_date: date) -> str:
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines: list[str] = []

        lines.append(f"# Claude Quant Attribution Report — {report_date}")
        lines.append("")
        lines.append(f"*Generated: {generated_at}*")
        lines.append("")
        lines.append(
            "> **Note:** Performance figures in this report reflect observed live trades "
            "in the database only. No backtested figures are cited here. "
            "See `docs/CURRENT_STATE.md` for the verified runtime snapshot."
        )
        lines.append("")

        # Section 1: Funnel conversion
        lines.append(_section("1. Decision Funnel Conversion"))
        lines.append(self._funnel_section())

        # Section 2: Per-cascade expectancy
        lines.append(_section("2. Per-Cascade Level Expectancy"))
        lines.append(self._cascade_expectancy_section())

        # Section 3: Per-regime expectancy
        lines.append(_section("3. Per-Regime Expectancy"))
        lines.append(self._regime_expectancy_section())

        # Section 4: Fee / Funding Drag
        lines.append(_section("4. Fee and Funding Drag Summary"))
        lines.append(self._fee_funding_section())

        # Section 5: Rejection reasons
        lines.append(_section("5. Top Rejection Reasons"))
        lines.append(self._rejection_section())

        # Section 6: Maker vs Taker stats
        lines.append(_section("6. Maker vs Taker Execution Stats"))
        lines.append(self._maker_taker_section())

        # Section 7: Exit-reason mix
        lines.append(_section("7. Exit-Reason Mix"))
        lines.append(self._exit_reason_section())

        # Section 8: Confidence bucket win rates
        lines.append(_section("8. Confidence-Bucket Win Rates"))
        lines.append(self._confidence_bucket_section())

        # Section 9: Consensus adjustment impact
        lines.append(_section("9. Cross-Asset Consensus Adjustment Impact"))
        lines.append(self._consensus_adj_section())

        # Section 10: Cycle latency
        lines.append(_section("10. Cycle Completion Latency (last 14 days)"))
        lines.append(self._cycle_latency_section())

        # Footer
        lines.append("---")
        lines.append("")
        lines.append(
            "_Queries source: `src/reporting/forensic_queries.py` "
            "| Views: `cycle_funnel`, `v_cascade_expectancy`, `v_regime_expectancy`, "
            "`v_maker_taker_pnl`, `v_exit_reason_mix`, `v_symbol_pnl`, `v_confidence_bucket_wr`_"
        )

        return "\n".join(lines) + "\n"

    def _funnel_section(self) -> str:
        rows = self._fq.cascade_conversion_funnel()
        if not rows:
            return "*(no data yet — requires instrumented live trades)*\n\n"
        table_rows = [
            [r.get("stage", ""), r.get("cascade_level") or "—", r.get("outcome", ""), str(r.get("n", 0))]
            for r in rows
        ]
        return _md_table(["Stage", "Cascade Level", "Outcome", "Count"], table_rows) + "\n"

    def _cascade_expectancy_section(self) -> str:
        rows = self._fq.per_cascade_expectancy()
        if not rows:
            return "*(no data yet — cascade_level column requires Phase 1B instrumented trades)*\n\n"
        table_rows = [
            [
                r.get("cascade_level") or "—",
                str(r.get("n", 0)),
                _fmt_float(r.get("avg_pnl"), 4),
                _fmt_pct(r.get("win_rate")),
                _fmt_float(r.get("total_pnl"), 4),
            ]
            for r in rows
        ]
        return _md_table(["Cascade Level", "N", "Avg PnL (USDT)", "Win Rate", "Total PnL (USDT)"], table_rows) + "\n"

    def _regime_expectancy_section(self) -> str:
        rows = self._fq.per_regime_expectancy()
        if not rows:
            return "*(no data yet — regime_at_entry column requires Phase 1B instrumented trades)*\n\n"
        table_rows = [
            [
                r.get("regime_at_entry") or "—",
                str(r.get("n", 0)),
                _fmt_float(r.get("avg_pnl"), 4),
                _fmt_float(r.get("avg_fees"), 4),
                _fmt_float(r.get("avg_funding"), 4),
                _fmt_float(r.get("total_pnl"), 4),
            ]
            for r in rows
        ]
        return _md_table(
            ["Regime", "N", "Avg PnL", "Avg Fees", "Avg Funding", "Total PnL"],
            table_rows,
        ) + "\n"

    def _fee_funding_section(self) -> str:
        """Combines symbol-level fee/funding drag from Q8."""
        rows = self._fq.symbol_pnl_with_drag()
        if not rows:
            return "*(no data yet — requires closed trades with fees_usd / funding_usd populated)*\n\n"
        lines: list[str] = []
        total_fees = sum(float(r.get("total_fees") or 0) for r in rows)
        total_funding = sum(float(r.get("total_funding") or 0) for r in rows)
        total_pnl = sum(float(r.get("total_pnl") or 0) for r in rows)
        total_gross = sum(float(r.get("gross_edge_pnl") or 0) for r in rows)
        lines.append(f"- **Total realized PnL:** {total_pnl:.4f} USDT")
        lines.append(f"- **Total fees paid:** {total_fees:.4f} USDT")
        lines.append(f"- **Total funding paid/received:** {total_funding:.4f} USDT (negative = paid)")
        lines.append(f"- **Gross edge (PnL − fees + funding):** {total_gross:.4f} USDT")
        lines.append("")
        table_rows = [
            [
                r.get("symbol", ""),
                str(r.get("n", 0)),
                _fmt_float(r.get("total_pnl"), 4),
                _fmt_float(r.get("total_fees"), 4),
                _fmt_float(r.get("total_funding"), 4),
                _fmt_float(r.get("gross_edge_pnl"), 4),
            ]
            for r in rows
        ]
        lines.append(_md_table(
            ["Symbol", "N", "Total PnL", "Total Fees", "Total Funding", "Gross Edge"],
            table_rows,
        ))
        return "\n".join(lines) + "\n"

    def _rejection_section(self) -> str:
        rows = self._fq.rejection_distribution()
        if not rows:
            return "*(no data yet — requires decision_log rows with outcome='reject')*\n\n"
        table_rows = [
            [r.get("stage", ""), r.get("reason") or "—", str(r.get("n", 0))]
            for r in rows
        ]
        return _md_table(["Stage", "Reason", "Count"], table_rows) + "\n"

    def _maker_taker_section(self) -> str:
        rows = self._fq.maker_taker_pnl()
        if not rows:
            return "*(no data yet — requires maker_entry column populated)*\n\n"
        # Also include slippage
        slippage_rows = self._fq.slippage_cost()
        slip_line = ""
        if slippage_rows:
            s = slippage_rows[0]
            slip_line = (
                f"\n**Slippage:** avg entry {_fmt_float(s.get('avg_entry_slippage_bps'), 2)} bps, "
                f"avg exit {_fmt_float(s.get('avg_exit_slippage_bps'), 2)} bps, "
                f"round-trip {_fmt_float(s.get('avg_roundtrip_slippage_bps'), 2)} bps "
                f"(n={s.get('n', 0)} trades)\n"
            )
        table_rows = [
            [
                _maker_label(r.get("maker_entry")),
                str(r.get("n", 0)),
                _fmt_float(r.get("avg_gross_pnl"), 4),
                _fmt_float(r.get("avg_fees"), 4),
                _fmt_float(r.get("avg_net_pnl"), 4),
            ]
            for r in rows
        ]
        table = _md_table(
            ["Entry Type", "N", "Avg Gross PnL", "Avg Fees", "Avg Net PnL"],
            table_rows,
        )
        return table + slip_line + "\n"

    def _exit_reason_section(self) -> str:
        rows = self._fq.exit_reason_mix()
        if not rows:
            return "*(no data yet — requires exit_reason_enum populated on closed trades)*\n\n"
        table_rows = [
            [
                r.get("exit_reason_enum") or "—",
                str(r.get("n", 0)),
                _fmt_float(r.get("avg_pnl"), 4),
                _fmt_pct(r.get("win_rate")),
                _fmt_float(r.get("total_pnl"), 4),
            ]
            for r in rows
        ]
        return _md_table(
            ["Exit Reason", "N", "Avg PnL", "Win Rate", "Total PnL"],
            table_rows,
        ) + "\n"

    def _confidence_bucket_section(self) -> str:
        rows = self._fq.confidence_bucket_win_rate()
        if not rows:
            return "*(no data yet — requires confidence_bucket populated)*\n\n"
        table_rows = [
            [
                r.get("confidence_bucket") or "—",
                str(r.get("n", 0)),
                _fmt_pct(r.get("win_rate")),
                _fmt_float(r.get("avg_pnl"), 4),
                _fmt_float(r.get("total_pnl"), 4),
            ]
            for r in rows
        ]
        return _md_table(
            ["Bucket", "N", "Win Rate", "Avg PnL", "Total PnL"],
            table_rows,
        ) + "\n"

    def _consensus_adj_section(self) -> str:
        rows = self._fq.consensus_adj_impact()
        if not rows:
            return "*(no data yet — requires consensus_adj populated)*\n\n"
        # Also funding filter
        ff_rows = self._fq.funding_filter_impact()
        ff_line = ""
        if ff_rows:
            ff_dict = {r.get("outcome", "unknown"): r.get("n", 0) for r in ff_rows}
            total_ff = sum(ff_dict.values())
            rejected = ff_dict.get("reject", 0)
            ff_line = (
                f"\n**Funding filter:** {rejected}/{total_ff} signals rejected "
                f"({_fmt_pct(rejected / total_ff if total_ff else 0)})\n"
            )
        table_rows = [
            [r.get("bucket", ""), str(r.get("n", 0)), _fmt_float(r.get("avg_pnl"), 4), _fmt_float(r.get("total_pnl"), 4)]
            for r in rows
        ]
        table = _md_table(["Bucket", "N", "Avg PnL", "Total PnL"], table_rows)
        return table + ff_line + "\n"

    def _cycle_latency_section(self) -> str:
        rows = self._fq.cycle_latency()
        if not rows:
            return "*(no data yet — requires cycle_history rows)*\n\n"
        table_rows = [
            [
                r.get("d", ""),
                _fmt_float(r.get("avg_seconds"), 1),
                _fmt_float(r.get("max_seconds"), 1),
                str(r.get("cycle_count", 0)),
            ]
            for r in rows
        ]
        return _md_table(["Date", "Avg Sec", "Max Sec", "Cycles"], table_rows) + "\n"
