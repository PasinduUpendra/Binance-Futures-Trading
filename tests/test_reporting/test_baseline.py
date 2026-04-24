"""
Phase 2C baseline reporting tests.

Covers:
- BaselineRow validation (frozen, UTC-aware, positive balance)
- DatabaseManager.set_baseline / get_baseline / clear_baseline / restore
- Archive semantics (previous_json)
- ForensicQueries since-filter correctness + backward compat
- AttributionReporter since-filter header and filename
- Historical trades are NOT mutated by any baseline operation
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from src.data.database import BaselineRow, DatabaseManager
from src.reporting.attribution_report import AttributionReporter
from src.reporting.forensic_queries import (
    ALL_QUERIES,
    ForensicQueries,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path):
    return DatabaseManager(tmp_path / "phase2c.db")


def _insert_trade(
    db: DatabaseManager,
    *,
    timestamp: datetime,
    pnl: str,
    cascade: str = "flip",
    bucket: str = "70-85",
    regime: str = "TRENDING",
    maker_entry: int = 1,
    fees: str = "0.10",
    funding: str = "-0.01",
    exit_reason: str = "tp_hit",
    symbol: str = "ETH/USDT:USDT",
    consensus_adj: float = 0.0,
) -> str:
    trade_id = str(uuid.uuid4())
    conn = db._get_conn()
    conn.execute(
        """
        INSERT INTO trades (
            trade_id, timestamp, symbol, direction, entry_price, exit_price,
            size, leverage, pnl, pnl_pct, strategy, regime, confidence,
            cascade_level, confidence_bucket, regime_at_entry,
            maker_entry, maker_exit, fees_usd, funding_usd,
            exit_reason_enum, consensus_adj, funding_adj,
            entry_slippage_bps, exit_slippage_bps, hold_bars, atr_at_entry
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            trade_id,
            timestamp.isoformat(),
            symbol,
            "long",
            "2000",
            "2100",
            "0.01",
            5,
            pnl,
            "1.0",
            "SupertrendTrend",
            regime,
            "80",
            cascade,
            bucket,
            regime,
            maker_entry,
            0,
            fees,
            funding,
            exit_reason,
            consensus_adj,
            0.0,
            1.0,
            1.5,
            5,
            "1.5",
        ),
    )
    conn.commit()
    return trade_id


# ---------------------------------------------------------------------------
# BaselineRow model
# ---------------------------------------------------------------------------


class TestBaselineRowModel:
    def test_frozen(self):
        row = BaselineRow(
            current_mode="m",
            started_at_utc=datetime(2026, 4, 22, tzinfo=timezone.utc),
            start_balance_usdt=Decimal("68.33"),
        )
        with pytest.raises(Exception):
            row.current_mode = "other"  # type: ignore[misc]

    def test_naive_started_at_rejected(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            BaselineRow(
                current_mode="m",
                started_at_utc=datetime(2026, 4, 22),  # naive
                start_balance_usdt=Decimal("68.33"),
            )

    def test_non_utc_normalised(self):
        # +05:30 should be normalised to UTC
        from datetime import timezone as tz, timedelta as td
        ist = tz(td(hours=5, minutes=30))
        row = BaselineRow(
            current_mode="m",
            started_at_utc=datetime(2026, 4, 22, 5, 30, 0, tzinfo=ist),
            start_balance_usdt=Decimal("68.33"),
        )
        assert row.started_at_utc.tzinfo == timezone.utc
        assert row.started_at_utc.hour == 0  # 05:30 IST == 00:00 UTC

    def test_empty_mode_rejected(self):
        with pytest.raises(Exception):
            BaselineRow(
                current_mode="",
                started_at_utc=datetime(2026, 4, 22, tzinfo=timezone.utc),
                start_balance_usdt=Decimal("68.33"),
            )


# ---------------------------------------------------------------------------
# DB: set / get / clear / restore
# ---------------------------------------------------------------------------


class TestBaselineDB:
    def test_get_returns_none_when_unset(self, db):
        assert db.get_baseline() is None
        assert db.get_previous_baseline() is None

    def test_set_then_get_roundtrip(self, db):
        row = BaselineRow(
            current_mode="mainnet_reduced_live_v1",
            started_at_utc=datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc),
            start_balance_usdt=Decimal("68.33"),
            notes="phase 2c test",
        )
        db.set_baseline(row)
        got = db.get_baseline()
        assert got is not None
        assert got.current_mode == "mainnet_reduced_live_v1"
        assert got.started_at_utc == row.started_at_utc
        assert got.start_balance_usdt == Decimal("68.33")
        assert got.notes == "phase 2c test"

    def test_set_archives_previous(self, db):
        first = BaselineRow(
            current_mode="era_1",
            started_at_utc=datetime(2026, 4, 1, tzinfo=timezone.utc),
            start_balance_usdt=Decimal("60.00"),
        )
        second = BaselineRow(
            current_mode="era_2",
            started_at_utc=datetime(2026, 4, 22, tzinfo=timezone.utc),
            start_balance_usdt=Decimal("68.33"),
        )
        db.set_baseline(first)
        db.set_baseline(second)
        current = db.get_baseline()
        assert current is not None
        assert current.current_mode == "era_2"
        prev = db.get_previous_baseline()
        assert prev is not None
        assert prev["current_mode"] == "era_1"
        assert prev["start_balance_usdt"] == "60.00"

    def test_clear_removes_current_and_archives(self, db):
        row = BaselineRow(
            current_mode="era_x",
            started_at_utc=datetime(2026, 4, 22, tzinfo=timezone.utc),
            start_balance_usdt=Decimal("68.33"),
        )
        db.set_baseline(row)
        assert db.clear_baseline() is True
        assert db.get_baseline() is None
        # Previous archive now contains era_x
        prev = db.get_previous_baseline()
        assert prev is not None
        assert prev["current_mode"] == "era_x"

    def test_clear_returns_false_if_none_set(self, db):
        assert db.clear_baseline() is False

    def test_restore_previous(self, db):
        first = BaselineRow(
            current_mode="era_1",
            started_at_utc=datetime(2026, 4, 1, tzinfo=timezone.utc),
            start_balance_usdt=Decimal("60.00"),
        )
        db.set_baseline(first)
        db.clear_baseline()
        assert db.get_baseline() is None
        assert db.restore_previous_baseline() is True
        restored = db.get_baseline()
        assert restored is not None
        assert restored.current_mode == "era_1"

    def test_restore_without_archive(self, db):
        assert db.restore_previous_baseline() is False

    def test_historical_trades_untouched_by_baseline_ops(self, db):
        # Insert trades BEFORE any baseline exists
        t_old = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
        _insert_trade(db, timestamp=t_old, pnl="1.23")
        _insert_trade(db, timestamp=t_old, pnl="-0.50")

        def trade_count():
            conn = db._get_conn()
            return conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

        assert trade_count() == 2

        # Every baseline op must leave trade rows identical
        db.set_baseline(BaselineRow(
            current_mode="phase2c",
            started_at_utc=datetime(2026, 4, 22, tzinfo=timezone.utc),
            start_balance_usdt=Decimal("68.33"),
        ))
        assert trade_count() == 2

        db.set_baseline(BaselineRow(
            current_mode="phase2c_v2",
            started_at_utc=datetime(2026, 4, 23, tzinfo=timezone.utc),
            start_balance_usdt=Decimal("70.00"),
        ))
        assert trade_count() == 2

        db.clear_baseline()
        assert trade_count() == 2

        db.restore_previous_baseline()
        assert trade_count() == 2


# ---------------------------------------------------------------------------
# ForensicQueries — since filter
# ---------------------------------------------------------------------------


class TestForensicQueriesSince:
    @pytest.fixture()
    def seeded(self, db):
        # Three trades: one old, two new
        old_ts = datetime(2026, 3, 15, tzinfo=timezone.utc)
        new_ts = datetime(2026, 4, 23, tzinfo=timezone.utc)
        _insert_trade(db, timestamp=old_ts, pnl="-2.00", cascade="flip")
        _insert_trade(db, timestamp=new_ts, pnl="1.50", cascade="flip")
        _insert_trade(db, timestamp=new_ts, pnl="0.80", cascade="continuation")
        return db

    def test_no_since_returns_all(self, seeded):
        fq = ForensicQueries(seeded)
        rows = fq.per_cascade_expectancy()
        # flip has 2 trades (one old negative, one new positive), continuation has 1
        by_cascade = {r["cascade_level"]: r for r in rows}
        assert by_cascade["flip"]["n"] == 2
        assert by_cascade["continuation"]["n"] == 1

    def test_since_filters_old_rows(self, seeded):
        fq = ForensicQueries(seeded)
        cutoff = datetime(2026, 4, 1, tzinfo=timezone.utc)
        rows = fq.per_cascade_expectancy(since=cutoff)
        by_cascade = {r["cascade_level"]: r for r in rows}
        assert by_cascade["flip"]["n"] == 1  # the OLD one is excluded
        assert by_cascade["flip"]["avg_pnl"] == pytest.approx(1.50)
        assert by_cascade["continuation"]["n"] == 1

    def test_since_future_returns_empty(self, seeded):
        fq = ForensicQueries(seeded)
        future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        assert fq.per_cascade_expectancy(since=future) == []

    def test_since_naive_datetime_returns_empty(self, seeded):
        # Naive datetime must raise inside _to_iso; _run_since catches via _run,
        # but _to_iso raises ValueError directly. Verify that helper rejects it.
        fq = ForensicQueries(seeded)
        with pytest.raises(ValueError, match="timezone-aware"):
            fq._to_iso(datetime(2026, 4, 1))  # type: ignore[arg-type]

    def test_apply_since_does_not_run_without_since(self):
        # apply_since with None returns original SQL unchanged and empty params.
        sql_in = "SELECT * FROM trades WHERE pnl IS NOT NULL;"
        sql_out, params = ForensicQueries._apply_since(sql_in, None)
        assert sql_out == sql_in
        assert params == ()

    def test_apply_since_injects_after_where(self):
        sql_in = "SELECT * FROM trades WHERE pnl IS NOT NULL GROUP BY cascade_level;"
        sql_out, params = ForensicQueries._apply_since(sql_in, "2026-04-22T00:00:00+00:00")
        assert "timestamp >= ?" in sql_out
        assert "AND pnl IS NOT NULL" in sql_out
        assert params == ("2026-04-22T00:00:00+00:00",)

    def test_apply_since_inserts_where_when_absent(self):
        # Q12 cycle_latency has no WHERE
        sql_in = "SELECT DATE(timestamp) FROM cycle_history GROUP BY d;"
        sql_out, params = ForensicQueries._apply_since(sql_in, "2026-04-22T00:00:00+00:00")
        assert "WHERE timestamp >= ?" in sql_out
        assert params == ("2026-04-22T00:00:00+00:00",)

    def test_apply_since_picks_correct_column_for_decision_log(self):
        sql_in = "SELECT * FROM decision_log WHERE outcome = 'reject';"
        sql_out, _ = ForensicQueries._apply_since(sql_in, "2026-04-22T00:00:00+00:00")
        assert "timestamp_utc >= ?" in sql_out

    def test_run_all_with_since_smoke(self, seeded):
        fq = ForensicQueries(seeded)
        cutoff = datetime(2026, 4, 1, tzinfo=timezone.utc)
        out = fq.run_all(since=cutoff)
        # Every query must be keyed in the output (even if empty).
        assert set(out.keys()) == {name for name, _ in ALL_QUERIES}

    def test_historical_trades_untouched_after_since_query(self, seeded):
        before = seeded._get_conn().execute(
            "SELECT trade_id, pnl, timestamp FROM trades ORDER BY trade_id"
        ).fetchall()
        fq = ForensicQueries(seeded)
        fq.per_cascade_expectancy(since=datetime(2026, 4, 1, tzinfo=timezone.utc))
        fq.exit_reason_mix(since=datetime(2026, 4, 1, tzinfo=timezone.utc))
        after = seeded._get_conn().execute(
            "SELECT trade_id, pnl, timestamp FROM trades ORDER BY trade_id"
        ).fetchall()
        assert [tuple(r) for r in before] == [tuple(r) for r in after]


# ---------------------------------------------------------------------------
# AttributionReporter — since mode
# ---------------------------------------------------------------------------


class TestAttributionReporterSince:
    @pytest.fixture()
    def seeded(self, db):
        old = datetime(2026, 3, 1, tzinfo=timezone.utc)
        new = datetime(2026, 4, 23, tzinfo=timezone.utc)
        _insert_trade(db, timestamp=old, pnl="-5.00")
        _insert_trade(db, timestamp=new, pnl="2.00")
        return db

    def test_no_since_filename_unchanged(self, seeded, tmp_path):
        reporter = AttributionReporter(seeded, reports_dir=tmp_path)
        from datetime import date
        out = reporter.generate(report_date=date(2026, 4, 23))
        assert out.name == "2026-04-23-attribution.md"
        assert out.exists()

    def test_since_baseline_filename_suffix(self, seeded, tmp_path):
        reporter = AttributionReporter(seeded, reports_dir=tmp_path)
        from datetime import date
        out = reporter.generate(
            report_date=date(2026, 4, 23),
            since=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )
        assert out.name == "2026-04-23-attribution-since-baseline.md"
        content = out.read_text()
        assert "SINCE BASELINE" in content
        assert "Baseline filter active" in content

    def test_since_baseline_header_includes_meta(self, seeded, tmp_path):
        reporter = AttributionReporter(seeded, reports_dir=tmp_path)
        from datetime import date
        content = reporter.build_content(
            report_date=date(2026, 4, 23),
            since=datetime(2026, 4, 1, tzinfo=timezone.utc),
            baseline_meta={
                "current_mode": "mainnet_reduced_live_v1",
                "start_balance_usdt": "68.33",
                "notes": "cohort A",
            },
        )
        assert "mainnet_reduced_live_v1" in content
        assert "68.33" in content
        assert "cohort A" in content

    def test_since_filters_cascade_data_in_report(self, seeded, tmp_path):
        reporter = AttributionReporter(seeded, reports_dir=tmp_path)
        from datetime import date

        # No since: both trades counted -> total = -5 + 2 = -3.0000
        full = reporter.build_content(report_date=date(2026, 4, 23))
        # Since April 1: only the +2.00 trade counted -> total = 2.0000
        filtered = reporter.build_content(
            report_date=date(2026, 4, 23),
            since=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )
        assert "Per-Cascade" in full
        assert "Per-Cascade" in filtered
        # Aggregated total PnL differs between the two views.
        assert "-3.0000" in full            # full view has the negative trade
        assert "-3.0000" not in filtered    # filtered view excludes it
        assert "2.0000" in filtered         # filtered view shows +2.00 total

    def test_since_context_reset_after_render(self, seeded, tmp_path):
        reporter = AttributionReporter(seeded, reports_dir=tmp_path)
        from datetime import date
        reporter.build_content(
            report_date=date(2026, 4, 23),
            since=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )
        # After one since-call, a subsequent call with no since must NOT leak the filter
        assert reporter._since is None
        assert reporter._baseline_meta is None


# ---------------------------------------------------------------------------
# Regression: Phase 1C backward compat
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    def test_forensic_queries_no_kwargs_still_works(self, db):
        fq = ForensicQueries(db)
        # With no trades, every method returns [] when called with no args.
        # This verifies Phase 1C call-site backward compatibility.
        assert fq.per_cascade_expectancy() == []
        assert fq.per_regime_expectancy() == []
        assert fq.maker_taker_pnl() == []
        assert fq.slippage_cost() == [] or fq.slippage_cost()[0]["n"] == 0
        assert fq.rejection_distribution() == []
        assert fq.exit_reason_mix() == []
        assert fq.symbol_pnl_with_drag() == []
        assert fq.confidence_bucket_win_rate() == []
        assert fq.funding_filter_impact() == []
        assert fq.consensus_adj_impact() == []
        assert fq.cascade_conversion_funnel() == []
        assert fq.cycle_latency() == []

    def test_attribution_report_no_kwargs_unchanged(self, db, tmp_path):
        reporter = AttributionReporter(db, reports_dir=tmp_path)
        from datetime import date
        out = reporter.generate(report_date=date(2026, 4, 23))
        assert out.name == "2026-04-23-attribution.md"
        content = out.read_text()
        # No since header
        assert "Baseline filter active" not in content
        assert "SINCE BASELINE" not in content
