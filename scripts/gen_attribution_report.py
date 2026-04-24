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
    p.add_argument(
        "--since-baseline",
        action="store_true",
        help="Filter the report to rows at or after the current baseline "
             "(see scripts/set_mainnet_baseline.py). Adds a -since-baseline "
             "suffix to the output filename.",
    )
    p.add_argument(
        "--since",
        default=None,
        help="Filter to rows at or after this UTC ISO-8601 timestamp. "
             "Mutually exclusive with --since-baseline.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if args.since_baseline and args.since:
        print("Error: --since-baseline and --since are mutually exclusive.", file=sys.stderr)
        sys.exit(1)

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

    since = None
    baseline_meta: dict | None = None
    if args.since_baseline:
        baseline = db.get_baseline()
        if baseline is None:
            print(
                "Error: --since-baseline requested but no baseline is set. "
                "Run scripts/set_mainnet_baseline.py --set first.",
                file=sys.stderr,
            )
            sys.exit(1)
        since = baseline.started_at_utc
        baseline_meta = {
            "current_mode": baseline.current_mode,
            "start_balance_usdt": str(baseline.start_balance_usdt),
            "notes": baseline.notes,
        }
    elif args.since:
        try:
            parsed = datetime.fromisoformat(args.since)
        except ValueError:
            print(f"Error: --since must be ISO-8601, got {args.since!r}", file=sys.stderr)
            sys.exit(1)
        if parsed.tzinfo is None:
            print("Error: --since must be timezone-aware (include +00:00).", file=sys.stderr)
            sys.exit(1)
        since = parsed.astimezone(timezone.utc)

    if args.stdout:
        content = reporter.build_content(report_date, since=since, baseline_meta=baseline_meta)
        print(content)
    else:
        output_path = reporter.generate(
            report_date, since=since, baseline_meta=baseline_meta
        )
        print(f"Report written to: {output_path}")


if __name__ == "__main__":
    main()
