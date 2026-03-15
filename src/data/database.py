"""
Consolidated SQLite database manager for Claude Quant.

Single database file at ``user_data/claude_quant.db`` replaces the
previously fragmented storage (trade_journal.db, candles.db, JSON state
files).  WAL mode for concurrent reads; all monetary values stored as TEXT
to preserve ``Decimal`` precision.

Tables
------
- ``trades``           — full trade journal (migrated from trade_journal.db)
- ``daily_reports``    — daily P&L snapshots
- ``cycle_history``    — every orchestrator cycle result
- ``system_state``     — key-value store for bot state (replaces JSON files)
- ``strategy_metrics`` — cached aggregated metrics per strategy/regime

Candle tables are dynamically created per symbol/timeframe via CandleStore
(unchanged — kept separate for table-per-pair pattern).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("claude_quant.data.database")

_DEFAULT_DB_PATH = Path("user_data/claude_quant.db")


# ---------------------------------------------------------------------------
# Pydantic models for new tables
# ---------------------------------------------------------------------------


class DailyReportRow(BaseModel):
    """Stored daily P&L report."""

    report_date: date
    start_balance: Decimal
    end_balance: Decimal
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    net_pnl: Decimal = Decimal("0")
    pnl_pct: Decimal = Decimal("0")
    trades_count: int = 0
    wins: int = 0
    losses: int = 0
    strategies_used: str = ""  # comma-separated
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class CycleHistoryRow(BaseModel):
    """Stored orchestrator cycle result."""

    cycle_number: int
    timestamp: datetime
    circuit_breaker_level: str
    balance: Decimal = Decimal("0")
    regime: str | None = None
    signal_generated: bool = False
    trade_placed: bool = False
    trade_details: str | None = None  # JSON string
    positions_closed: str = "[]"  # JSON string
    errors: str = "[]"  # JSON string
    duration_seconds: float = 0.0


class StrategyMetricRow(BaseModel):
    """Cached strategy performance metrics."""

    strategy: str
    regime: str = ""
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: Decimal = Decimal("0")
    avg_pnl: Decimal = Decimal("0")
    total_pnl: Decimal = Decimal("0")
    max_win: Decimal = Decimal("0")
    max_loss: Decimal = Decimal("0")
    profit_factor: Decimal = Decimal("0")
    sharpe: float = 0.0
    last_updated: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


# ---------------------------------------------------------------------------
# SQL schema
# ---------------------------------------------------------------------------

_SCHEMA_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    trade_id       TEXT PRIMARY KEY,
    timestamp      TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    direction      TEXT NOT NULL,
    entry_price    TEXT NOT NULL,
    exit_price     TEXT,
    size           TEXT NOT NULL,
    leverage       INTEGER NOT NULL DEFAULT 1,
    pnl            TEXT,
    pnl_pct        TEXT,
    strategy       TEXT DEFAULT '',
    regime         TEXT DEFAULT '',
    confidence     TEXT DEFAULT '0',
    stop_loss      TEXT,
    take_profit    TEXT,
    duration       REAL,
    fees           TEXT DEFAULT '0',
    slippage       TEXT DEFAULT '0',
    reasoning      TEXT DEFAULT '',
    lessons        TEXT DEFAULT ''
);
"""

_SCHEMA_DAILY_REPORTS = """
CREATE TABLE IF NOT EXISTS daily_reports (
    report_date    TEXT PRIMARY KEY,
    start_balance  TEXT NOT NULL,
    end_balance    TEXT NOT NULL,
    realized_pnl   TEXT DEFAULT '0',
    unrealized_pnl TEXT DEFAULT '0',
    fees           TEXT DEFAULT '0',
    net_pnl        TEXT DEFAULT '0',
    pnl_pct        TEXT DEFAULT '0',
    trades_count   INTEGER DEFAULT 0,
    wins           INTEGER DEFAULT 0,
    losses         INTEGER DEFAULT 0,
    strategies_used TEXT DEFAULT '',
    created_at     TEXT NOT NULL
);
"""

_SCHEMA_CYCLE_HISTORY = """
CREATE TABLE IF NOT EXISTS cycle_history (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_number          INTEGER NOT NULL,
    timestamp             TEXT NOT NULL,
    circuit_breaker_level TEXT NOT NULL,
    balance               TEXT DEFAULT '0',
    regime                TEXT,
    signal_generated      INTEGER DEFAULT 0,
    trade_placed          INTEGER DEFAULT 0,
    trade_details         TEXT,
    positions_closed      TEXT DEFAULT '[]',
    errors                TEXT DEFAULT '[]',
    duration_seconds      REAL DEFAULT 0.0
);
"""

_SCHEMA_SYSTEM_STATE = """
CREATE TABLE IF NOT EXISTS system_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_SCHEMA_STRATEGY_METRICS = """
CREATE TABLE IF NOT EXISTS strategy_metrics (
    strategy     TEXT NOT NULL,
    regime       TEXT DEFAULT '',
    total_trades INTEGER DEFAULT 0,
    wins         INTEGER DEFAULT 0,
    losses       INTEGER DEFAULT 0,
    win_rate     TEXT DEFAULT '0',
    avg_pnl      TEXT DEFAULT '0',
    total_pnl    TEXT DEFAULT '0',
    max_win      TEXT DEFAULT '0',
    max_loss     TEXT DEFAULT '0',
    profit_factor TEXT DEFAULT '0',
    sharpe       REAL DEFAULT 0.0,
    last_updated TEXT NOT NULL,
    PRIMARY KEY (strategy, regime)
);
"""

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
CREATE INDEX IF NOT EXISTS idx_trades_regime ON trades(regime);
CREATE INDEX IF NOT EXISTS idx_daily_reports_date ON daily_reports(report_date);
CREATE INDEX IF NOT EXISTS idx_cycle_history_timestamp ON cycle_history(timestamp);
CREATE INDEX IF NOT EXISTS idx_cycle_history_cycle ON cycle_history(cycle_number);
"""


# ---------------------------------------------------------------------------
# DatabaseManager
# ---------------------------------------------------------------------------


class DatabaseManager:
    """Consolidated SQLite database for all Claude Quant persistence.

    Parameters
    ----------
    db_path : Path | str | None
        Path to the SQLite database file.  Defaults to
        ``user_data/claude_quant.db``.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        if db_path is None:
            db_path = _DEFAULT_DB_PATH
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._initialize()

    def _initialize(self) -> None:
        """Create all tables and indexes."""
        conn = self._get_conn()
        conn.executescript(
            _SCHEMA_TRADES
            + _SCHEMA_DAILY_REPORTS
            + _SCHEMA_CYCLE_HISTORY
            + _SCHEMA_SYSTEM_STATE
            + _SCHEMA_STRATEGY_METRICS
            + _INDEXES
        )
        conn.commit()
        logger.info("DatabaseManager initialized at %s", self._db_path)

    def _get_conn(self) -> sqlite3.Connection:
        """Get or create the SQLite connection with WAL mode."""
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def db_path(self) -> Path:
        """Return the database file path."""
        return self._db_path

    # -----------------------------------------------------------------------
    # Daily Reports
    # -----------------------------------------------------------------------

    def store_daily_report(self, report: DailyReportRow) -> None:
        """Insert or replace a daily report."""
        conn = self._get_conn()
        conn.execute(
            """
            INSERT OR REPLACE INTO daily_reports (
                report_date, start_balance, end_balance, realized_pnl,
                unrealized_pnl, fees, net_pnl, pnl_pct, trades_count,
                wins, losses, strategies_used, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report.report_date.isoformat(),
                str(report.start_balance),
                str(report.end_balance),
                str(report.realized_pnl),
                str(report.unrealized_pnl),
                str(report.fees),
                str(report.net_pnl),
                str(report.pnl_pct),
                report.trades_count,
                report.wins,
                report.losses,
                report.strategies_used,
                report.created_at.isoformat(),
            ),
        )
        conn.commit()
        logger.info("Daily report stored for %s", report.report_date)

    def get_daily_report(self, report_date: date) -> DailyReportRow | None:
        """Retrieve a single daily report by date."""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT * FROM daily_reports WHERE report_date = ?",
            (report_date.isoformat(),),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_daily_report(dict(row))

    def get_all_daily_reports(self) -> list[DailyReportRow]:
        """Retrieve all daily reports in chronological order."""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT * FROM daily_reports ORDER BY report_date ASC"
        )
        return [self._row_to_daily_report(dict(r)) for r in cursor.fetchall()]

    @staticmethod
    def _row_to_daily_report(row: dict[str, Any]) -> DailyReportRow:
        return DailyReportRow(
            report_date=date.fromisoformat(row["report_date"]),
            start_balance=Decimal(row["start_balance"]),
            end_balance=Decimal(row["end_balance"]),
            realized_pnl=Decimal(row["realized_pnl"]),
            unrealized_pnl=Decimal(row["unrealized_pnl"]),
            fees=Decimal(row["fees"]),
            net_pnl=Decimal(row["net_pnl"]),
            pnl_pct=Decimal(row["pnl_pct"]),
            trades_count=row["trades_count"],
            wins=row["wins"],
            losses=row["losses"],
            strategies_used=row["strategies_used"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    # -----------------------------------------------------------------------
    # Cycle History
    # -----------------------------------------------------------------------

    def store_cycle(self, cycle: CycleHistoryRow) -> None:
        """Insert a cycle result into history."""
        conn = self._get_conn()
        conn.execute(
            """
            INSERT INTO cycle_history (
                cycle_number, timestamp, circuit_breaker_level, balance,
                regime, signal_generated, trade_placed, trade_details,
                positions_closed, errors, duration_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cycle.cycle_number,
                cycle.timestamp.isoformat(),
                cycle.circuit_breaker_level,
                str(cycle.balance),
                cycle.regime,
                int(cycle.signal_generated),
                int(cycle.trade_placed),
                cycle.trade_details,
                cycle.positions_closed,
                cycle.errors,
                cycle.duration_seconds,
            ),
        )
        conn.commit()

    def get_recent_cycles(self, n: int = 20) -> list[CycleHistoryRow]:
        """Get the most recent N cycles."""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT * FROM cycle_history ORDER BY id DESC LIMIT ?", (n,)
        )
        return [self._row_to_cycle(dict(r)) for r in cursor.fetchall()]

    def get_cycles_since(self, since: datetime) -> list[CycleHistoryRow]:
        """Get all cycles since a given timestamp."""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT * FROM cycle_history WHERE timestamp >= ? ORDER BY id ASC",
            (since.isoformat(),),
        )
        return [self._row_to_cycle(dict(r)) for r in cursor.fetchall()]

    @staticmethod
    def _row_to_cycle(row: dict[str, Any]) -> CycleHistoryRow:
        return CycleHistoryRow(
            cycle_number=row["cycle_number"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            circuit_breaker_level=row["circuit_breaker_level"],
            balance=Decimal(row["balance"]),
            regime=row["regime"],
            signal_generated=bool(row["signal_generated"]),
            trade_placed=bool(row["trade_placed"]),
            trade_details=row["trade_details"],
            positions_closed=row["positions_closed"] or "[]",
            errors=row["errors"] or "[]",
            duration_seconds=row["duration_seconds"],
        )

    # -----------------------------------------------------------------------
    # System State (key-value)
    # -----------------------------------------------------------------------

    def set_state(self, key: str, value: str) -> None:
        """Set a system state key-value pair."""
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT OR REPLACE INTO system_state (key, value, updated_at)
            VALUES (?, ?, ?)
            """,
            (key, value, now),
        )
        conn.commit()

    def get_state(self, key: str, default: str | None = None) -> str | None:
        """Get a system state value by key."""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT value FROM system_state WHERE key = ?", (key,)
        )
        row = cursor.fetchone()
        if row is None:
            return default
        return row["value"]

    def get_all_state(self) -> dict[str, str]:
        """Get all system state key-value pairs."""
        conn = self._get_conn()
        cursor = conn.execute("SELECT key, value FROM system_state")
        return {row["key"]: row["value"] for row in cursor.fetchall()}

    # -----------------------------------------------------------------------
    # Strategy Metrics
    # -----------------------------------------------------------------------

    def store_strategy_metrics(self, metrics: StrategyMetricRow) -> None:
        """Insert or replace strategy metrics."""
        conn = self._get_conn()
        conn.execute(
            """
            INSERT OR REPLACE INTO strategy_metrics (
                strategy, regime, total_trades, wins, losses, win_rate,
                avg_pnl, total_pnl, max_win, max_loss, profit_factor,
                sharpe, last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                metrics.strategy,
                metrics.regime,
                metrics.total_trades,
                metrics.wins,
                metrics.losses,
                str(metrics.win_rate),
                str(metrics.avg_pnl),
                str(metrics.total_pnl),
                str(metrics.max_win),
                str(metrics.max_loss),
                str(metrics.profit_factor),
                metrics.sharpe,
                metrics.last_updated.isoformat(),
            ),
        )
        conn.commit()

    def get_strategy_metrics(
        self, strategy: str, regime: str = ""
    ) -> StrategyMetricRow | None:
        """Get cached metrics for a strategy/regime combination."""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT * FROM strategy_metrics WHERE strategy = ? AND regime = ?",
            (strategy, regime),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_strategy_metric(dict(row))

    def get_all_strategy_metrics(self) -> list[StrategyMetricRow]:
        """Get all cached strategy metrics."""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT * FROM strategy_metrics ORDER BY strategy, regime"
        )
        return [self._row_to_strategy_metric(dict(r)) for r in cursor.fetchall()]

    @staticmethod
    def _row_to_strategy_metric(row: dict[str, Any]) -> StrategyMetricRow:
        return StrategyMetricRow(
            strategy=row["strategy"],
            regime=row["regime"],
            total_trades=row["total_trades"],
            wins=row["wins"],
            losses=row["losses"],
            win_rate=Decimal(row["win_rate"]),
            avg_pnl=Decimal(row["avg_pnl"]),
            total_pnl=Decimal(row["total_pnl"]),
            max_win=Decimal(row["max_win"]),
            max_loss=Decimal(row["max_loss"]),
            profit_factor=Decimal(row["profit_factor"]),
            sharpe=row["sharpe"],
            last_updated=datetime.fromisoformat(row["last_updated"]),
        )

    # -----------------------------------------------------------------------
    # Migration helpers
    # -----------------------------------------------------------------------

    def migrate_from_trade_journal(self, old_db_path: Path | str) -> int:
        """Import trades from an existing trade_journal.db into the
        consolidated database.

        Parameters
        ----------
        old_db_path : Path | str
            Path to the old ``trade_journal.db``.

        Returns
        -------
        int
            Number of trades imported.
        """
        old_path = Path(old_db_path)
        if not old_path.exists():
            logger.warning("Old trade journal not found at %s", old_path)
            return 0

        old_conn = sqlite3.connect(str(old_path))
        old_conn.row_factory = sqlite3.Row

        try:
            cursor = old_conn.execute("SELECT * FROM trades")
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            logger.warning("No 'trades' table in %s", old_path)
            old_conn.close()
            return 0

        conn = self._get_conn()
        count = 0
        for row in rows:
            d = dict(row)
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO trades (
                        trade_id, timestamp, symbol, direction, entry_price,
                        exit_price, size, leverage, pnl, pnl_pct, strategy,
                        regime, confidence, stop_loss, take_profit, duration,
                        fees, slippage, reasoning, lessons
                    ) VALUES (
                        :trade_id, :timestamp, :symbol, :direction,
                        :entry_price, :exit_price, :size, :leverage,
                        :pnl, :pnl_pct, :strategy, :regime, :confidence,
                        :stop_loss, :take_profit, :duration, :fees,
                        :slippage, :reasoning, :lessons
                    )
                    """,
                    d,
                )
                count += 1
            except (sqlite3.IntegrityError, KeyError) as exc:
                logger.warning("Skipping trade %s: %s", d.get("trade_id"), exc)

        conn.commit()
        old_conn.close()
        logger.info("Migrated %d trades from %s", count, old_path)
        return count

    def migrate_drawdown_state(self, json_path: Path | str) -> bool:
        """Import drawdown state from a JSON file into system_state table.

        Parameters
        ----------
        json_path : Path | str
            Path to ``drawdown_state.json``.

        Returns
        -------
        bool
            True if migration succeeded.
        """
        path = Path(json_path)
        if not path.exists():
            logger.warning("Drawdown state file not found at %s", path)
            return False

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for key in ("peak_balance", "current_balance", "max_drawdown_pct",
                        "max_drawdown_balance"):
                if key in data:
                    self.set_state(f"drawdown.{key}", str(data[key]))
            if "updated_at" in data:
                self.set_state("drawdown.updated_at", data["updated_at"])
            logger.info("Migrated drawdown state from %s", path)
            return True
        except (json.JSONDecodeError, KeyError) as exc:
            logger.error("Failed to migrate drawdown state: %s", exc)
            return False
