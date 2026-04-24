#!/usr/bin/env python3
"""
Set, show, clear, or restore the Phase 2C reporting baseline.

The baseline is an additive marker stored in the ``system_state`` table of
the canonical DB (``user_data/claude_quant.db``).  It does not delete or
mutate any historical trade, decision, or cycle row.  Reporting code
interprets the baseline as a ``since=`` filter so the "current mainnet
reduced-live era" can be measured independently of pre-baseline history.

USAGE
-----
    # Show current baseline
    .venv/bin/python scripts/set_mainnet_baseline.py --show

    # Set a new baseline (required: --mode, --balance)
    .venv/bin/python scripts/set_mainnet_baseline.py \\
        --set \\
        --mode mainnet_reduced_live_v1 \\
        --balance 68.33 \\
        --notes "Phase 2B reduced-live cohort starts here"

    # Set with an explicit UTC start timestamp (must be timezone-aware ISO)
    .venv/bin/python scripts/set_mainnet_baseline.py \\
        --set --mode foo --balance 68.33 \\
        --started-at 2026-04-22T00:00:00+00:00

    # Clear current baseline (archives it for restore)
    .venv/bin/python scripts/set_mainnet_baseline.py --clear

    # Restore the previously-archived baseline
    .venv/bin/python scripts/set_mainnet_baseline.py --restore-previous

EXIT CODES
----------
    0 success, 1 validation error, 2 aborted by user, 3 no baseline found
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.data.database import BaselineRow, DatabaseManager  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Set / show / clear / restore the Phase 2C reporting baseline.",
    )
    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument("--set", dest="do_set", action="store_true",
                        help="Set a new baseline (requires --mode and --balance).")
    action.add_argument("--show", action="store_true",
                        help="Print the current baseline (and previous archive, if any).")
    action.add_argument("--clear", action="store_true",
                        help="Clear the current baseline; archives it for restore.")
    action.add_argument("--restore-previous", action="store_true",
                        help="Restore the most recently archived baseline.")

    p.add_argument("--mode", default=None,
                   help="Free-form mode label (e.g. mainnet_reduced_live_v1).")
    p.add_argument("--balance", default=None,
                   help="Start balance in USDT (decimal string).")
    p.add_argument("--notes", default="",
                   help="Optional audit note.")
    p.add_argument("--started-at", default=None,
                   help="UTC ISO-8601 start timestamp (default: now UTC).")
    p.add_argument("--db", default=None,
                   help="Path to claude_quant.db (default: user_data/claude_quant.db).")
    p.add_argument("--yes", action="store_true",
                   help="Skip interactive confirmation.")
    return p.parse_args()


def _open_db(db_arg: str | None) -> DatabaseManager:
    db_path = Path(db_arg) if db_arg else PROJECT_ROOT / "user_data" / "claude_quant.db"
    return DatabaseManager(db_path)


def _print_baseline(db: DatabaseManager) -> None:
    current = db.get_baseline()
    if current is None:
        print("Current baseline: NONE")
    else:
        print("Current baseline:")
        print(f"  current_mode:       {current.current_mode}")
        print(f"  started_at_utc:     {current.started_at_utc.isoformat()}")
        print(f"  start_balance_usdt: {current.start_balance_usdt}")
        print(f"  notes:              {current.notes!r}")
        print(f"  set_at_utc:         {current.set_at_utc.isoformat()}")
    previous = db.get_previous_baseline()
    if previous is not None:
        print("\nPrevious archive:")
        for k, v in previous.items():
            print(f"  {k}: {v}")


def _do_set(db: DatabaseManager, args: argparse.Namespace) -> int:
    if args.mode is None or args.balance is None:
        print("ERROR: --set requires both --mode and --balance.", file=sys.stderr)
        return 1

    try:
        balance = Decimal(args.balance)
    except InvalidOperation:
        print(f"ERROR: --balance must be a decimal, got {args.balance!r}", file=sys.stderr)
        return 1
    if balance <= Decimal("0"):
        print("ERROR: --balance must be positive.", file=sys.stderr)
        return 1

    if args.started_at is not None:
        try:
            started_at = datetime.fromisoformat(args.started_at)
        except ValueError:
            print(f"ERROR: --started-at must be ISO-8601, got {args.started_at!r}",
                  file=sys.stderr)
            return 1
        if started_at.tzinfo is None:
            print("ERROR: --started-at must be timezone-aware (include +00:00).",
                  file=sys.stderr)
            return 1
        started_at = started_at.astimezone(timezone.utc)
    else:
        started_at = datetime.now(timezone.utc)

    existing = db.get_baseline()
    print("About to set baseline:")
    print(f"  mode:           {args.mode}")
    print(f"  started_at_utc: {started_at.isoformat()}")
    print(f"  start_balance:  {balance} USDT")
    print(f"  notes:          {args.notes!r}")
    if existing is not None:
        print("\nExisting baseline WILL be archived to 'baseline.previous_json':")
        print(f"  (was) mode:       {existing.current_mode}")
        print(f"  (was) started:    {existing.started_at_utc.isoformat()}")
        print(f"  (was) balance:    {existing.start_balance_usdt} USDT")

    if not args.yes:
        resp = input("\nProceed? [y/N] ").strip().lower()
        if resp not in ("y", "yes"):
            print("Aborted.")
            return 2

    row = BaselineRow(
        current_mode=args.mode,
        started_at_utc=started_at,
        start_balance_usdt=balance,
        notes=args.notes,
    )
    db.set_baseline(row)
    print("OK — baseline set.")
    _print_baseline(db)
    return 0


def _do_clear(db: DatabaseManager, args: argparse.Namespace) -> int:
    existing = db.get_baseline()
    if existing is None:
        print("No baseline set; nothing to clear.")
        return 3
    print("About to CLEAR current baseline (will be archived for restore):")
    print(f"  mode:           {existing.current_mode}")
    print(f"  started_at_utc: {existing.started_at_utc.isoformat()}")
    if not args.yes:
        resp = input("\nProceed? [y/N] ").strip().lower()
        if resp not in ("y", "yes"):
            print("Aborted.")
            return 2
    db.clear_baseline()
    print("OK — baseline cleared.")
    return 0


def _do_restore(db: DatabaseManager, args: argparse.Namespace) -> int:
    prev = db.get_previous_baseline()
    if prev is None:
        print("No archived baseline to restore.")
        return 3
    print("About to RESTORE archived baseline as current:")
    for k, v in prev.items():
        print(f"  {k}: {v}")
    if not args.yes:
        resp = input("\nProceed? [y/N] ").strip().lower()
        if resp not in ("y", "yes"):
            print("Aborted.")
            return 2
    ok = db.restore_previous_baseline()
    if not ok:
        print("ERROR: restore failed (corrupt archive?).", file=sys.stderr)
        return 1
    print("OK — baseline restored.")
    _print_baseline(db)
    return 0


def main() -> int:
    args = _parse_args()
    db = _open_db(args.db)

    if args.show:
        _print_baseline(db)
        return 0
    if args.do_set:
        return _do_set(db, args)
    if args.clear:
        return _do_clear(db, args)
    if args.restore_previous:
        return _do_restore(db, args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
