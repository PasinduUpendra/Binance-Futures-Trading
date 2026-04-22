"""Tests for the stale-cycle heartbeat CLI."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "heartbeat_monitor.py"


def _seed_db(db_path: Path, last_ts: datetime | None) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE cycle_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_number INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            circuit_breaker_level TEXT NOT NULL,
            balance TEXT,
            regime TEXT,
            signal_generated INTEGER,
            trade_placed INTEGER,
            trade_details TEXT,
            positions_closed TEXT,
            errors TEXT,
            duration_seconds REAL
        )
    """)
    if last_ts is not None:
        conn.execute(
            "INSERT INTO cycle_history (cycle_number, timestamp, "
            "circuit_breaker_level, balance) VALUES (1, ?, 'GREEN', '100')",
            (last_ts.isoformat(),),
        )
    conn.commit()
    conn.close()


def _run(db: Path, minutes: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(db),
         "--minutes", str(minutes), "--quiet"],
        capture_output=True, text=True,
    )


def test_fresh_cycle_exits_zero(tmp_path: Path) -> None:
    db = tmp_path / "canon.db"
    _seed_db(db, datetime.now(timezone.utc) - timedelta(minutes=30))
    result = _run(db)
    assert result.returncode == 0


def test_stale_cycle_exits_two(tmp_path: Path) -> None:
    db = tmp_path / "canon.db"
    _seed_db(db, datetime.now(timezone.utc) - timedelta(minutes=200))
    result = _run(db)
    assert result.returncode == 2


def test_missing_db_exits_three(tmp_path: Path) -> None:
    db = tmp_path / "does_not_exist.db"
    result = _run(db)
    assert result.returncode == 3


def test_empty_cycle_history_exits_three(tmp_path: Path) -> None:
    db = tmp_path / "canon.db"
    _seed_db(db, last_ts=None)
    result = _run(db)
    assert result.returncode == 3


def test_custom_minutes_threshold(tmp_path: Path) -> None:
    db = tmp_path / "canon.db"
    _seed_db(db, datetime.now(timezone.utc) - timedelta(minutes=40))
    # 30-min threshold → 40-min age is stale
    assert _run(db, minutes=30).returncode == 2
    # 60-min threshold → 40-min age is fresh
    assert _run(db, minutes=60).returncode == 0
