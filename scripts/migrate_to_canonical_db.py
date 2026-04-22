#!/usr/bin/env python3
"""
One-shot, idempotent migration to the canonical single DB.

Merges all fragmented persistence into ``user_data/claude_quant.db``:
    1. Legacy ``user_data/agent_state/trade_journal.db``  → ``trades`` table
    2. Legacy ``user_data/audit_trail.db``                 → ``audit_trail`` table
    3. Legacy JSON state files                             → ``system_state`` / ``trailing_stops`` tables

After a successful migration the legacy files are MOVED (not deleted) to
``user_data/agent_state/archive/`` with a timestamp suffix, so forensic
recovery is still possible. Re-running the script is a no-op.

Usage
-----
    python scripts/migrate_to_canonical_db.py             # apply
    python scripts/migrate_to_canonical_db.py --dry-run   # preview only
    python scripts/migrate_to_canonical_db.py --no-archive # leave files in place
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.data.database import DatabaseManager  # noqa: E402


LEGACY_TRADE_JOURNAL = PROJECT_ROOT / "user_data" / "agent_state" / "trade_journal.db"
LEGACY_AUDIT_DB = PROJECT_ROOT / "user_data" / "audit_trail.db"
LEGACY_DAILY_STATE = PROJECT_ROOT / "user_data" / "agent_state" / "daily_state.json"
LEGACY_DRAWDOWN_STATE = PROJECT_ROOT / "user_data" / "agent_state" / "drawdown_state.json"
LEGACY_TRAILING_STOPS = PROJECT_ROOT / "user_data" / "agent_state" / "trailing_stops.json"
ARCHIVE_DIR = PROJECT_ROOT / "user_data" / "agent_state" / "archive"


def _archive(src: Path, dry_run: bool) -> None:
    """Move a legacy file (plus any -wal / -shm sidecars) into archive dir."""
    if not src.exists():
        return
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    for path in [src, src.with_suffix(src.suffix + "-wal"),
                 src.with_suffix(src.suffix + "-shm")]:
        if path.exists():
            dst = ARCHIVE_DIR / f"{path.name}.{stamp}"
            if dry_run:
                print(f"  [dry-run] would archive {path} → {dst}")
            else:
                path.rename(dst)
                print(f"  archived {path.name} → {dst.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview the migration, do not write anything.")
    parser.add_argument("--no-archive", action="store_true",
                        help="Do not move legacy files to archive after migration.")
    args = parser.parse_args()

    print("== Canonical-DB Migration ==")
    print(f"Dry run: {args.dry_run}")

    db = DatabaseManager()
    print(f"Canonical DB: {db.db_path}")

    # 1. Trade journal
    if LEGACY_TRADE_JOURNAL.exists():
        print(f"\n[1] Legacy trade journal found: {LEGACY_TRADE_JOURNAL}")
        if args.dry_run:
            import sqlite3
            c = sqlite3.connect(str(LEGACY_TRADE_JOURNAL))
            try:
                n = c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            except sqlite3.OperationalError:
                n = 0
            c.close()
            print(f"  [dry-run] would import up to {n} trades (INSERT OR IGNORE).")
        else:
            n = db.migrate_from_trade_journal(LEGACY_TRADE_JOURNAL)
            print(f"  imported {n} trade rows.")
    else:
        print("\n[1] Legacy trade journal absent — skipping.")

    # 2. Audit trail DB
    if LEGACY_AUDIT_DB.exists():
        print(f"\n[2] Legacy audit DB found: {LEGACY_AUDIT_DB}")
        if args.dry_run:
            import sqlite3
            c = sqlite3.connect(str(LEGACY_AUDIT_DB))
            try:
                n = c.execute("SELECT COUNT(*) FROM audit_trail").fetchone()[0]
            except sqlite3.OperationalError:
                n = 0
            c.close()
            print(f"  [dry-run] would import up to {n} audit rows.")
        else:
            n = db.migrate_from_audit_trail(LEGACY_AUDIT_DB)
            print(f"  imported {n} audit rows.")
    else:
        print("\n[2] Legacy audit DB absent — skipping.")

    # 3. JSON state
    if LEGACY_DRAWDOWN_STATE.exists():
        print(f"\n[3a] Legacy drawdown state JSON found.")
        if not args.dry_run:
            ok = db.migrate_drawdown_state(LEGACY_DRAWDOWN_STATE)
            print(f"  migrated: {ok}")
    if LEGACY_DAILY_STATE.exists():
        print(f"\n[3b] Legacy daily state JSON found.")
        if not args.dry_run:
            ok = db.migrate_daily_state(LEGACY_DAILY_STATE)
            print(f"  migrated: {ok}")
    if LEGACY_TRAILING_STOPS.exists():
        print(f"\n[3c] Legacy trailing_stops JSON found.")
        if not args.dry_run:
            n = db.migrate_trailing_stops_json(LEGACY_TRAILING_STOPS)
            print(f"  migrated {n} trailing stops.")

    # 4. Archive legacy files
    if not args.no_archive:
        print("\n[4] Archiving legacy files …")
        for src in (LEGACY_TRADE_JOURNAL, LEGACY_AUDIT_DB,
                    LEGACY_DAILY_STATE, LEGACY_DRAWDOWN_STATE,
                    LEGACY_TRAILING_STOPS):
            _archive(src, dry_run=args.dry_run)
    else:
        print("\n[4] Skipping archive step (--no-archive).")

    # 5. Verification
    print("\n== Verification ==")
    conn = db._get_conn()  # noqa: SLF001 — CLI tool
    trade_count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    audit_count = conn.execute("SELECT COUNT(*) FROM audit_trail").fetchone()[0]
    trailing_count = conn.execute(
        "SELECT COUNT(*) FROM trailing_stops").fetchone()[0]
    state_count = conn.execute(
        "SELECT COUNT(*) FROM system_state").fetchone()[0]
    cycle_count = conn.execute(
        "SELECT COUNT(*) FROM cycle_history").fetchone()[0]
    print(f"  trades:          {trade_count}")
    print(f"  audit_trail:     {audit_count}")
    print(f"  trailing_stops:  {trailing_count}")
    print(f"  system_state:    {state_count}")
    print(f"  cycle_history:   {cycle_count}")
    db.close()

    print("\n== Migration complete ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
