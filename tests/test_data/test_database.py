"""Tests for DatabaseManager — consolidated SQLite persistence."""

import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from src.data.database import (
    CycleHistoryRow,
    DailyReportRow,
    DatabaseManager,
    StrategyMetricRow,
)


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    """Create a DatabaseManager backed by a temporary SQLite file."""
    return DatabaseManager(db_path=tmp_path / "test.db")


@pytest.fixture
def sample_daily_report() -> DailyReportRow:
    return DailyReportRow(
        report_date=date(2026, 3, 15),
        start_balance=Decimal("1000.00"),
        end_balance=Decimal("1010.50"),
        realized_pnl=Decimal("10.50"),
        unrealized_pnl=Decimal("0.00"),
        fees=Decimal("0.35"),
        net_pnl=Decimal("10.15"),
        pnl_pct=Decimal("1.015"),
        trades_count=3,
        wins=2,
        losses=1,
        strategies_used="SupertrendTrend",
        created_at=datetime(2026, 3, 15, 23, 59, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def sample_cycle() -> CycleHistoryRow:
    return CycleHistoryRow(
        cycle_number=42,
        timestamp=datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc),
        circuit_breaker_level="GREEN",
        balance=Decimal("1005.25"),
        regime="TRENDING",
        signal_generated=True,
        trade_placed=False,
        trade_details=None,
        positions_closed="[]",
        errors="[]",
        duration_seconds=2.45,
    )


@pytest.fixture
def sample_metrics() -> StrategyMetricRow:
    return StrategyMetricRow(
        strategy="SupertrendTrend",
        regime="TRENDING",
        total_trades=50,
        wins=30,
        losses=20,
        win_rate=Decimal("0.60"),
        avg_pnl=Decimal("1.25"),
        total_pnl=Decimal("62.50"),
        max_win=Decimal("15.00"),
        max_loss=Decimal("-8.50"),
        profit_factor=Decimal("1.85"),
        sharpe=2.3,
        last_updated=datetime(2026, 3, 15, 23, 0, 0, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


class TestDatabaseInit:
    def test_creates_db_file(self, tmp_path: Path) -> None:
        db_path = tmp_path / "subdir" / "test.db"
        DatabaseManager(db_path=db_path)
        assert db_path.exists()

    def test_wal_mode_enabled(self, db: DatabaseManager) -> None:
        conn = db._get_conn()
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

    def test_foreign_keys_on(self, db: DatabaseManager) -> None:
        conn = db._get_conn()
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1

    def test_tables_created(self, db: DatabaseManager) -> None:
        conn = db._get_conn()
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        expected = {"trades", "daily_reports", "cycle_history",
                    "system_state", "strategy_metrics"}
        assert expected.issubset(tables)

    def test_close_and_reopen(self, tmp_path: Path) -> None:
        path = tmp_path / "test.db"
        db = DatabaseManager(db_path=path)
        db.set_state("key1", "val1")
        db.close()
        # Reopen — data should persist
        db2 = DatabaseManager(db_path=path)
        assert db2.get_state("key1") == "val1"
        db2.close()


# ---------------------------------------------------------------------------
# Daily Reports CRUD
# ---------------------------------------------------------------------------


class TestDailyReports:
    def test_store_and_get(
        self, db: DatabaseManager, sample_daily_report: DailyReportRow
    ) -> None:
        db.store_daily_report(sample_daily_report)
        result = db.get_daily_report(date(2026, 3, 15))
        assert result is not None
        assert result.start_balance == Decimal("1000.00")
        assert result.end_balance == Decimal("1010.50")
        assert result.trades_count == 3

    def test_get_nonexistent_returns_none(self, db: DatabaseManager) -> None:
        assert db.get_daily_report(date(2099, 1, 1)) is None

    def test_upsert_replaces(
        self, db: DatabaseManager, sample_daily_report: DailyReportRow
    ) -> None:
        db.store_daily_report(sample_daily_report)
        updated = DailyReportRow(
            report_date=date(2026, 3, 15),
            start_balance=Decimal("1000.00"),
            end_balance=Decimal("1020.00"),
            realized_pnl=Decimal("20.00"),
            net_pnl=Decimal("19.50"),
            pnl_pct=Decimal("1.95"),
            trades_count=5,
            wins=4,
            losses=1,
            created_at=datetime(2026, 3, 16, 0, 0, 0, tzinfo=timezone.utc),
        )
        db.store_daily_report(updated)
        result = db.get_daily_report(date(2026, 3, 15))
        assert result is not None
        assert result.end_balance == Decimal("1020.00")
        assert result.trades_count == 5

    def test_get_all_daily_reports_ordered(self, db: DatabaseManager) -> None:
        for day_offset in [3, 1, 2]:
            db.store_daily_report(
                DailyReportRow(
                    report_date=date(2026, 3, day_offset),
                    start_balance=Decimal("100"),
                    end_balance=Decimal("101"),
                    created_at=datetime(2026, 3, day_offset, tzinfo=timezone.utc),
                )
            )
        reports = db.get_all_daily_reports()
        assert len(reports) == 3
        assert reports[0].report_date < reports[1].report_date < reports[2].report_date

    def test_decimal_precision_preserved(self, db: DatabaseManager) -> None:
        report = DailyReportRow(
            report_date=date(2026, 3, 15),
            start_balance=Decimal("68.33"),
            end_balance=Decimal("68.7614322"),
            realized_pnl=Decimal("0.4314322"),
            net_pnl=Decimal("0.4314322"),
            pnl_pct=Decimal("0.631472"),
            created_at=datetime(2026, 3, 15, tzinfo=timezone.utc),
        )
        db.store_daily_report(report)
        result = db.get_daily_report(date(2026, 3, 15))
        assert result is not None
        assert result.end_balance == Decimal("68.7614322")
        assert result.pnl_pct == Decimal("0.631472")


# ---------------------------------------------------------------------------
# Cycle History CRUD
# ---------------------------------------------------------------------------


class TestCycleHistory:
    def test_store_and_get_recent(
        self, db: DatabaseManager, sample_cycle: CycleHistoryRow
    ) -> None:
        db.store_cycle(sample_cycle)
        cycles = db.get_recent_cycles(n=5)
        assert len(cycles) == 1
        assert cycles[0].cycle_number == 42
        assert cycles[0].circuit_breaker_level == "GREEN"

    def test_recent_returns_newest_first(self, db: DatabaseManager) -> None:
        for i in range(5):
            db.store_cycle(
                CycleHistoryRow(
                    cycle_number=i,
                    timestamp=datetime(2026, 3, 15, i, 0, 0, tzinfo=timezone.utc),
                    circuit_breaker_level="GREEN",
                    balance=Decimal("100"),
                )
            )
        cycles = db.get_recent_cycles(n=3)
        assert len(cycles) == 3
        # Most recent first (highest cycle_number = highest id)
        assert cycles[0].cycle_number == 4
        assert cycles[2].cycle_number == 2

    def test_get_cycles_since(self, db: DatabaseManager) -> None:
        for i in range(5):
            db.store_cycle(
                CycleHistoryRow(
                    cycle_number=i,
                    timestamp=datetime(2026, 3, 15, i, 0, 0, tzinfo=timezone.utc),
                    circuit_breaker_level="GREEN",
                    balance=Decimal("100"),
                )
            )
        since = datetime(2026, 3, 15, 3, 0, 0, tzinfo=timezone.utc)
        cycles = db.get_cycles_since(since)
        assert len(cycles) == 2
        assert cycles[0].cycle_number == 3

    def test_boolean_roundtrip(self, db: DatabaseManager) -> None:
        cycle = CycleHistoryRow(
            cycle_number=1,
            timestamp=datetime(2026, 3, 15, tzinfo=timezone.utc),
            circuit_breaker_level="YELLOW",
            signal_generated=True,
            trade_placed=True,
        )
        db.store_cycle(cycle)
        result = db.get_recent_cycles(n=1)[0]
        assert result.signal_generated is True
        assert result.trade_placed is True

    def test_balance_decimal_preserved(self, db: DatabaseManager) -> None:
        cycle = CycleHistoryRow(
            cycle_number=99,
            timestamp=datetime(2026, 3, 15, tzinfo=timezone.utc),
            circuit_breaker_level="RED",
            balance=Decimal("33.142857"),
        )
        db.store_cycle(cycle)
        result = db.get_recent_cycles(n=1)[0]
        assert result.balance == Decimal("33.142857")


# ---------------------------------------------------------------------------
# System State (key-value)
# ---------------------------------------------------------------------------


class TestSystemState:
    def test_set_and_get(self, db: DatabaseManager) -> None:
        db.set_state("bot.version", "3.0.0")
        assert db.get_state("bot.version") == "3.0.0"

    def test_get_missing_returns_default(self, db: DatabaseManager) -> None:
        assert db.get_state("nonexistent") is None
        assert db.get_state("nonexistent", "fallback") == "fallback"

    def test_upsert_overwrites(self, db: DatabaseManager) -> None:
        db.set_state("key", "v1")
        db.set_state("key", "v2")
        assert db.get_state("key") == "v2"

    def test_get_all_state(self, db: DatabaseManager) -> None:
        db.set_state("a", "1")
        db.set_state("b", "2")
        db.set_state("c", "3")
        all_state = db.get_all_state()
        assert all_state == {"a": "1", "b": "2", "c": "3"}


# ---------------------------------------------------------------------------
# Strategy Metrics CRUD
# ---------------------------------------------------------------------------


class TestStrategyMetrics:
    def test_store_and_get(
        self, db: DatabaseManager, sample_metrics: StrategyMetricRow
    ) -> None:
        db.store_strategy_metrics(sample_metrics)
        result = db.get_strategy_metrics("SupertrendTrend", "TRENDING")
        assert result is not None
        assert result.total_trades == 50
        assert result.win_rate == Decimal("0.60")
        assert result.profit_factor == Decimal("1.85")

    def test_get_nonexistent_returns_none(self, db: DatabaseManager) -> None:
        assert db.get_strategy_metrics("NoSuchStrategy") is None

    def test_upsert_replaces(
        self, db: DatabaseManager, sample_metrics: StrategyMetricRow
    ) -> None:
        db.store_strategy_metrics(sample_metrics)
        updated = StrategyMetricRow(
            strategy="SupertrendTrend",
            regime="TRENDING",
            total_trades=60,
            wins=38,
            losses=22,
            win_rate=Decimal("0.6333"),
            avg_pnl=Decimal("1.50"),
            total_pnl=Decimal("90.00"),
            max_win=Decimal("18.00"),
            max_loss=Decimal("-9.00"),
            profit_factor=Decimal("2.10"),
            sharpe=2.8,
            last_updated=datetime(2026, 3, 16, tzinfo=timezone.utc),
        )
        db.store_strategy_metrics(updated)
        result = db.get_strategy_metrics("SupertrendTrend", "TRENDING")
        assert result is not None
        assert result.total_trades == 60
        assert result.profit_factor == Decimal("2.10")

    def test_get_all_strategy_metrics(self, db: DatabaseManager) -> None:
        for strat, regime in [("ST", "TRENDING"), ("ST", "RANGING"), ("MR", "RANGING")]:
            db.store_strategy_metrics(
                StrategyMetricRow(
                    strategy=strat,
                    regime=regime,
                    total_trades=10,
                    wins=6,
                    losses=4,
                    win_rate=Decimal("0.6"),
                    avg_pnl=Decimal("1"),
                    total_pnl=Decimal("10"),
                    max_win=Decimal("5"),
                    max_loss=Decimal("-3"),
                    profit_factor=Decimal("1.5"),
                    sharpe=1.0,
                    last_updated=datetime(2026, 3, 15, tzinfo=timezone.utc),
                )
            )
        all_metrics = db.get_all_strategy_metrics()
        assert len(all_metrics) == 3

    def test_decimal_precision_on_metrics(self, db: DatabaseManager) -> None:
        m = StrategyMetricRow(
            strategy="Test",
            regime="ALL",
            total_trades=1,
            wins=1,
            losses=0,
            win_rate=Decimal("1.000000"),
            avg_pnl=Decimal("0.123456789"),
            total_pnl=Decimal("0.123456789"),
            max_win=Decimal("0.123456789"),
            max_loss=Decimal("0"),
            profit_factor=Decimal("999.999"),
            sharpe=5.5,
            last_updated=datetime(2026, 3, 15, tzinfo=timezone.utc),
        )
        db.store_strategy_metrics(m)
        result = db.get_strategy_metrics("Test", "ALL")
        assert result is not None
        assert result.avg_pnl == Decimal("0.123456789")


# ---------------------------------------------------------------------------
# Migration helpers
# ---------------------------------------------------------------------------


class TestMigration:
    def test_migrate_from_nonexistent_journal(self, db: DatabaseManager, tmp_path: Path) -> None:
        count = db.migrate_from_trade_journal(tmp_path / "no_such.db")
        assert count == 0

    def test_migrate_from_empty_journal(self, db: DatabaseManager, tmp_path: Path) -> None:
        old_path = tmp_path / "old.db"
        conn = sqlite3.connect(str(old_path))
        conn.execute("CREATE TABLE other (id INTEGER)")
        conn.close()
        count = db.migrate_from_trade_journal(old_path)
        assert count == 0

    def test_migrate_trades(self, db: DatabaseManager, tmp_path: Path) -> None:
        old_path = tmp_path / "old.db"
        conn = sqlite3.connect(str(old_path))
        conn.execute(
            """CREATE TABLE trades (
                trade_id TEXT PRIMARY KEY, timestamp TEXT, symbol TEXT,
                direction TEXT, entry_price TEXT, exit_price TEXT, size TEXT,
                leverage INTEGER, pnl TEXT, pnl_pct TEXT, strategy TEXT,
                regime TEXT, confidence REAL, stop_loss TEXT, take_profit TEXT,
                duration REAL, fees TEXT, slippage TEXT, reasoning TEXT,
                lessons TEXT
            )"""
        )
        conn.execute(
            """INSERT INTO trades VALUES (
                'T001', '2026-03-15T12:00:00', 'ETH/USDT:USDT', 'LONG',
                '3500.00', '3550.00', '0.01', 5, '0.50', '1.0',
                'SupertrendTrend', 'TRENDING', 75.0, '3400.00', '3700.00',
                3600.0, '0.035', '0.001', 'test trade', 'lesson learned'
            )"""
        )
        conn.commit()
        conn.close()

        count = db.migrate_from_trade_journal(old_path)
        assert count == 1

    def test_migrate_drawdown_state(self, db: DatabaseManager, tmp_path: Path) -> None:
        json_path = tmp_path / "drawdown.json"
        json_path.write_text(
            '{"peak_balance": "1000.00", "current_balance": "980.00", '
            '"max_drawdown_pct": "2.0", "max_drawdown_balance": "20.00", '
            '"updated_at": "2026-03-15T12:00:00"}'
        )
        result = db.migrate_drawdown_state(json_path)
        assert result is True
        assert db.get_state("drawdown.peak_balance") == "1000.00"
        assert db.get_state("drawdown.current_balance") == "980.00"

    def test_migrate_drawdown_nonexistent(self, db: DatabaseManager, tmp_path: Path) -> None:
        result = db.migrate_drawdown_state(tmp_path / "no_such.json")
        assert result is False
