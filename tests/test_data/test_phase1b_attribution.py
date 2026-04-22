"""Phase 1B trade attribution and fill_events tests.

Covers:
- Schema: 14 new attribution columns present after fresh DB creation
- Migration: existing DB without columns gets them added
- fill_events table creation and index
- fill_events insert + retrieve by trade_id
- TradeEntry accepts all 14 new fields
- record_trade_entry returns trade_id
- record_trade_entry populates attribution fields
- update_trade_exit accepts Phase 1B params, returns trade_id
- update_trade_exit populates hold_bars / exit_reason_enum
- Backward compat: _row_to_trade_entry handles rows missing new columns
- Full pipeline: entry -> fill_events -> update exit -> attribution query
"""

from __future__ import annotations

import sqlite3
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from src.data.database import DatabaseManager
from src.memory.trade_journal import TradeEntry, TradeJournal, _row_to_trade_entry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> DatabaseManager:
    return DatabaseManager(db_path=tmp_path / "test.db")


def _make_journal(tmp_path: Path) -> TradeJournal:
    return TradeJournal(db_path=tmp_path / "test.db")


_ATTRIBUTION_COLUMNS = [
    "cascade_level",
    "confidence_bucket",
    "regime_at_entry",
    "atr_at_entry",
    "entry_slippage_bps",
    "exit_slippage_bps",
    "maker_entry",
    "maker_exit",
    "fees_usd",
    "funding_usd",
    "hold_bars",
    "exit_reason_enum",
    "consensus_adj",
    "funding_adj",
]


# ---------------------------------------------------------------------------
# Schema: fresh DB via DatabaseManager
# ---------------------------------------------------------------------------


def test_fresh_db_has_attribution_columns(tmp_path: Path) -> None:
    """A newly created DB must have all 14 attribution columns."""
    db = _make_db(tmp_path)
    conn = db._get_conn()
    cursor = conn.execute("PRAGMA table_info(trades)")
    cols = {row["name"] for row in cursor.fetchall()}
    for col in _ATTRIBUTION_COLUMNS:
        assert col in cols, f"Missing column: {col}"


def test_fresh_db_has_fill_events_table(tmp_path: Path) -> None:
    """A newly created DB must have the fill_events table and index."""
    db = _make_db(tmp_path)
    conn = db._get_conn()
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='fill_events'"
    )
    assert cursor.fetchone() is not None, "fill_events table not created"
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_fill_events_trade'"
    )
    assert cursor.fetchone() is not None, "idx_fill_events_trade index not created"


# ---------------------------------------------------------------------------
# Schema: fresh DB via TradeJournal
# ---------------------------------------------------------------------------


def test_trade_journal_fresh_db_has_attribution_columns(tmp_path: Path) -> None:
    """TradeJournal on a fresh DB must also create all 14 columns."""
    journal = _make_journal(tmp_path)
    conn = journal._get_conn()
    cursor = conn.execute("PRAGMA table_info(trades)")
    cols = {row["name"] for row in cursor.fetchall()}
    for col in _ATTRIBUTION_COLUMNS:
        assert col in cols, f"TradeJournal fresh: missing column {col}"


# ---------------------------------------------------------------------------
# Migration: existing DB without attribution columns
# ---------------------------------------------------------------------------


def test_migration_adds_attribution_columns_to_old_db(tmp_path: Path) -> None:
    """DatabaseManager migration must add Phase 1B columns to a pre-existing DB."""
    db_path = tmp_path / "old.db"
    # Create an old-style DB with only the original columns
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
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
            lessons TEXT DEFAULT ''
        )
        """
    )
    conn.commit()
    conn.close()

    # Open with DatabaseManager — should trigger migration
    db = DatabaseManager(db_path=db_path)
    conn2 = db._get_conn()
    cursor = conn2.execute("PRAGMA table_info(trades)")
    cols = {row["name"] for row in cursor.fetchall()}
    for col in _ATTRIBUTION_COLUMNS:
        assert col in cols, f"Migration did not add column: {col}"


def test_migration_creates_fill_events_if_missing(tmp_path: Path) -> None:
    """DatabaseManager migration creates fill_events table if not present."""
    db_path = tmp_path / "old.db"
    # Create an old-style DB (full original schema, no Phase 1B cols, no fill_events)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE trades (
            trade_id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL DEFAULT '',
            direction TEXT NOT NULL DEFAULT '',
            entry_price TEXT NOT NULL DEFAULT '0',
            exit_price TEXT,
            size TEXT NOT NULL DEFAULT '0',
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
            lessons TEXT DEFAULT ''
        )
        """
    )
    conn.commit()
    conn.close()

    db = DatabaseManager(db_path=db_path)
    conn2 = db._get_conn()
    cursor = conn2.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='fill_events'"
    )
    assert cursor.fetchone() is not None


def test_trade_journal_migration_adds_columns_to_old_db(tmp_path: Path) -> None:
    """TradeJournal migration must add Phase 1B columns to a pre-existing DB."""
    db_path = tmp_path / "old_journal.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE trades (
            trade_id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL DEFAULT '',
            direction TEXT NOT NULL DEFAULT '',
            entry_price TEXT NOT NULL DEFAULT '0',
            exit_price TEXT,
            size TEXT NOT NULL DEFAULT '0',
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
            lessons TEXT DEFAULT ''
        )
        """
    )
    conn.commit()
    conn.close()

    journal = TradeJournal(db_path=db_path)
    conn2 = journal._get_conn()
    cursor = conn2.execute("PRAGMA table_info(trades)")
    cols = {row["name"] for row in cursor.fetchall()}
    for col in _ATTRIBUTION_COLUMNS:
        assert col in cols, f"TradeJournal migration did not add: {col}"


# ---------------------------------------------------------------------------
# TradeEntry: new fields with defaults
# ---------------------------------------------------------------------------


def test_trade_entry_default_attribution_fields() -> None:
    """TradeEntry must be constructible without new fields (defaults)."""
    entry = TradeEntry(
        symbol="ETH/USDT:USDT",
        direction="long",
        entry_price=Decimal("2000"),
        size=Decimal("0.01"),
    )
    assert entry.cascade_level == ""
    assert entry.confidence_bucket == ""
    assert entry.entry_slippage_bps == 0.0
    assert entry.maker_entry == 0
    assert entry.hold_bars == 0
    assert entry.exit_reason_enum == ""
    assert entry.consensus_adj == 0.0
    assert entry.funding_adj == 0.0


def test_trade_entry_accepts_attribution_values() -> None:
    """TradeEntry must accept all 14 Phase 1B attribution values."""
    entry = TradeEntry(
        symbol="SOL/USDT:USDT",
        direction="short",
        entry_price=Decimal("150"),
        size=Decimal("1"),
        cascade_level="4h_flip",
        confidence_bucket="70-85",
        regime_at_entry="TRENDING",
        atr_at_entry="3.5",
        entry_slippage_bps=2.5,
        exit_slippage_bps=1.1,
        maker_entry=1,
        maker_exit=0,
        fees_usd="0.075",
        funding_usd="0",
        hold_bars=24,
        exit_reason_enum="trail",
        consensus_adj=-5.0,
        funding_adj=2.0,
    )
    assert entry.cascade_level == "4h_flip"
    assert entry.confidence_bucket == "70-85"
    assert entry.maker_entry == 1
    assert entry.hold_bars == 24
    assert entry.exit_reason_enum == "trail"
    assert entry.consensus_adj == -5.0


# ---------------------------------------------------------------------------
# record_trade_entry returns trade_id + populates attribution
# ---------------------------------------------------------------------------


def test_record_trade_entry_returns_trade_id(tmp_path: Path) -> None:
    """record_trade_entry must return a non-empty str (the trade_id)."""
    journal = _make_journal(tmp_path)
    trade_id = journal.record_trade_entry({
        "pair": "ETH/USDT:USDT",
        "direction": "long",
        "entry_price": 2000.0,
        "size": 0.01,
        "leverage": 5,
        "stop_loss": 1940.0,
        "take_profit": 2120.0,
        "strategy": "supertrend_trend",
        "confidence": 75.0,
        "regime": "TRENDING",
    })
    assert isinstance(trade_id, str)
    assert len(trade_id) > 0


def test_record_trade_entry_populates_attribution_fields(tmp_path: Path) -> None:
    """record_trade_entry must persist all Phase 1B attribution fields."""
    journal = _make_journal(tmp_path)
    trade_id = journal.record_trade_entry({
        "pair": "SOL/USDT:USDT",
        "direction": "long",
        "entry_price": 150.0,
        "size": 0.1,
        "leverage": 5,
        "cascade_level": "4h_flip",
        "confidence_bucket": "70-85",
        "regime_at_entry": "TRENDING",
        "atr_at_entry": "3.5",
        "entry_slippage_bps": 2.5,
        "maker_entry": 1,
        "fees_usd": "0.075",
        "consensus_adj": -5.0,
        "funding_adj": 2.0,
    })

    conn = journal._get_conn()
    row = dict(conn.execute(
        "SELECT * FROM trades WHERE trade_id = ?", (trade_id,)
    ).fetchone())

    assert row["cascade_level"] == "4h_flip"
    assert row["confidence_bucket"] == "70-85"
    assert row["regime_at_entry"] == "TRENDING"
    assert row["maker_entry"] == 1
    assert abs(row["entry_slippage_bps"] - 2.5) < 0.001
    assert abs(row["consensus_adj"] - (-5.0)) < 0.001
    assert abs(row["funding_adj"] - 2.0) < 0.001


# ---------------------------------------------------------------------------
# update_trade_exit returns trade_id + populates exit attribution
# ---------------------------------------------------------------------------


def test_update_trade_exit_returns_trade_id(tmp_path: Path) -> None:
    """update_trade_exit must return the trade_id string when a match exists."""
    journal = _make_journal(tmp_path)
    trade_id = journal.record_trade_entry({
        "pair": "ETH/USDT:USDT",
        "direction": "long",
        "entry_price": 2000.0,
        "size": 0.01,
        "leverage": 5,
    })
    returned = journal.update_trade_exit(
        symbol="ETH/USDT:USDT",
        exit_price=Decimal("2100"),
        pnl=Decimal("10"),
        pnl_pct=Decimal("5.0"),
        reason="time_exit",
    )
    assert returned == trade_id


def test_update_trade_exit_populates_hold_bars_and_enum(tmp_path: Path) -> None:
    """update_trade_exit must persist hold_bars and exit_reason_enum."""
    journal = _make_journal(tmp_path)
    trade_id = journal.record_trade_entry({
        "pair": "SOL/USDT:USDT",
        "direction": "short",
        "entry_price": 150.0,
        "size": 0.1,
        "leverage": 3,
    })
    journal.update_trade_exit(
        symbol="SOL/USDT:USDT",
        exit_price=Decimal("145"),
        pnl=Decimal("5"),
        pnl_pct=Decimal("3.5"),
        reason="trailing_stop",
        hold_bars=36,
        exit_reason_enum="trail",
        exit_slippage_bps=1.5,
        maker_exit=0,
    )
    conn = journal._get_conn()
    row = dict(conn.execute(
        "SELECT hold_bars, exit_reason_enum, exit_slippage_bps, maker_exit "
        "FROM trades WHERE trade_id = ?", (trade_id,)
    ).fetchone())

    assert row["hold_bars"] == 36
    assert row["exit_reason_enum"] == "trail"
    assert abs(row["exit_slippage_bps"] - 1.5) < 0.001
    assert row["maker_exit"] == 0


def test_update_trade_exit_no_match_returns_none(tmp_path: Path) -> None:
    """update_trade_exit returns None when no open trade matches."""
    journal = _make_journal(tmp_path)
    result = journal.update_trade_exit(
        symbol="XRP/USDT:USDT",
        exit_price=Decimal("0.5"),
        pnl=Decimal("-1"),
        pnl_pct=Decimal("-2"),
        reason="sl_hit",
    )
    assert result is None


# ---------------------------------------------------------------------------
# fill_events insert and retrieve
# ---------------------------------------------------------------------------


def test_fill_events_insert_and_retrieve(tmp_path: Path) -> None:
    """insert_fill_event must persist a row retrievable by trade_id."""
    db = _make_db(tmp_path)
    journal = TradeJournal(db_path=tmp_path / "test.db")

    trade_id = journal.record_trade_entry({
        "pair": "ETH/USDT:USDT",
        "direction": "long",
        "entry_price": 2000.0,
        "size": 0.01,
        "leverage": 5,
    })

    row_id = db.insert_fill_event(
        trade_id=trade_id,
        side_of_trade="entry",
        timestamp_utc="2026-04-10T12:00:00+00:00",
        order_type="limit_gtx",
        requested_price="2000.00",
        fill_price="2000.10",
        filled_qty="0.01",
        is_maker=1,
        fees_usd="0.004",
        client_order_id="cq_abc123",
        exchange_order_id="9876543210",
    )

    assert row_id > 0

    rows = db.get_fill_events_for_trade(trade_id)
    assert len(rows) == 1
    r = rows[0]
    assert r["trade_id"] == trade_id
    assert r["side_of_trade"] == "entry"
    assert r["order_type"] == "limit_gtx"
    assert r["fill_price"] == "2000.10"
    assert r["is_maker"] == 1
    assert r["fees_usd"] == "0.004"
    assert r["client_order_id"] == "cq_abc123"


def test_fill_events_multiple_legs(tmp_path: Path) -> None:
    """Both entry and exit fill_events rows can be stored for one trade_id."""
    db = _make_db(tmp_path)
    journal = TradeJournal(db_path=tmp_path / "test.db")

    trade_id = journal.record_trade_entry({
        "pair": "SOL/USDT:USDT",
        "direction": "long",
        "entry_price": 150.0,
        "size": 0.5,
        "leverage": 5,
    })

    db.insert_fill_event(
        trade_id=trade_id,
        side_of_trade="entry",
        timestamp_utc="2026-04-10T12:00:00+00:00",
        order_type="market",
        requested_price="150.00",
        fill_price="150.05",
        filled_qty="0.5",
        is_maker=0,
        fees_usd="0.04",
    )
    db.insert_fill_event(
        trade_id=trade_id,
        side_of_trade="exit",
        timestamp_utc="2026-04-10T18:00:00+00:00",
        order_type="market",
        requested_price="155.00",
        fill_price="155.00",
        filled_qty="0.5",
        is_maker=0,
        fees_usd="0.04",
    )

    rows = db.get_fill_events_for_trade(trade_id)
    assert len(rows) == 2
    sides = [r["side_of_trade"] for r in rows]
    assert "entry" in sides
    assert "exit" in sides


def test_fill_events_empty_for_unknown_trade(tmp_path: Path) -> None:
    """get_fill_events_for_trade returns [] for a non-existent trade_id."""
    db = _make_db(tmp_path)
    rows = db.get_fill_events_for_trade("nonexistent_trade_id")
    assert rows == []


# ---------------------------------------------------------------------------
# update_trade_attribution
# ---------------------------------------------------------------------------


def test_update_trade_attribution(tmp_path: Path) -> None:
    """update_trade_attribution must update allowed attribution columns."""
    db = _make_db(tmp_path)
    journal = TradeJournal(db_path=tmp_path / "test.db")

    trade_id = journal.record_trade_entry({
        "pair": "ETH/USDT:USDT",
        "direction": "long",
        "entry_price": 2000.0,
        "size": 0.01,
        "leverage": 5,
    })

    result = db.update_trade_attribution(
        trade_id,
        exit_slippage_bps=3.2,
        maker_exit=0,
        hold_bars=48,
        exit_reason_enum="time_exit",
    )
    assert result is True

    conn = db._get_conn()
    row = dict(conn.execute(
        "SELECT exit_slippage_bps, maker_exit, hold_bars, exit_reason_enum "
        "FROM trades WHERE trade_id = ?", (trade_id,)
    ).fetchone())
    assert abs(row["exit_slippage_bps"] - 3.2) < 0.001
    assert row["maker_exit"] == 0
    assert row["hold_bars"] == 48
    assert row["exit_reason_enum"] == "time_exit"


def test_update_trade_attribution_rejects_unknown_keys(tmp_path: Path) -> None:
    """update_trade_attribution ignores keys not in the allowed list."""
    db = _make_db(tmp_path)
    result = db.update_trade_attribution(
        "nonexistent", direction="hacked", exit_reason_enum="sl_hit"
    )
    # No rows matched, but should not raise
    assert result is False


# ---------------------------------------------------------------------------
# Backward compat: _row_to_trade_entry with missing Phase 1B columns
# ---------------------------------------------------------------------------


def test_row_to_trade_entry_backward_compat() -> None:
    """_row_to_trade_entry must not raise when Phase 1B columns are absent."""
    row: dict = {
        "trade_id": "abc123",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "symbol": "ETH/USDT:USDT",
        "direction": "long",
        "entry_price": "2000",
        "exit_price": None,
        "size": "0.01",
        "leverage": 5,
        "pnl": None,
        "pnl_pct": None,
        "strategy": "supertrend",
        "regime": "TRENDING",
        "confidence": "75",
        "stop_loss": "1940",
        "take_profit": "2120",
        "duration": None,
        "fees": "0",
        "slippage": "0",
        "reasoning": "",
        "lessons": "",
        "mode": "mainnet",
        "signal_tag": "4h_flip",
        "exit_reason": "",
        # Phase 1B columns intentionally absent (simulates old rows)
    }
    entry = _row_to_trade_entry(row)
    assert entry.cascade_level == ""
    assert entry.maker_entry == 0
    assert entry.hold_bars == 0
    assert entry.consensus_adj == 0.0


# ---------------------------------------------------------------------------
# Full pipeline: entry -> fill_events -> update exit -> attribution query
# ---------------------------------------------------------------------------


def test_full_pipeline(tmp_path: Path) -> None:
    """End-to-end: record entry, insert fill_events, update exit, query."""
    db = _make_db(tmp_path)
    journal = TradeJournal(db_path=tmp_path / "test.db")

    # 1. Record entry with attribution
    trade_id = journal.record_trade_entry({
        "pair": "ETH/USDT:USDT",
        "direction": "long",
        "entry_price": 2000.0,
        "size": 0.05,
        "leverage": 5,
        "stop_loss": 1940.0,
        "take_profit": 2120.0,
        "strategy": "supertrend_trend",
        "confidence": 78.0,
        "cascade_level": "4h_flip",
        "confidence_bucket": "70-85",
        "regime_at_entry": "TRENDING",
        "atr_at_entry": "42.5",
        "entry_slippage_bps": 1.2,
        "maker_entry": 0,
        "fees_usd": "0.05",
        "consensus_adj": 3.0,
        "funding_adj": -2.0,
    })

    # 2. Insert entry fill_event
    db.insert_fill_event(
        trade_id=trade_id,
        side_of_trade="entry",
        timestamp_utc="2026-04-10T10:00:00+00:00",
        order_type="market",
        requested_price="2000.00",
        fill_price="2000.24",
        filled_qty="0.05",
        is_maker=0,
        fees_usd="0.05",
        client_order_id="cq_entry001",
        exchange_order_id="111222333",
    )

    # 3. Update exit with attribution
    updated_id = journal.update_trade_exit(
        symbol="ETH/USDT:USDT",
        exit_price=Decimal("2100"),
        pnl=Decimal("5"),
        pnl_pct=Decimal("25"),
        duration=24.0,
        reason="time_exit",
        hold_bars=24,
        exit_reason_enum="time_exit",
        exit_slippage_bps=0.8,
        maker_exit=0,
    )
    assert updated_id == trade_id

    # 4. Insert exit fill_event
    db.insert_fill_event(
        trade_id=trade_id,
        side_of_trade="exit",
        timestamp_utc="2026-04-11T10:00:00+00:00",
        order_type="market",
        requested_price="2100.00",
        fill_price="2100.00",
        filled_qty="0.05",
        is_maker=0,
        fees_usd="0.05",
    )

    # 5. Query fill_events — expect entry + exit
    events = db.get_fill_events_for_trade(trade_id)
    assert len(events) == 2
    assert {e["side_of_trade"] for e in events} == {"entry", "exit"}

    # 6. Query trade attribution columns
    conn = db._get_conn()
    row = dict(conn.execute(
        "SELECT * FROM trades WHERE trade_id = ?", (trade_id,)
    ).fetchone())
    assert row["cascade_level"] == "4h_flip"
    assert row["confidence_bucket"] == "70-85"
    assert row["hold_bars"] == 24
    assert row["exit_reason_enum"] == "time_exit"
    assert abs(row["entry_slippage_bps"] - 1.2) < 0.001
    assert abs(row["exit_slippage_bps"] - 0.8) < 0.001
