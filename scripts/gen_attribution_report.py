#!/usr/bin/env python3
"""
Generate a daily attribution report.

Usage
-----
    # today's report
    .venv/bin/python scripts/gen_attribution_report.py

    # specific date
    .venv/bin/python scripts/gen_attribution_report.py --date 2026-04-22

    # print to stdout only (do not write file)
    .venv/bin/python scripts/gen_attribution_report.py --stdout

Output
------
    docs/reports/YYYY-MM-DD-attribution.md
"""

import argparse
import sys
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.data.database import DatabaseManager
from src.reporting.attribution_report import AttributionReporter


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate daily attribution report")
    p.add_argument(
        "--date",
        default=None,
        help="Report date in YYYY-MM-DD format (default: today UTC)",
    )
    p.add_argument(
        "--db",
        default=None,
        help="Path to claude_quant.db (default: user_data/claude_quant.db)",
    )
    p.add_argument(
        "--stdout",
        action="store_true",
        help="Print report to stdout instead of writing to docs/reports/",
    )
    p.add_argument(
        "--reports-dir",
        default=None,
        help="Override output directory for the report file",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # Parse date
    if args.date:
        try:
            report_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"Error: --date must be in YYYY-MM-DD format, got: {args.date!r}", file=sys.stderr)
            sys.exit(1)
    else:
        report_date = datetime.now(timezone.utc).date()

    # Resolve database path
    db_path = Path(args.db) if args.db else PROJECT_ROOT / "user_data" / "claude_quant.db"
    if not db_path.exists():
        print(f"Warning: database not found at {db_path} — report will contain empty sections", file=sys.stderr)

    # Build report
    db = DatabaseManager(db_path)
    reports_dir = Path(args.reports_dir) if args.reports_dir else None
    reporter = AttributionReporter(db, reports_dir=reports_dir)

    if args.stdout:
        content = reporter.build_content(report_date)
        print(content)
    else:
        output_path = reporter.generate(report_date)
        print(f"Report written to: {output_path}")


if __name__ == "__main__":
    main()
