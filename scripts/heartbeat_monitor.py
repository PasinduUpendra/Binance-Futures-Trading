#!/usr/bin/env python3
"""Stale-cycle detector for the Claude Quant bot.

Polls ``cycle_history`` in the canonical SQLite DB and, if the newest row
is older than ``STALE_MINUTES`` minutes (default 90 — 3× the 30-minute
cycle interval), writes a WARNING line to ``user_data/logs/heartbeat.log``
and exits non-zero so launchd / cron can surface the alert.

Design tenets
-------------
- Read-only. This script never writes to any DB table.
- Fast. Opens one SQLite connection, runs one SELECT, returns.
- Deterministic exit codes:
    0  → fresh cycle, no alert
    2  → stale cycle, alert emitted
    3  → DB missing / unreadable (treat as stale-equivalent)
- No external deps beyond the stdlib — safe to run even if the venv is
  broken.

Usage
-----
    python3 scripts/heartbeat_monitor.py                 # default 90 min
    python3 scripts/heartbeat_monitor.py --minutes 60    # custom threshold
    python3 scripts/heartbeat_monitor.py --quiet         # suppress stdout
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "user_data" / "claude_quant.db"
LOG_PATH = PROJECT_ROOT / "user_data" / "logs" / "heartbeat.log"

EXIT_FRESH = 0
EXIT_STALE = 2
EXIT_DB_MISSING = 3


def _emit(msg: str, quiet: bool) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = f"{stamp} {msg}\n"
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line)
    if not quiet:
        sys.stderr.write(line)


def _fetch_last_cycle_ts(db_path: Path) -> datetime | None:
    """Return the timestamp of the most recent cycle or None if empty."""
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT MAX(timestamp) FROM cycle_history"
        ).fetchone()
    except sqlite3.OperationalError:
        conn.close()
        return None
    conn.close()
    if row is None or row[0] is None:
        return None
    try:
        return datetime.fromisoformat(row[0])
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--minutes", type=int, default=90,
        help="Alert threshold in minutes (default 90).",
    )
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress stderr output.")
    parser.add_argument(
        "--db", type=Path, default=DB_PATH,
        help="Override canonical DB path (testing only).",
    )
    args = parser.parse_args()

    last = _fetch_last_cycle_ts(args.db)
    if last is None:
        _emit(
            f"HEARTBEAT_DB_MISSING path={args.db} — cannot read cycle_history",
            args.quiet,
        )
        return EXIT_DB_MISSING

    age = datetime.now(timezone.utc) - last
    if age > timedelta(minutes=args.minutes):
        _emit(
            f"HEARTBEAT_STALE last_cycle={last.isoformat()} "
            f"age_min={age.total_seconds()/60:.1f} threshold_min={args.minutes}",
            args.quiet,
        )
        return EXIT_STALE

    if not args.quiet:
        print(
            f"OK last_cycle={last.isoformat()} "
            f"age_min={age.total_seconds()/60:.1f}"
        )
    return EXIT_FRESH


if __name__ == "__main__":
    sys.exit(main())
