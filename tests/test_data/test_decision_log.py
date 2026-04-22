"""Tests for decision_log schema, begin_cycle/finish_cycle, and cycle_funnel view."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from src.data.database import CycleHistoryRow, DatabaseManager


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    return DatabaseManager(db_path=tmp_path / "test_decision_log.db")


@pytest.fixture
def sample_cycle_row() -> CycleHistoryRow:
    return CycleHistoryRow(
        cycle_number=1,
        timestamp=datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc),
        circuit_breaker_level="GREEN",
        balance=Decimal("68.33"),
        regime="TRENDING",
        signal_generated=True,
        trade_placed=False,
        trade_details=None,
        positions_closed="[]",
        errors="[]",
        duration_seconds=1.23,
    )


# ─── Table creation ──────────────────────────────────────────────────────────


class TestDecisionLogTable:
    def test_decision_log_table_exists(self, db: DatabaseManager) -> None:
        conn = sqlite3.connect(db._db_path)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "decision_log" in tables

    def test_decision_log_has_required_columns(self, db: DatabaseManager) -> None:
        conn = sqlite3.connect(db._db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(decision_log)").fetchall()}
        conn.close()
        required = {
            "id", "cycle_id", "timestamp_utc", "symbol", "stage",
            "outcome", "reason", "numeric_context", "cascade_level",
            "confidence", "regime",
        }
        assert required.issubset(cols)

    def test_decision_log_indexes_exist(self, db: DatabaseManager) -> None:
        conn = sqlite3.connect(db._db_path)
        idx = {r[1] for r in conn.execute(
            "SELECT type, name FROM sqlite_master WHERE type='index'"
        ).fetchall()}
        conn.close()
        assert "idx_decision_log_cycle" in idx
        assert "idx_decision_log_symbol_stage" in idx
        assert "idx_decision_log_timestamp" in idx

    def test_cycle_funnel_view_exists(self, db: DatabaseManager) -> None:
        conn = sqlite3.connect(db._db_path)
        views = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view'"
        ).fetchall()}
        conn.close()
        assert "cycle_funnel" in views


# ─── insert_decision_log ─────────────────────────────────────────────────────


class TestInsertDecisionLog:
    def test_basic_insert_and_retrieve(self, db: DatabaseManager) -> None:
        cycle_id = db.begin_cycle(1, datetime.now(timezone.utc))
        db.insert_decision_log(
            cycle_id=cycle_id,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            symbol="BTC/USDT:USDT",
            stage="regime_detect",
            outcome="pass",
            reason="trending",
        )
        rows = db.get_decision_logs(cycle_id=cycle_id)
        assert len(rows) == 1
        assert rows[0]["symbol"] == "BTC/USDT:USDT"
        assert rows[0]["stage"] == "regime_detect"
        assert rows[0]["outcome"] == "pass"

    def test_retrieve_by_symbol(self, db: DatabaseManager) -> None:
        cycle_id = db.begin_cycle(1, datetime.now(timezone.utc))
        db.insert_decision_log(cycle_id, datetime.now(timezone.utc).isoformat(),
                                "ETH/USDT:USDT", "signal_generate", "pass")
        db.insert_decision_log(cycle_id, datetime.now(timezone.utc).isoformat(),
                                "SOL/USDT:USDT", "signal_generate", "reject")
        rows = db.get_decision_logs(symbol="ETH/USDT:USDT")
        assert all(r["symbol"] == "ETH/USDT:USDT" for r in rows)
        assert len(rows) == 1

    def test_retrieve_by_stage(self, db: DatabaseManager) -> None:
        cycle_id = db.begin_cycle(1, datetime.now(timezone.utc))
        db.insert_decision_log(cycle_id, datetime.now(timezone.utc).isoformat(),
                                "BTC/USDT:USDT", "funding_filter", "pass")
        db.insert_decision_log(cycle_id, datetime.now(timezone.utc).isoformat(),
                                "BTC/USDT:USDT", "leverage_determine", "pass")
        rows = db.get_decision_logs(stage="funding_filter")
        assert all(r["stage"] == "funding_filter" for r in rows)

    def test_numeric_context_stored_as_json(self, db: DatabaseManager) -> None:
        import json

        cycle_id = db.begin_cycle(1, datetime.now(timezone.utc))
        ctx = {"confidence": 72.5, "adx": 23.1}
        db.insert_decision_log(
            cycle_id=cycle_id,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            symbol="ETH/USDT:USDT",
            stage="regime_detect",
            outcome="pass",
            numeric_context=ctx,
        )
        rows = db.get_decision_logs(cycle_id=cycle_id)
        assert len(rows) == 1
        stored = json.loads(rows[0]["numeric_context"])
        assert stored["confidence"] == pytest.approx(72.5)

    def test_null_optional_fields_allowed(self, db: DatabaseManager) -> None:
        cycle_id = db.begin_cycle(1, datetime.now(timezone.utc))
        db.insert_decision_log(
            cycle_id=cycle_id,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            symbol="XRP/USDT:USDT",
            stage="data_fetch_4h",
            outcome="pass",
        )
        rows = db.get_decision_logs(cycle_id=cycle_id)
        assert rows[0]["reason"] is None
        assert rows[0]["numeric_context"] is None
        assert rows[0]["cascade_level"] is None
        assert rows[0]["confidence"] is None
        assert rows[0]["regime"] is None

    def test_limit_respected(self, db: DatabaseManager) -> None:
        cycle_id = db.begin_cycle(1, datetime.now(timezone.utc))
        for i in range(20):
            db.insert_decision_log(cycle_id, datetime.now(timezone.utc).isoformat(),
                                    "BTC/USDT:USDT", "data_fetch_4h", "pass")
        rows = db.get_decision_logs(limit=5)
        assert len(rows) <= 5


# ─── begin_cycle / finish_cycle ──────────────────────────────────────────────


class TestBeginFinishCycle:
    def test_begin_cycle_returns_positive_int(self, db: DatabaseManager) -> None:
        ts = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
        cycle_id = db.begin_cycle(cycle_number=1, timestamp=ts)
        assert isinstance(cycle_id, int)
        assert cycle_id > 0

    def test_begin_cycle_placeholder_has_pending_level(self, db: DatabaseManager) -> None:
        ts = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
        cycle_id = db.begin_cycle(cycle_number=1, timestamp=ts)
        conn = sqlite3.connect(db._db_path)
        row = conn.execute(
            "SELECT circuit_breaker_level FROM cycle_history WHERE id=?", (cycle_id,)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "PENDING"

    def test_finish_cycle_updates_row(
        self, db: DatabaseManager, sample_cycle_row: CycleHistoryRow
    ) -> None:
        ts = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
        cycle_id = db.begin_cycle(cycle_number=1, timestamp=ts)
        db.finish_cycle(cycle_id=cycle_id, cycle=sample_cycle_row)

        conn = sqlite3.connect(db._db_path)
        row = conn.execute(
            "SELECT circuit_breaker_level, regime FROM cycle_history WHERE id=?",
            (cycle_id,),
        ).fetchone()
        conn.close()
        assert row[0] == "GREEN"
        assert row[1] == "TRENDING"

    def test_finish_cycle_does_not_create_duplicate(
        self, db: DatabaseManager, sample_cycle_row: CycleHistoryRow
    ) -> None:
        ts = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
        cycle_id = db.begin_cycle(cycle_number=1, timestamp=ts)
        db.finish_cycle(cycle_id=cycle_id, cycle=sample_cycle_row)

        conn = sqlite3.connect(db._db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM cycle_history WHERE id=?", (cycle_id,)
        ).fetchone()[0]
        conn.close()
        assert count == 1

    def test_decision_log_fk_links_to_cycle(self, db: DatabaseManager) -> None:
        ts = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
        cycle_id = db.begin_cycle(cycle_number=1, timestamp=ts)
        db.insert_decision_log(
            cycle_id=cycle_id,
            timestamp_utc=ts.isoformat(),
            symbol="BTC/USDT:USDT",
            stage="data_fetch_4h",
            outcome="pass",
        )
        rows = db.get_decision_logs(cycle_id=cycle_id)
        assert rows[0]["cycle_id"] == cycle_id

    def test_multiple_cycles_have_distinct_ids(self, db: DatabaseManager) -> None:
        ts = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
        id1 = db.begin_cycle(cycle_number=1, timestamp=ts)
        id2 = db.begin_cycle(cycle_number=2, timestamp=ts)
        assert id1 != id2


# ─── cycle_funnel view ───────────────────────────────────────────────────────


class TestCycleFunnelView:
    def test_view_aggregates_correctly(self, db: DatabaseManager) -> None:
        ts = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
        cycle_id = db.begin_cycle(cycle_number=1, timestamp=ts)
        db.insert_decision_log(cycle_id, ts.isoformat(), "BTC/USDT:USDT", "regime_detect", "pass")
        db.insert_decision_log(cycle_id, ts.isoformat(), "ETH/USDT:USDT", "regime_detect", "pass")
        db.insert_decision_log(cycle_id, ts.isoformat(), "SOL/USDT:USDT", "regime_detect", "reject")

        conn = sqlite3.connect(db._db_path)
        row = conn.execute(
            "SELECT attempts, passes, rejects, errors "
            "FROM cycle_funnel WHERE cycle_id=? AND stage='regime_detect'",
            (cycle_id,),
        ).fetchone()
        conn.close()
        assert row is not None
        attempts, passes, rejects, errors = row
        assert attempts == 3
        assert passes == 2
        assert rejects == 1
        assert errors == 0

    def test_view_counts_errors(self, db: DatabaseManager) -> None:
        ts = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
        cycle_id = db.begin_cycle(cycle_number=1, timestamp=ts)
        db.insert_decision_log(cycle_id, ts.isoformat(), "BTC/USDT:USDT", "data_fetch_4h", "error")

        conn = sqlite3.connect(db._db_path)
        row = conn.execute(
            "SELECT errors FROM cycle_funnel WHERE cycle_id=? AND stage='data_fetch_4h'",
            (cycle_id,),
        ).fetchone()
        conn.close()
        assert row[0] == 1
