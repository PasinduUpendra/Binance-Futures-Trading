"""
Tests for ForensicQueries — Phase 1C canonical queries.

All tests use an in-memory SQLite database seeded with fixture data.
These are pure read-only tests — no exchange, no async, no side effects.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.data.database import DatabaseManager
from src.reporting.forensic_queries import (
    ALL_QUERIES,
    ForensicQueries,
    Q1_CASCADE_CONVERSION_FUNNEL,
    Q2_PER_CASCADE_EXPECTANCY,
    Q3_PER_REGIME_EXPECTANCY,
    Q4_MAKER_TAKER_PNL,
    Q5_SLIPPAGE_COST,
    Q6_REJECTION_DISTRIBUTION,
    Q7_EXIT_REASON_MIX,
    Q8_SYMBOL_PNL_WITH_DRAG,
    Q9_CONFIDENCE_BUCKET_WIN_RATE,
    Q10_FUNDING_FILTER_IMPACT,
    Q11_CONSENSUS_ADJ_IMPACT,
    Q12_CYCLE_LATENCY,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path):
    """Return a fresh in-memory DatabaseManager."""
    return DatabaseManager(tmp_path / "test.db")


@pytest.fixture()
def db_with_data(db):
    """Seed the database with minimal fixture rows for all 12 queries."""
    _seed_cycle_history(db)
    _seed_decision_log(db)
    _seed_trades(db)
    return db


def _seed_cycle_history(db: DatabaseManager) -> None:
    from src.data.database import CycleHistoryRow

    for i in range(3):
        row = CycleHistoryRow(
            cycle_number=i + 1,
            timestamp=datetime(2026, 4, 22, i, 0, 0, tzinfo=timezone.utc),
            circuit_breaker_level="GREEN",
            balance=Decimal("68.33"),
            regime="TRENDING",
            signal_generated=True,
            trade_placed=True,
            duration_seconds=12.5 + i,
        )
        db.store_cycle(row)


def _seed_decision_log(db: DatabaseManager) -> None:
    """Insert decision_log rows covering Q1, Q6, Q10 fixtures."""
    # Get cycle IDs (1, 2, 3)
    for cycle_id in range(1, 4):
        # signal_generate — pass (flip), pass (continuation)
        db.insert_decision_log(
            cycle_id=cycle_id,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            symbol="ETH/USDT:USDT",
            stage="signal_generate",
            outcome="pass",
            reason="flip detected",
            cascade_level="flip",
            confidence=72.0,
            regime="TRENDING",
        )
        # confidence_gate — pass
        db.insert_decision_log(
            cycle_id=cycle_id,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            symbol="ETH/USDT:USDT",
            stage="confidence_gate",
            outcome="pass",
            reason="confidence 72 >= 45",
            cascade_level="flip",
            confidence=72.0,
        )
        # funding_filter — one pass, one reject
        outcome = "pass" if cycle_id % 2 == 0 else "reject"
        db.insert_decision_log(
            cycle_id=cycle_id,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            symbol="SOL/USDT:USDT",
            stage="funding_filter",
            outcome=outcome,
            reason="funding 0.06% > threshold" if outcome == "reject" else "funding ok",
        )
        # decision_audit — one reject
        if cycle_id == 2:
            db.insert_decision_log(
                cycle_id=cycle_id,
                timestamp_utc=datetime.now(timezone.utc).isoformat(),
                symbol="ETH/USDT:USDT",
                stage="decision_audit",
                outcome="reject",
                reason="slippage too high",
                cascade_level="flip",
                confidence=72.0,
            )
        # liquidation_buffer — pass
        db.insert_decision_log(
            cycle_id=cycle_id,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            symbol="ETH/USDT:USDT",
            stage="liquidation_buffer",
            outcome="pass",
            reason="buffer 14.2% >= 5%",
        )


def _seed_trades(db: DatabaseManager) -> None:
    """Insert closed trade rows covering Q2–Q11 fixtures."""
    conn = db._get_conn()
    import uuid

    trades = [
        {
            "trade_id": str(uuid.uuid4()),
            "timestamp": "2026-04-22T01:00:00+00:00",
            "symbol": "ETH/USDT:USDT",
            "direction": "long",
            "entry_price": "2450.00",
            "exit_price": "2600.00",
            "size": "0.01",
            "leverage": 5,
            "pnl": "1.50",
            "pnl_pct": "0.022",
            "strategy": "SupertrendTrend",
            "regime": "TRENDING",
            "confidence": "72.0",
            "cascade_level": "flip",
            "confidence_bucket": "70-85",
            "regime_at_entry": "TRENDING",
            "atr_at_entry": "1.40",
            "entry_slippage_bps": 2.1,
            "exit_slippage_bps": 3.5,
            "maker_entry": 1,
            "maker_exit": 0,
            "fees_usd": "0.12",
            "funding_usd": "-0.02",
            "hold_bars": 12,
            "exit_reason_enum": "tp_hit",
            "consensus_adj": 5.0,
            "funding_adj": 0.0,
        },
        {
            "trade_id": str(uuid.uuid4()),
            "timestamp": "2026-04-22T05:00:00+00:00",
            "symbol": "SOL/USDT:USDT",
            "direction": "long",
            "entry_price": "140.00",
            "exit_price": "132.00",
            "size": "0.1",
            "leverage": 5,
            "pnl": "-0.80",
            "pnl_pct": "-0.057",
            "strategy": "SupertrendTrend",
            "regime": "TRENDING",
            "confidence": "55.0",
            "cascade_level": "continuation",
            "confidence_bucket": "55-70",
            "regime_at_entry": "TRENDING",
            "atr_at_entry": "1.20",
            "entry_slippage_bps": 1.5,
            "exit_slippage_bps": 2.0,
            "maker_entry": 0,
            "maker_exit": 0,
            "fees_usd": "0.08",
            "funding_usd": "0.01",
            "hold_bars": 8,
            "exit_reason_enum": "sl_hit",
            "consensus_adj": -3.0,
            "funding_adj": 0.0,
        },
        {
            "trade_id": str(uuid.uuid4()),
            "timestamp": "2026-04-22T09:00:00+00:00",
            "symbol": "ETH/USDT:USDT",
            "direction": "short",
            "entry_price": "2480.00",
            "exit_price": "2430.00",
            "size": "0.01",
            "leverage": 5,
            "pnl": "0.50",
            "pnl_pct": "0.020",
            "strategy": "SupertrendTrend",
            "regime": "TRENDING",
            "confidence": "80.0",
            "cascade_level": "flip",
            "confidence_bucket": "70-85",
            "regime_at_entry": "TRENDING",
            "atr_at_entry": "1.50",
            "entry_slippage_bps": 1.0,
            "exit_slippage_bps": 1.5,
            "maker_entry": 1,
            "maker_exit": 1,
            "fees_usd": "0.06",
            "funding_usd": "0.00",
            "hold_bars": 6,
            "exit_reason_enum": "trail",
            "consensus_adj": 0.0,
            "funding_adj": 0.0,
        },
    ]

    for t in trades:
        conn.execute(
            """
            INSERT INTO trades (
                trade_id, timestamp, symbol, direction, entry_price, exit_price,
                size, leverage, pnl, pnl_pct, strategy, regime, confidence,
                cascade_level, confidence_bucket, regime_at_entry, atr_at_entry,
                entry_slippage_bps, exit_slippage_bps, maker_entry, maker_exit,
                fees_usd, funding_usd, hold_bars, exit_reason_enum, consensus_adj,
                funding_adj
            ) VALUES (
                :trade_id, :timestamp, :symbol, :direction, :entry_price, :exit_price,
                :size, :leverage, :pnl, :pnl_pct, :strategy, :regime, :confidence,
                :cascade_level, :confidence_bucket, :regime_at_entry, :atr_at_entry,
                :entry_slippage_bps, :exit_slippage_bps, :maker_entry, :maker_exit,
                :fees_usd, :funding_usd, :hold_bars, :exit_reason_enum, :consensus_adj,
                :funding_adj
            )
            """,
            t,
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Tests: empty database (graceful no-data behaviour)
# ---------------------------------------------------------------------------


class TestForensicQueriesEmpty:
    """All queries return empty lists on an empty DB — never raise."""

    def test_cascade_funnel_empty(self, db):
        fq = ForensicQueries(db)
        assert fq.cascade_conversion_funnel() == []

    def test_per_cascade_empty(self, db):
        assert ForensicQueries(db).per_cascade_expectancy() == []

    def test_per_regime_empty(self, db):
        assert ForensicQueries(db).per_regime_expectancy() == []

    def test_maker_taker_empty(self, db):
        assert ForensicQueries(db).maker_taker_pnl() == []

    def test_slippage_empty(self, db):
        rows = ForensicQueries(db).slippage_cost()
        # Query returns a single aggregate row with all NULLs on empty table
        assert isinstance(rows, list)

    def test_rejection_empty(self, db):
        assert ForensicQueries(db).rejection_distribution() == []

    def test_exit_reason_empty(self, db):
        assert ForensicQueries(db).exit_reason_mix() == []

    def test_symbol_pnl_empty(self, db):
        assert ForensicQueries(db).symbol_pnl_with_drag() == []

    def test_confidence_bucket_empty(self, db):
        assert ForensicQueries(db).confidence_bucket_win_rate() == []

    def test_funding_filter_empty(self, db):
        assert ForensicQueries(db).funding_filter_impact() == []

    def test_consensus_adj_empty(self, db):
        assert ForensicQueries(db).consensus_adj_impact() == []

    def test_cycle_latency_empty(self, db):
        assert ForensicQueries(db).cycle_latency() == []

    def test_run_all_returns_dict(self, db):
        result = ForensicQueries(db).run_all()
        assert isinstance(result, dict)
        assert len(result) == 12

    def test_all_queries_registry_complete(self, db):
        """ALL_QUERIES contains exactly 12 entries."""
        assert len(ALL_QUERIES) == 12
        names = [name for name, _ in ALL_QUERIES]
        assert "q1_cascade_conversion_funnel" in names
        assert "q12_cycle_latency" in names


# ---------------------------------------------------------------------------
# Tests: seeded database
# ---------------------------------------------------------------------------


class TestForensicQueriesWithData:
    """Verify each query returns expected rows and columns."""

    def test_q1_cascade_funnel(self, db_with_data):
        rows = ForensicQueries(db_with_data).cascade_conversion_funnel()
        assert len(rows) > 0
        # Must contain 'stage', 'outcome', 'n'
        assert "stage" in rows[0]
        assert "outcome" in rows[0]
        assert "n" in rows[0]
        stages = {r["stage"] for r in rows}
        assert "signal_generate" in stages

    def test_q2_cascade_expectancy(self, db_with_data):
        rows = ForensicQueries(db_with_data).per_cascade_expectancy()
        assert len(rows) >= 2  # flip + continuation
        buckets = {r["cascade_level"] for r in rows}
        assert "flip" in buckets
        assert "continuation" in buckets
        for r in rows:
            assert "avg_pnl" in r
            assert "win_rate" in r
            assert 0.0 <= float(r["win_rate"]) <= 1.0

    def test_q3_regime_expectancy(self, db_with_data):
        rows = ForensicQueries(db_with_data).per_regime_expectancy()
        assert len(rows) >= 1
        assert "regime_at_entry" in rows[0]
        assert "avg_pnl" in rows[0]
        assert "avg_fees" in rows[0]

    def test_q4_maker_taker(self, db_with_data):
        rows = ForensicQueries(db_with_data).maker_taker_pnl()
        assert len(rows) >= 1
        maker_vals = {r["maker_entry"] for r in rows}
        # We seeded maker=1 and maker=0
        assert 1 in maker_vals
        assert 0 in maker_vals

    def test_q5_slippage(self, db_with_data):
        rows = ForensicQueries(db_with_data).slippage_cost()
        assert len(rows) == 1
        r = rows[0]
        assert r["avg_entry_slippage_bps"] is not None
        assert r["n"] == 3

    def test_q6_rejections(self, db_with_data):
        rows = ForensicQueries(db_with_data).rejection_distribution()
        assert len(rows) >= 1
        assert "stage" in rows[0]
        assert "reason" in rows[0]
        assert "n" in rows[0]

    def test_q7_exit_reason(self, db_with_data):
        rows = ForensicQueries(db_with_data).exit_reason_mix()
        reasons = {r["exit_reason_enum"] for r in rows}
        assert "tp_hit" in reasons
        assert "sl_hit" in reasons
        assert "trail" in reasons

    def test_q8_symbol_pnl(self, db_with_data):
        rows = ForensicQueries(db_with_data).symbol_pnl_with_drag()
        symbols = {r["symbol"] for r in rows}
        assert "ETH/USDT:USDT" in symbols
        for r in rows:
            assert "total_fees" in r
            assert "total_funding" in r

    def test_q9_confidence_bucket(self, db_with_data):
        rows = ForensicQueries(db_with_data).confidence_bucket_win_rate()
        buckets = {r["confidence_bucket"] for r in rows}
        assert "70-85" in buckets
        for r in rows:
            assert 0.0 <= float(r["win_rate"]) <= 1.0

    def test_q10_funding_filter(self, db_with_data):
        rows = ForensicQueries(db_with_data).funding_filter_impact()
        outcomes = {r["outcome"] for r in rows}
        assert "pass" in outcomes or "reject" in outcomes

    def test_q11_consensus_adj(self, db_with_data):
        rows = ForensicQueries(db_with_data).consensus_adj_impact()
        buckets = {r["bucket"] for r in rows}
        assert "boosted" in buckets or "penalised" in buckets or "neutral" in buckets

    def test_q12_cycle_latency(self, db_with_data):
        rows = ForensicQueries(db_with_data).cycle_latency()
        assert len(rows) >= 1
        assert "d" in rows[0]
        assert "avg_seconds" in rows[0]
        assert rows[0]["cycle_count"] >= 1

    def test_run_all_with_data(self, db_with_data):
        result = ForensicQueries(db_with_data).run_all()
        assert len(result) == 12
        # At least some queries should return data
        non_empty = [k for k, v in result.items() if v]
        assert len(non_empty) >= 6

    # -- Guard: pnl arithmetic uses CAST correctly ----------------------------

    def test_q2_win_rate_within_bounds(self, db_with_data):
        """Win rates are always 0–1 (SQL division is 0-1 ratio, not %)."""
        rows = ForensicQueries(db_with_data).per_cascade_expectancy()
        for r in rows:
            wr = r.get("win_rate")
            if wr is not None:
                assert 0.0 <= float(wr) <= 1.0, f"win_rate out of bounds: {wr}"

    def test_q2_flip_is_profitable(self, db_with_data):
        """Flip cascade has net positive avg_pnl in our fixture (1.50 + 0.50 = 2.0, n=2)."""
        rows = ForensicQueries(db_with_data).per_cascade_expectancy()
        flip_row = next((r for r in rows if r["cascade_level"] == "flip"), None)
        assert flip_row is not None
        assert float(flip_row["avg_pnl"]) > 0

    def test_q8_total_fees_positive(self, db_with_data):
        """Fees are always positive (cost, not revenue)."""
        rows = ForensicQueries(db_with_data).symbol_pnl_with_drag()
        for r in rows:
            assert float(r["total_fees"]) >= 0


# ---------------------------------------------------------------------------
# Tests: SQL constants are valid strings (not empty, not None)
# ---------------------------------------------------------------------------


class TestQueryConstants:
    @pytest.mark.parametrize("name,sql", ALL_QUERIES)
    def test_query_string_non_empty(self, name, sql):
        assert isinstance(sql, str)
        assert len(sql.strip()) > 20, f"Query {name} looks too short"
        # Must contain SELECT
        assert "SELECT" in sql.upper()

    def test_all_queries_have_semicolon(self):
        for name, sql in ALL_QUERIES:
            assert sql.strip().endswith(";"), f"Query {name} missing terminal semicolon"
