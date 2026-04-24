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


class BaselineRow(BaseModel):
    """A canonical baseline epoch marker for reporting.

    Stored in ``system_state`` under the ``baseline.*`` key namespace.
    Used by Phase 2C reporting to filter cumulative views to the current
    reduced-live era without deleting historical data.

    All fields are required at set time.  ``started_at_utc`` MUST be
    timezone-aware (UTC).  Naive datetimes are rejected.
    """

    model_config = {"frozen": True}

    current_mode: str = Field(
        ..., min_length=1,
        description="Free-form label for the reporting era (e.g. 'mainnet_reduced_live_v1').",
    )
    started_at_utc: datetime = Field(
        ...,
        description="UTC timestamp from which the baseline counts (inclusive).",
    )
    start_balance_usdt: Decimal = Field(
        ...,
        description="Balance in USDT at the moment the baseline was set.",
    )
    notes: str = Field(default="", description="Free-form audit note.")
    set_at_utc: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When the baseline row itself was written to the DB.",
    )

    @classmethod
    def _validate_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("datetime must be timezone-aware (UTC)")
        return value.astimezone(timezone.utc)

    def model_post_init(self, __context: Any) -> None:  # type: ignore[override]
        # frozen=True — we must use object.__setattr__ to normalise to UTC
        started = self.started_at_utc
        if started.tzinfo is None:
            raise ValueError("started_at_utc must be timezone-aware (UTC)")
        set_at = self.set_at_utc
        if set_at.tzinfo is None:
            raise ValueError("set_at_utc must be timezone-aware (UTC)")
        object.__setattr__(self, "started_at_utc", started.astimezone(timezone.utc))
        object.__setattr__(self, "set_at_utc", set_at.astimezone(timezone.utc))


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
    trade_id            TEXT PRIMARY KEY,
    timestamp           TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    direction           TEXT NOT NULL,
    entry_price         TEXT NOT NULL,
    exit_price          TEXT,
    size                TEXT NOT NULL,
    leverage            INTEGER NOT NULL DEFAULT 1,
    pnl                 TEXT,
    pnl_pct             TEXT,
    strategy            TEXT DEFAULT '',
    regime              TEXT DEFAULT '',
    confidence          TEXT DEFAULT '0',
    stop_loss           TEXT,
    take_profit         TEXT,
    duration            REAL,
    fees                TEXT DEFAULT '0',
    slippage            TEXT DEFAULT '0',
    reasoning           TEXT DEFAULT '',
    lessons             TEXT DEFAULT '',
    -- Phase 1B: per-trade attribution columns
    cascade_level       TEXT DEFAULT '',
    confidence_bucket   TEXT DEFAULT '',
    regime_at_entry     TEXT DEFAULT '',
    atr_at_entry        TEXT DEFAULT '0',
    entry_slippage_bps  REAL DEFAULT 0,
    exit_slippage_bps   REAL DEFAULT 0,
    maker_entry         INTEGER DEFAULT 0,
    maker_exit          INTEGER DEFAULT 0,
    fees_usd            TEXT DEFAULT '0',
    funding_usd         TEXT DEFAULT '0',
    hold_bars           INTEGER DEFAULT 0,
    exit_reason_enum    TEXT DEFAULT '',
    consensus_adj       REAL DEFAULT 0,
    funding_adj         REAL DEFAULT 0
);
"""

_SCHEMA_FILL_EVENTS = """
CREATE TABLE IF NOT EXISTS fill_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id          TEXT NOT NULL,
    side_of_trade     TEXT NOT NULL,
    timestamp_utc     TEXT NOT NULL,
    order_type        TEXT NOT NULL,
    requested_price   TEXT NOT NULL,
    fill_price        TEXT NOT NULL,
    filled_qty        TEXT NOT NULL,
    is_maker          INTEGER NOT NULL,
    fees_usd          TEXT NOT NULL,
    client_order_id   TEXT,
    exchange_order_id TEXT,
    FOREIGN KEY (trade_id) REFERENCES trades(trade_id)
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

_SCHEMA_TRAILING_STOPS = """
CREATE TABLE IF NOT EXISTS trailing_stops (
    symbol          TEXT PRIMARY KEY,
    direction       TEXT NOT NULL,
    entry_price     REAL NOT NULL,
    best_price      REAL NOT NULL,
    atr_4h          REAL NOT NULL DEFAULT 0.0,
    activated       INTEGER NOT NULL DEFAULT 0,
    strategy_name   TEXT DEFAULT '',
    take_profit     REAL DEFAULT 0.0,
    tp_pending      INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL
);
"""

_SCHEMA_AUDIT_TRAIL = """
CREATE TABLE IF NOT EXISTS audit_trail (
    audit_id       TEXT PRIMARY KEY,
    trade_id       TEXT,
    timestamp      TEXT NOT NULL,
    symbol         TEXT,
    direction      TEXT,
    strategy_name  TEXT,
    regime         TEXT,
    decision       TEXT,
    report_json    TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_SCHEMA_DECISION_LOG = """
CREATE TABLE IF NOT EXISTS decision_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id          INTEGER NOT NULL,
    timestamp_utc     TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    stage             TEXT NOT NULL,
    outcome           TEXT NOT NULL,
    reason            TEXT,
    numeric_context   TEXT,
    cascade_level     TEXT,
    confidence        REAL,
    regime            TEXT,
    FOREIGN KEY (cycle_id) REFERENCES cycle_history(id)
);
"""

_VIEW_CYCLE_FUNNEL = """
CREATE VIEW IF NOT EXISTS cycle_funnel AS
SELECT
    cycle_id,
    stage,
    COUNT(*)                                        AS attempts,
    SUM(CASE WHEN outcome='pass'   THEN 1 ELSE 0 END) AS passes,
    SUM(CASE WHEN outcome='reject' THEN 1 ELSE 0 END) AS rejects,
    SUM(CASE WHEN outcome='error'  THEN 1 ELSE 0 END) AS errors
FROM decision_log
GROUP BY cycle_id, stage;
"""

# ---------------------------------------------------------------------------
# Phase 1C: Canonical forensic views (queries 2–12 from LIVE_FORENSICS_SPEC §4)
# All views are CREATE VIEW IF NOT EXISTS so they are idempotent.
# ---------------------------------------------------------------------------

_VIEWS_FORENSIC = """
CREATE VIEW IF NOT EXISTS v_cascade_expectancy AS
SELECT
    cascade_level,
    COUNT(*)                                                                   AS n,
    AVG(CAST(pnl AS REAL))                                                     AS avg_pnl,
    SUM(CASE WHEN CAST(pnl AS REAL) > 0 THEN 1.0 ELSE 0 END) / COUNT(*)       AS win_rate,
    SUM(CAST(pnl AS REAL))                                                     AS total_pnl
FROM trades
WHERE pnl IS NOT NULL AND cascade_level != ''
GROUP BY cascade_level;

CREATE VIEW IF NOT EXISTS v_regime_expectancy AS
SELECT
    regime_at_entry,
    COUNT(*)                                                                   AS n,
    AVG(CAST(pnl AS REAL))                                                     AS avg_pnl,
    AVG(CAST(fees_usd AS REAL))                                                AS avg_fees,
    AVG(CAST(funding_usd AS REAL))                                             AS avg_funding,
    SUM(CAST(pnl AS REAL))                                                     AS total_pnl
FROM trades
WHERE pnl IS NOT NULL
GROUP BY regime_at_entry;

CREATE VIEW IF NOT EXISTS v_maker_taker_pnl AS
SELECT
    maker_entry,
    COUNT(*)                                                                   AS n,
    AVG(CAST(pnl AS REAL) - CAST(fees_usd AS REAL))                            AS avg_net_pnl,
    AVG(CAST(fees_usd AS REAL))                                                AS avg_fees,
    AVG(CAST(pnl AS REAL))                                                     AS avg_gross_pnl
FROM trades
WHERE pnl IS NOT NULL
GROUP BY maker_entry;

CREATE VIEW IF NOT EXISTS v_exit_reason_mix AS
SELECT
    exit_reason_enum,
    COUNT(*)                                                                   AS n,
    AVG(CAST(pnl AS REAL))                                                     AS avg_pnl,
    SUM(CASE WHEN CAST(pnl AS REAL) > 0 THEN 1.0 ELSE 0 END) / COUNT(*)       AS win_rate,
    SUM(CAST(pnl AS REAL))                                                     AS total_pnl
FROM trades
WHERE pnl IS NOT NULL
GROUP BY exit_reason_enum;

CREATE VIEW IF NOT EXISTS v_symbol_pnl AS
SELECT
    symbol,
    COUNT(*)                                                                   AS n,
    SUM(CAST(pnl AS REAL))                                                     AS total_pnl,
    SUM(CAST(fees_usd AS REAL))                                                AS total_fees,
    SUM(CAST(funding_usd AS REAL))                                             AS total_funding,
    SUM(CAST(pnl AS REAL))
        - SUM(CAST(fees_usd AS REAL))
        + SUM(CAST(funding_usd AS REAL))                                       AS gross_edge_pnl
FROM trades
WHERE pnl IS NOT NULL
GROUP BY symbol;

CREATE VIEW IF NOT EXISTS v_confidence_bucket_wr AS
SELECT
    confidence_bucket,
    COUNT(*)                                                                   AS n,
    SUM(CASE WHEN CAST(pnl AS REAL) > 0 THEN 1.0 ELSE 0 END) / COUNT(*)       AS win_rate,
    AVG(CAST(pnl AS REAL))                                                     AS avg_pnl,
    SUM(CAST(pnl AS REAL))                                                     AS total_pnl
FROM trades
WHERE pnl IS NOT NULL
GROUP BY confidence_bucket;
"""

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
CREATE INDEX IF NOT EXISTS idx_trades_regime ON trades(regime);
CREATE INDEX IF NOT EXISTS idx_daily_reports_date ON daily_reports(report_date);
CREATE INDEX IF NOT EXISTS idx_cycle_history_timestamp ON cycle_history(timestamp);
CREATE INDEX IF NOT EXISTS idx_cycle_history_cycle ON cycle_history(cycle_number);
CREATE INDEX IF NOT EXISTS idx_audit_trail_trade_id ON audit_trail(trade_id);
CREATE INDEX IF NOT EXISTS idx_audit_trail_timestamp ON audit_trail(timestamp);
CREATE INDEX IF NOT EXISTS idx_decision_log_cycle ON decision_log(cycle_id);
CREATE INDEX IF NOT EXISTS idx_decision_log_symbol_stage ON decision_log(symbol, stage);
CREATE INDEX IF NOT EXISTS idx_decision_log_timestamp ON decision_log(timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_fill_events_trade ON fill_events(trade_id);
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
        self._mirror: Any | None = None  # SupabaseMirror, set via attach_mirror()
        self._initialize()

    def attach_mirror(self, mirror: Any) -> None:
        """Attach a SupabaseMirror (or any object exposing ``enqueue(table, row)``).

        The mirror is best-effort: every successful local write also enqueues
        an upsert to Supabase. Local writes NEVER block on the mirror and
        never raise if the mirror is down.
        """
        self._mirror = mirror

    def _mirror_enqueue(self, table: str, row: dict[str, Any]) -> None:
        if self._mirror is None:
            return
        try:
            self._mirror.enqueue(table, row)
        except Exception as exc:  # noqa: BLE001 — never block local writes
            logger.warning("Mirror enqueue failed for %s: %s", table, exc)

    def _initialize(self) -> None:
        """Create all tables, indexes, and views."""
        conn = self._get_conn()
        conn.executescript(
            _SCHEMA_TRADES
            + _SCHEMA_DAILY_REPORTS
            + _SCHEMA_CYCLE_HISTORY
            + _SCHEMA_SYSTEM_STATE
            + _SCHEMA_STRATEGY_METRICS
            + _SCHEMA_TRAILING_STOPS
            + _SCHEMA_AUDIT_TRAIL
            + _SCHEMA_DECISION_LOG
            + _SCHEMA_FILL_EVENTS
            + _INDEXES
            + _VIEW_CYCLE_FUNNEL
            + _VIEWS_FORENSIC
        )
        conn.commit()
        self._run_migrations(conn)
        logger.info("DatabaseManager initialized at %s", self._db_path)

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        """Apply schema migrations for columns added after initial release."""
        # v6.12: Add tp_pending column to trailing_stops
        try:
            cursor = conn.execute("PRAGMA table_info(trailing_stops)")
            columns = {row["name"] for row in cursor.fetchall()}
            if "tp_pending" not in columns:
                conn.execute(
                    "ALTER TABLE trailing_stops ADD COLUMN tp_pending INTEGER NOT NULL DEFAULT 0"
                )
                conn.commit()
                logger.info("Migration: added tp_pending column to trailing_stops")
        except Exception as exc:
            logger.warning("Migration check (trailing_stops) failed: %s", exc)

        # Phase 1B: add 14 per-trade attribution columns to trades
        _TRADES_1B_COLUMNS = [
            ("cascade_level",      "TEXT DEFAULT ''"),
            ("confidence_bucket",  "TEXT DEFAULT ''"),
            ("regime_at_entry",    "TEXT DEFAULT ''"),
            ("atr_at_entry",       "TEXT DEFAULT '0'"),
            ("entry_slippage_bps", "REAL DEFAULT 0"),
            ("exit_slippage_bps",  "REAL DEFAULT 0"),
            ("maker_entry",        "INTEGER DEFAULT 0"),
            ("maker_exit",         "INTEGER DEFAULT 0"),
            ("fees_usd",           "TEXT DEFAULT '0'"),
            ("funding_usd",        "TEXT DEFAULT '0'"),
            ("hold_bars",          "INTEGER DEFAULT 0"),
            ("exit_reason_enum",   "TEXT DEFAULT ''"),
            ("consensus_adj",      "REAL DEFAULT 0"),
            ("funding_adj",        "REAL DEFAULT 0"),
        ]
        try:
            cursor = conn.execute("PRAGMA table_info(trades)")
            existing_cols = {row["name"] for row in cursor.fetchall()}
            added: list[str] = []
            for col_name, col_def in _TRADES_1B_COLUMNS:
                if col_name not in existing_cols:
                    conn.execute(
                        f"ALTER TABLE trades ADD COLUMN {col_name} {col_def}"
                    )
                    added.append(col_name)
            if added:
                conn.commit()
                logger.info("Migration: added Phase 1B attribution columns to trades: %s", added)
        except Exception as exc:
            logger.warning("Migration check (trades Phase 1B) failed: %s", exc)

        # Phase 1B: create fill_events table if not present
        try:
            conn.executescript(_SCHEMA_FILL_EVENTS)
            conn.executescript(
                "CREATE INDEX IF NOT EXISTS idx_fill_events_trade ON fill_events(trade_id);"
            )
            conn.commit()
        except Exception as exc:
            logger.warning("Migration check (fill_events) failed: %s", exc)

    def _get_conn(self) -> sqlite3.Connection:
        """Get or create the SQLite connection with WAL mode."""
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
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
        self._mirror_enqueue("daily_reports", {
            "report_date": report.report_date.isoformat(),
            "start_balance": str(report.start_balance),
            "end_balance": str(report.end_balance),
            "realized_pnl": str(report.realized_pnl),
            "unrealized_pnl": str(report.unrealized_pnl),
            "fees": str(report.fees),
            "net_pnl": str(report.net_pnl),
            "pnl_pct": str(report.pnl_pct),
            "trades_count": report.trades_count,
            "wins": report.wins,
            "losses": report.losses,
            "strategies_used": report.strategies_used,
            "created_at": report.created_at.isoformat(),
        })
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
        self._mirror_enqueue("cycle_history", {
            "cycle_number": cycle.cycle_number,
            "timestamp": cycle.timestamp.isoformat(),
            "circuit_breaker_level": cycle.circuit_breaker_level,
            "balance": str(cycle.balance),
            "regime": cycle.regime,
            "signal_generated": bool(cycle.signal_generated),
            "trade_placed": bool(cycle.trade_placed),
            "trade_details": cycle.trade_details,
            "positions_closed": cycle.positions_closed,
            "errors": cycle.errors,
            "duration_seconds": cycle.duration_seconds,
        })

    def get_recent_cycles(self, n: int = 20) -> list[CycleHistoryRow]:
        """Get the most recent N cycles."""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT * FROM cycle_history ORDER BY id DESC LIMIT ?", (n,)
        )
        return [self._row_to_cycle(dict(r)) for r in cursor.fetchall()]

    def begin_cycle(self, cycle_number: int, timestamp: datetime) -> int:
        """Insert a minimal cycle_history placeholder at cycle START.

        Returns the new row's ``id`` (cycle_id) so decision_log rows can
        reference it via FK before the cycle completes.  The row is updated
        with final data by ``finish_cycle``.
        """
        conn = self._get_conn()
        cursor = conn.execute(
            """
            INSERT INTO cycle_history (
                cycle_number, timestamp, circuit_breaker_level,
                balance, positions_closed, errors, duration_seconds
            ) VALUES (?, ?, 'PENDING', '0', '[]', '[]', 0.0)
            """,
            (cycle_number, timestamp.isoformat()),
        )
        conn.commit()
        cycle_id: int = cursor.lastrowid  # type: ignore[assignment]
        return cycle_id

    def finish_cycle(self, cycle_id: int, cycle: "CycleHistoryRow") -> None:
        """Update the placeholder row created by ``begin_cycle`` with final data."""
        conn = self._get_conn()
        conn.execute(
            """
            UPDATE cycle_history SET
                circuit_breaker_level = ?,
                balance               = ?,
                regime                = ?,
                signal_generated      = ?,
                trade_placed          = ?,
                trade_details         = ?,
                positions_closed      = ?,
                errors                = ?,
                duration_seconds      = ?
            WHERE id = ?
            """,
            (
                cycle.circuit_breaker_level,
                str(cycle.balance),
                cycle.regime,
                int(cycle.signal_generated),
                int(cycle.trade_placed),
                cycle.trade_details,
                cycle.positions_closed,
                cycle.errors,
                cycle.duration_seconds,
                cycle_id,
            ),
        )
        conn.commit()
        self._mirror_enqueue("cycle_history", {
            "id": cycle_id,
            "cycle_number": cycle.cycle_number,
            "timestamp": cycle.timestamp.isoformat(),
            "circuit_breaker_level": cycle.circuit_breaker_level,
            "balance": str(cycle.balance),
            "regime": cycle.regime,
            "signal_generated": bool(cycle.signal_generated),
            "trade_placed": bool(cycle.trade_placed),
            "trade_details": cycle.trade_details,
            "positions_closed": cycle.positions_closed,
            "errors": cycle.errors,
            "duration_seconds": cycle.duration_seconds,
        })

    # -----------------------------------------------------------------------
    # Decision Log
    # -----------------------------------------------------------------------

    def insert_decision_log(
        self,
        cycle_id: int,
        timestamp_utc: str,
        symbol: str,
        stage: str,
        outcome: str,
        reason: str | None = None,
        numeric_context: str | dict | None = None,
        cascade_level: str | None = None,
        confidence: float | None = None,
        regime: str | None = None,
    ) -> None:
        """Insert one decision_log row.

        ``numeric_context`` may be a pre-serialised JSON string or a raw
        dict (which will be serialised here with ``default=str``).
        Callers should prefer ``DecisionLogger.log()`` over calling this
        directly.
        """
        if isinstance(numeric_context, dict):
            numeric_context = json.dumps(numeric_context, default=str)
        conn = self._get_conn()
        conn.execute(
            """
            INSERT INTO decision_log (
                cycle_id, timestamp_utc, symbol, stage, outcome,
                reason, numeric_context, cascade_level, confidence, regime
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cycle_id, timestamp_utc, symbol, stage, outcome,
                reason, numeric_context, cascade_level, confidence, regime,
            ),
        )
        conn.commit()

    def get_decision_logs(
        self,
        cycle_id: int | None = None,
        symbol: str | None = None,
        stage: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Retrieve decision_log rows, optionally filtered.

        Returns a list of plain dicts (one per row) ordered by id ASC.
        """
        conn = self._get_conn()
        clauses: list[str] = []
        params: list[Any] = []
        if cycle_id is not None:
            clauses.append("cycle_id = ?")
            params.append(cycle_id)
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(symbol)
        if stage is not None:
            clauses.append("stage = ?")
            params.append(stage)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        cursor = conn.execute(
            f"SELECT * FROM decision_log {where} ORDER BY id ASC LIMIT ?",  # noqa: S608
            params,
        )
        return [dict(r) for r in cursor.fetchall()]



    # -----------------------------------------------------------------------
    # Fill Events (Phase 1B)
    # -----------------------------------------------------------------------

    def insert_fill_event(
        self,
        trade_id: str,
        side_of_trade: str,
        timestamp_utc: str,
        order_type: str,
        requested_price: str,
        fill_price: str,
        filled_qty: str,
        is_maker: int,
        fees_usd: str,
        client_order_id: str | None = None,
        exchange_order_id: str | None = None,
    ) -> int:
        """Insert one fill_events row; returns the new row's id.

        Parameters
        ----------
        trade_id : str
            FK reference to trades.trade_id.
        side_of_trade : str
            'entry' or 'exit'.
        timestamp_utc : str
            ISO-8601 UTC timestamp.
        order_type : str
            'limit_gtx', 'market', 'stop_market', 'tp_market', or 'native_trail'.
        requested_price : str
            The signal/intended execution price as a decimal string.
        fill_price : str
            Actual fill price as a decimal string.
        filled_qty : str
            Filled quantity (base units) as a decimal string.
        is_maker : int
            1 if maker fill, 0 if taker.
        fees_usd : str
            Fees paid in USDT as a decimal string.
        client_order_id : str | None
            ccxt client order ID (cq_<uuid16> format).
        exchange_order_id : str | None
            Exchange-assigned order ID.
        """
        conn = self._get_conn()
        cursor = conn.execute(
            """
            INSERT INTO fill_events (
                trade_id, side_of_trade, timestamp_utc, order_type,
                requested_price, fill_price, filled_qty, is_maker,
                fees_usd, client_order_id, exchange_order_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade_id, side_of_trade, timestamp_utc, order_type,
                requested_price, fill_price, filled_qty, is_maker,
                fees_usd, client_order_id, exchange_order_id,
            ),
        )
        conn.commit()
        row_id: int = cursor.lastrowid  # type: ignore[assignment]
        return row_id

    def get_fill_events_for_trade(self, trade_id: str) -> list[dict[str, Any]]:
        """Return all fill_events rows for *trade_id*, ordered by id ASC."""
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT * FROM fill_events WHERE trade_id = ? ORDER BY id ASC",
            (trade_id,),
        )
        return [dict(r) for r in cursor.fetchall()]

    def update_trade_attribution(self, trade_id: str, **kwargs: Any) -> bool:
        """Update attribution columns on an existing trade row.

        Only the keys present in *kwargs* are written.  Accepted keys:
        ``exit_slippage_bps``, ``maker_exit``, ``hold_bars``,
        ``exit_reason_enum``, ``fees_usd``, ``funding_usd``,
        ``exit_slippage_bps``.

        Returns True if at least one row was updated.
        """
        _ALLOWED = {
            "cascade_level", "confidence_bucket", "regime_at_entry",
            "atr_at_entry", "entry_slippage_bps", "exit_slippage_bps",
            "maker_entry", "maker_exit", "fees_usd", "funding_usd",
            "hold_bars", "exit_reason_enum", "consensus_adj", "funding_adj",
        }
        safe = {k: v for k, v in kwargs.items() if k in _ALLOWED}
        if not safe:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in safe)
        values = list(safe.values()) + [trade_id]
        conn = self._get_conn()
        cursor = conn.execute(
            f"UPDATE trades SET {set_clause} WHERE trade_id = ?",  # noqa: S608
            values,
        )
        conn.commit()
        return cursor.rowcount > 0

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
    # Trailing Stops (ACID-safe persistence for restart survival)
    # -----------------------------------------------------------------------

    def upsert_trailing_stop(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        best_price: float,
        atr_4h: float,
        activated: bool,
        strategy_name: str = "",
        take_profit: float = 0.0,
        tp_pending: bool = False,
    ) -> None:
        """Insert or update a trailing stop record for *symbol*."""
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT OR REPLACE INTO trailing_stops (
                symbol, direction, entry_price, best_price, atr_4h,
                activated, strategy_name, take_profit, tp_pending, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol,
                direction,
                entry_price,
                best_price,
                atr_4h,
                int(activated),
                strategy_name,
                take_profit,
                int(tp_pending),
                now,
            ),
        )
        conn.commit()
        self._mirror_enqueue("trailing_stops", {
            "symbol": symbol,
            "direction": direction,
            "entry_price": entry_price,
            "best_price": best_price,
            "atr_4h": atr_4h,
            "activated": bool(activated),
            "strategy_name": strategy_name,
            "take_profit": take_profit,
            "tp_pending": bool(tp_pending),
            "updated_at": now,
            "deleted": False,
        })

    def get_all_trailing_stops(self) -> dict[str, dict]:
        """Return all trailing stop rows as ``{symbol: {field: value, ...}}``."""
        conn = self._get_conn()
        cursor = conn.execute("SELECT * FROM trailing_stops")
        result: dict[str, dict] = {}
        for row in cursor.fetchall():
            d = dict(row)
            d["activated"] = bool(d["activated"])
            d["tp_pending"] = bool(d.get("tp_pending", 0))
            result[d["symbol"]] = d
        return result

    def delete_trailing_stop(self, symbol: str) -> None:
        """Remove a trailing stop record (position closed)."""
        conn = self._get_conn()
        conn.execute("DELETE FROM trailing_stops WHERE symbol = ?", (symbol,))
        conn.commit()
        self._mirror_enqueue("trailing_stops", {
            "symbol": symbol,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "deleted": True,
        })

    def delete_all_trailing_stops(self) -> None:
        """Remove all trailing stop records."""
        conn = self._get_conn()
        conn.execute("DELETE FROM trailing_stops")
        conn.commit()

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
    # Baseline epoch (Phase 2C)
    # -----------------------------------------------------------------------
    #
    # The "baseline" marks the start of a reporting era.  Historical data is
    # never deleted or mutated.  Reporting code may pass the baseline start
    # timestamp as a ``since=`` filter to any forensic query to get a
    # "since-baseline" view while leaving the all-history view unchanged.
    #
    # Storage: key-value rows in ``system_state`` under the ``baseline.*``
    # namespace.  No new schema or migration.

    _BASELINE_KEYS = (
        "baseline.current_mode",
        "baseline.started_at_utc",
        "baseline.start_balance_usdt",
        "baseline.notes",
        "baseline.set_at_utc",
    )
    _BASELINE_PREVIOUS_KEY = "baseline.previous_json"

    def set_baseline(self, baseline: "BaselineRow") -> None:
        """Persist a new baseline.

        Any existing baseline is archived to ``baseline.previous_json``
        (overwriting only the previous archive slot — one level of undo).
        This method is idempotent w.r.t. the current baseline: calling it
        twice with the same row simply re-archives.

        Does NOT delete, mutate, or otherwise touch any ``trades``,
        ``cycle_history``, or ``decision_log`` row.
        """
        existing = self.get_baseline()
        if existing is not None:
            archive_payload = {
                "current_mode": existing.current_mode,
                "started_at_utc": existing.started_at_utc.isoformat(),
                "start_balance_usdt": str(existing.start_balance_usdt),
                "notes": existing.notes,
                "set_at_utc": existing.set_at_utc.isoformat(),
                "archived_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            self.set_state(self._BASELINE_PREVIOUS_KEY, json.dumps(archive_payload))

        self.set_state("baseline.current_mode", baseline.current_mode)
        self.set_state("baseline.started_at_utc", baseline.started_at_utc.isoformat())
        self.set_state("baseline.start_balance_usdt", str(baseline.start_balance_usdt))
        self.set_state("baseline.notes", baseline.notes)
        self.set_state("baseline.set_at_utc", baseline.set_at_utc.isoformat())
        logger.info(
            "Baseline set: mode=%s started_at=%s start_balance=%s",
            baseline.current_mode,
            baseline.started_at_utc.isoformat(),
            baseline.start_balance_usdt,
        )

    def get_baseline(self) -> "BaselineRow | None":
        """Return the current baseline, or ``None`` if none has been set."""
        mode = self.get_state("baseline.current_mode")
        started = self.get_state("baseline.started_at_utc")
        start_balance = self.get_state("baseline.start_balance_usdt")
        if mode is None or started is None or start_balance is None:
            return None
        set_at = self.get_state("baseline.set_at_utc")
        notes = self.get_state("baseline.notes") or ""
        try:
            started_dt = datetime.fromisoformat(started)
            set_at_dt = (
                datetime.fromisoformat(set_at)
                if set_at is not None
                else datetime.now(timezone.utc)
            )
            return BaselineRow(
                current_mode=mode,
                started_at_utc=started_dt,
                start_balance_usdt=Decimal(start_balance),
                notes=notes,
                set_at_utc=set_at_dt,
            )
        except (ValueError, TypeError) as exc:
            logger.warning("Corrupt baseline row in system_state: %s", exc)
            return None

    def clear_baseline(self) -> bool:
        """Remove the current baseline keys.

        Archives the cleared row into ``baseline.previous_json`` first so
        it can be restored via ``restore_previous_baseline``.
        Returns True if a baseline was cleared, False if none was set.
        The ``baseline.previous_json`` archive slot is preserved by clear.
        """
        existing = self.get_baseline()
        if existing is None:
            return False
        archive_payload = {
            "current_mode": existing.current_mode,
            "started_at_utc": existing.started_at_utc.isoformat(),
            "start_balance_usdt": str(existing.start_balance_usdt),
            "notes": existing.notes,
            "set_at_utc": existing.set_at_utc.isoformat(),
            "archived_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        self.set_state(self._BASELINE_PREVIOUS_KEY, json.dumps(archive_payload))
        conn = self._get_conn()
        for key in self._BASELINE_KEYS:
            conn.execute("DELETE FROM system_state WHERE key = ?", (key,))
        conn.commit()
        logger.info("Baseline cleared (archived to %s)", self._BASELINE_PREVIOUS_KEY)
        return True

    def get_previous_baseline(self) -> dict[str, Any] | None:
        """Return the previously-archived baseline payload, if any."""
        raw = self.get_state(self._BASELINE_PREVIOUS_KEY)
        if raw is None:
            return None
        try:
            data: dict[str, Any] = json.loads(raw)
            return data
        except json.JSONDecodeError:
            logger.warning("Corrupt baseline.previous_json")
            return None

    def restore_previous_baseline(self) -> bool:
        """Restore the archived baseline as the current one.

        Returns True on success, False if no archive exists.
        """
        prev = self.get_previous_baseline()
        if prev is None:
            return False
        try:
            row = BaselineRow(
                current_mode=prev["current_mode"],
                started_at_utc=datetime.fromisoformat(prev["started_at_utc"]),
                start_balance_usdt=Decimal(prev["start_balance_usdt"]),
                notes=prev.get("notes", ""),
                set_at_utc=datetime.fromisoformat(prev["set_at_utc"]),
            )
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("Cannot restore previous baseline: %s", exc)
            return False
        self.set_baseline(row)
        return True

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

    def migrate_daily_state(self, json_path: Path | str) -> bool:
        """Import daily state from a JSON file into system_state table."""
        path = Path(json_path)
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.set_state("daily.raw", json.dumps(data))
            for key in ("date", "start_of_day_balance", "last_daily_report",
                        "updated_at"):
                if key in data:
                    self.set_state(f"daily.{key}", str(data[key]))
            logger.info("Migrated daily state from %s", path)
            return True
        except (json.JSONDecodeError, KeyError) as exc:
            logger.error("Failed to migrate daily state: %s", exc)
            return False

    def migrate_trailing_stops_json(self, json_path: Path | str) -> int:
        """Import trailing-stop state from legacy JSON into the trailing_stops
        table. Returns the number of rows upserted."""
        path = Path(json_path)
        if not path.exists():
            return 0
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to read trailing stops JSON: %s", exc)
            return 0
        count = 0
        for symbol, state in raw.items():
            try:
                self.upsert_trailing_stop(
                    symbol=symbol,
                    direction=str(state.get("direction", "")),
                    entry_price=float(state.get("entry_price", 0.0)),
                    best_price=float(state.get("best_price", 0.0)),
                    atr_4h=float(state.get("atr_4h", 0.0)),
                    activated=bool(state.get("activated", False)),
                    strategy_name=str(state.get("strategy_name", "")),
                    take_profit=float(state.get("take_profit", 0.0)),
                    tp_pending=bool(state.get("tp_pending", False)),
                )
                count += 1
            except (ValueError, TypeError) as exc:
                logger.warning("Skip trailing stop %s: %s", symbol, exc)
        logger.info("Migrated %d trailing stops from %s", count, path)
        return count

    def migrate_from_audit_trail(self, old_db_path: Path | str) -> int:
        """Import rows from a standalone audit_trail.db into this DB.

        Returns the number of rows imported. Idempotent via INSERT OR IGNORE.
        """
        old_path = Path(old_db_path)
        if not old_path.exists():
            return 0
        old_conn = sqlite3.connect(str(old_path))
        old_conn.row_factory = sqlite3.Row
        try:
            cursor = old_conn.execute("SELECT * FROM audit_trail")
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            old_conn.close()
            return 0
        conn = self._get_conn()
        count = 0
        for row in rows:
            d = dict(row)
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO audit_trail (
                        audit_id, trade_id, timestamp, symbol, direction,
                        strategy_name, regime, decision, report_json, created_at
                    ) VALUES (
                        :audit_id, :trade_id, :timestamp, :symbol, :direction,
                        :strategy_name, :regime, :decision, :report_json, :created_at
                    )
                    """,
                    d,
                )
                count += 1
            except (sqlite3.IntegrityError, KeyError) as exc:
                logger.warning("Skip audit %s: %s", d.get("audit_id"), exc)
        conn.commit()
        old_conn.close()
        logger.info("Migrated %d audit rows from %s", count, old_path)
        return count

    # -----------------------------------------------------------------------
    # Audit trail (merged from standalone audit_trail.db)
    # -----------------------------------------------------------------------

    def insert_audit(
        self,
        audit_id: str,
        trade_id: str | None,
        timestamp: str,
        symbol: str | None,
        direction: str | None,
        strategy_name: str | None,
        regime: str | None,
        decision: str | None,
        report_json: str,
    ) -> None:
        """Insert (or replace) an audit trail row."""
        conn = self._get_conn()
        created_at = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT OR REPLACE INTO audit_trail (
                audit_id, trade_id, timestamp, symbol, direction,
                strategy_name, regime, decision, report_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id, trade_id, timestamp, symbol, direction,
                strategy_name, regime, decision, report_json,
                created_at,
            ),
        )
        conn.commit()
        self._mirror_enqueue("audit_trail", {
            "audit_id": audit_id,
            "trade_id": trade_id,
            "timestamp": timestamp,
            "symbol": symbol,
            "direction": direction,
            "strategy_name": strategy_name,
            "regime": regime,
            "decision": decision,
            "report_json": report_json,
            "created_at": created_at,
        })

    def get_audit_by_trade(self, trade_id: str) -> str | None:
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT report_json FROM audit_trail WHERE trade_id = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (trade_id,),
        )
        row = cursor.fetchone()
        return row["report_json"] if row else None

    def get_recent_audits(self, limit: int = 10) -> list[str]:
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT report_json FROM audit_trail "
            "ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        return [row["report_json"] for row in cursor.fetchall()]
