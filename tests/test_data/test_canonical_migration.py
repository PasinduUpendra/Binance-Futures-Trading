"""End-to-end test of the canonical-DB migration.

We build a fake "legacy" directory tree with a trade_journal.db, an
audit_trail.db, and the three JSON state files, then verify that every
helper imports the data into a fresh canonical DB exactly once (idempotent).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.data.database import DatabaseManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed_legacy_trade_journal(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE trades (
            trade_id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_price TEXT NOT NULL,
            exit_price TEXT,
            size TEXT NOT NULL,
            leverage INTEGER NOT NULL DEFAULT 1,
            pnl TEXT,
            pnl_pct TEXT,
            strategy TEXT DEFAULT '',
            regime TEXT DEFAULT '',
            confidence TEXT DEFAULT '0',
            stop_loss TEXT,
            take_profit TEXT,
            duration REAL,
            fees TEXT DEFAULT '0',
            slippage TEXT DEFAULT '0',
            reasoning TEXT DEFAULT '',
            lessons TEXT DEFAULT '',
            mode TEXT DEFAULT '',
            signal_tag TEXT DEFAULT '',
            exit_reason TEXT DEFAULT ''
        );
    """)
    for i in range(3):
        conn.execute(
            "INSERT INTO trades (trade_id, timestamp, symbol, direction, "
            "entry_price, size) VALUES (?, ?, ?, ?, ?, ?)",
            (f"t{i}", "2026-04-22T10:00:00+00:00",
             "ETH/USDT:USDT", "long", "2000", "0.01"),
        )
    conn.commit()
    conn.close()


def _seed_legacy_audit(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE audit_trail (
            audit_id TEXT PRIMARY KEY,
            trade_id TEXT,
            timestamp TEXT NOT NULL,
            symbol TEXT,
            direction TEXT,
            strategy_name TEXT,
            regime TEXT,
            decision TEXT,
            report_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    for i in range(4):
        conn.execute(
            "INSERT INTO audit_trail (audit_id, trade_id, timestamp, "
            "symbol, decision, report_json) VALUES (?, ?, ?, ?, ?, ?)",
            (f"a{i}", f"t{i % 3}", "2026-04-22T10:00:00+00:00",
             "ETH/USDT:USDT", "EXECUTE", '{"ok":true}'),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def legacy(tmp_path: Path) -> dict[str, Path]:
    paths = {
        "trade_journal": tmp_path / "trade_journal.db",
        "audit": tmp_path / "audit_trail.db",
        "daily_state": tmp_path / "daily_state.json",
        "drawdown_state": tmp_path / "drawdown_state.json",
        "trailing_stops": tmp_path / "trailing_stops.json",
    }
    _seed_legacy_trade_journal(paths["trade_journal"])
    _seed_legacy_audit(paths["audit"])
    paths["daily_state"].write_text(json.dumps({
        "date": "2026-04-22",
        "start_of_day_balance": "68.33",
        "last_daily_report": "2026-04-21",
        "updated_at": "2026-04-22T00:05:00+00:00",
    }))
    paths["drawdown_state"].write_text(json.dumps({
        "peak_balance": "75.00",
        "current_balance": "68.33",
        "max_drawdown_pct": "0.089",
        "max_drawdown_balance": "68.33",
        "updated_at": "2026-04-22T00:05:00+00:00",
    }))
    paths["trailing_stops"].write_text(json.dumps({
        "ETH/USDT:USDT": {
            "symbol": "ETH/USDT:USDT",
            "direction": "long",
            "entry_price": 2000.0,
            "best_price": 2050.0,
            "atr_4h": 35.0,
            "activated": True,
            "strategy_name": "SupertrendTrend",
            "take_profit": 2070.0,
            "tp_pending": False,
        }
    }))
    return paths


# ---------------------------------------------------------------------------
# Migration tests
# ---------------------------------------------------------------------------


def test_trade_journal_migration_imports_all_rows(
    tmp_path: Path, legacy: dict[str, Path],
) -> None:
    db = DatabaseManager(db_path=tmp_path / "canon.db")
    n = db.migrate_from_trade_journal(legacy["trade_journal"])
    assert n == 3
    conn = db._get_conn()  # noqa: SLF001
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 3
    db.close()


def test_trade_journal_migration_is_idempotent(
    tmp_path: Path, legacy: dict[str, Path],
) -> None:
    db = DatabaseManager(db_path=tmp_path / "canon.db")
    db.migrate_from_trade_journal(legacy["trade_journal"])
    db.migrate_from_trade_journal(legacy["trade_journal"])  # second run
    conn = db._get_conn()  # noqa: SLF001
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 3
    db.close()


def test_audit_migration_imports_all_rows(
    tmp_path: Path, legacy: dict[str, Path],
) -> None:
    db = DatabaseManager(db_path=tmp_path / "canon.db")
    n = db.migrate_from_audit_trail(legacy["audit"])
    assert n == 4
    conn = db._get_conn()  # noqa: SLF001
    assert conn.execute("SELECT COUNT(*) FROM audit_trail").fetchone()[0] == 4
    db.close()


def test_audit_migration_is_idempotent(
    tmp_path: Path, legacy: dict[str, Path],
) -> None:
    db = DatabaseManager(db_path=tmp_path / "canon.db")
    db.migrate_from_audit_trail(legacy["audit"])
    db.migrate_from_audit_trail(legacy["audit"])
    conn = db._get_conn()  # noqa: SLF001
    assert conn.execute("SELECT COUNT(*) FROM audit_trail").fetchone()[0] == 4
    db.close()


def test_daily_state_migration_populates_system_state(
    tmp_path: Path, legacy: dict[str, Path],
) -> None:
    db = DatabaseManager(db_path=tmp_path / "canon.db")
    assert db.migrate_daily_state(legacy["daily_state"]) is True
    assert db.get_state("daily.date") == "2026-04-22"
    assert db.get_state("daily.start_of_day_balance") == "68.33"
    db.close()


def test_trailing_stops_json_migration_upserts_rows(
    tmp_path: Path, legacy: dict[str, Path],
) -> None:
    db = DatabaseManager(db_path=tmp_path / "canon.db")
    n = db.migrate_trailing_stops_json(legacy["trailing_stops"])
    assert n == 1
    stops = db.get_all_trailing_stops()
    assert "ETH/USDT:USDT" in stops
    assert stops["ETH/USDT:USDT"]["activated"] is True
    db.close()


def test_missing_legacy_files_are_handled_gracefully(tmp_path: Path) -> None:
    db = DatabaseManager(db_path=tmp_path / "canon.db")
    assert db.migrate_from_trade_journal(tmp_path / "nope.db") == 0
    assert db.migrate_from_audit_trail(tmp_path / "nope.db") == 0
    assert db.migrate_daily_state(tmp_path / "nope.json") is False
    assert db.migrate_trailing_stops_json(tmp_path / "nope.json") == 0
    assert db.migrate_drawdown_state(tmp_path / "nope.json") is False
    db.close()


def test_audit_trail_insert_and_query(tmp_path: Path) -> None:
    db = DatabaseManager(db_path=tmp_path / "canon.db")
    db.insert_audit(
        audit_id="aid-1",
        trade_id="tid-1",
        timestamp="2026-04-22T10:00:00+00:00",
        symbol="ETH/USDT:USDT",
        direction="long",
        strategy_name="SupertrendTrend",
        regime="TRENDING",
        decision="EXECUTE",
        report_json='{"ok":true}',
    )
    assert db.get_audit_by_trade("tid-1") == '{"ok":true}'
    assert db.get_recent_audits(limit=5) == ['{"ok":true}']
    db.close()
