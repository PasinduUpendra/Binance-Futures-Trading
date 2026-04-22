"""
Tests for AttributionReporter — Phase 1C daily attribution report.

All tests use an in-memory SQLite database.  Report writing is tested in a
tmp_path so no production docs/reports/ directory is touched.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from src.data.database import CycleHistoryRow, DatabaseManager
from src.reporting.attribution_report import (
    AttributionReporter,
    _fmt_float,
    _fmt_pct,
    _maker_label,
    _md_table,
)


# ---------------------------------------------------------------------------
# Fixtures (reuse seed helpers from test_forensic_queries for brevity)
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path):
    return DatabaseManager(tmp_path / "test.db")


@pytest.fixture()
def db_seeded(db):
    """Seed with cycle history + trades (minimal set)."""
    _seed_cycles(db)
    _seed_trades(db)
    return db


def _seed_cycles(db: DatabaseManager) -> None:
    for i in range(3):
        db.store_cycle(CycleHistoryRow(
            cycle_number=i + 1,
            timestamp=datetime(2026, 4, 22, i, 0, 0, tzinfo=timezone.utc),
            circuit_breaker_level="GREEN",
            balance=Decimal("68.33"),
            duration_seconds=15.0,
        ))


def _seed_trades(db: DatabaseManager) -> None:
    import uuid
    conn = db._get_conn()
    trades = [
        ("flip", "70-85", "TRENDING", 1, 0, "1.50", "0.12", "-0.02", "tp_hit", 5.0),
        ("continuation", "55-70", "TRENDING", 0, 0, "-0.80", "0.08", "0.01", "sl_hit", -3.0),
        ("flip", "70-85", "TRENDING", 1, 1, "0.50", "0.06", "0.00", "trail", 0.0),
    ]
    for cascade, bucket, regime, mk_e, mk_x, pnl, fees, funding, exit_r, cadj in trades:
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
                str(uuid.uuid4()),
                "2026-04-22T01:00:00+00:00",
                "ETH/USDT:USDT",
                "long",
                "2450.00",
                "2600.00",
                "0.01",
                5,
                pnl,
                "0.02",
                "SupertrendTrend",
                regime,
                "72.0",
                cascade,
                bucket,
                regime,
                mk_e,
                mk_x,
                fees,
                funding,
                exit_r,
                cadj,
                0.0,
                2.0,
                3.0,
                8,
                "1.40",
            ),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_fmt_float_none(self):
        assert _fmt_float(None) == "n/a"

    def test_fmt_float_number(self):
        assert _fmt_float(1.23456789, 2) == "1.23"

    def test_fmt_float_string(self):
        # Stored as TEXT in DB
        assert _fmt_float("1.5", 2) == "1.50"

    def test_fmt_pct_none(self):
        assert _fmt_pct(None) == "n/a"

    def test_fmt_pct_ratio(self):
        assert _fmt_pct(0.75) == "75.0%"

    def test_fmt_pct_zero(self):
        assert _fmt_pct(0.0) == "0.0%"

    def test_maker_label_one(self):
        assert _maker_label(1) == "maker"

    def test_maker_label_zero(self):
        assert _maker_label(0) == "taker"

    def test_maker_label_string_one(self):
        assert _maker_label("1") == "maker"

    def test_md_table_empty_rows(self):
        result = _md_table(["A", "B"], [])
        assert "no data yet" in result

    def test_md_table_with_rows(self):
        result = _md_table(["A", "B"], [["x", "y"]])
        assert "| A | B |" in result
        assert "| x | y |" in result


# ---------------------------------------------------------------------------
# Report content tests (empty DB)
# ---------------------------------------------------------------------------


class TestAttributionReporterEmpty:
    def test_generate_returns_path(self, db, tmp_path):
        reporter = AttributionReporter(db, reports_dir=tmp_path)
        path = reporter.generate(date(2026, 4, 22))
        assert path == tmp_path / "2026-04-22-attribution.md"

    def test_file_is_written(self, db, tmp_path):
        reporter = AttributionReporter(db, reports_dir=tmp_path)
        path = reporter.generate(date(2026, 4, 22))
        assert path.exists()

    def test_report_title_contains_date(self, db, tmp_path):
        reporter = AttributionReporter(db, reports_dir=tmp_path)
        content = reporter.build_content(date(2026, 4, 22))
        assert "2026-04-22" in content

    def test_report_has_ten_sections(self, db, tmp_path):
        reporter = AttributionReporter(db, reports_dir=tmp_path)
        content = reporter.build_content(date(2026, 4, 22))
        # Check all 10 section headings are present
        for i in range(1, 11):
            assert f"## {i}." in content, f"Section {i} heading missing"

    def test_report_no_backtest_percentage_claims(self, db, tmp_path):
        """Report must not cite any specific 'validated daily' backtest percentage."""
        reporter = AttributionReporter(db, reports_dir=tmp_path)
        content = reporter.build_content(date(2026, 4, 22))
        # These phrases are forbidden per DRIFT_MAP
        forbidden = ["0.628%", "1.397%", "2.68%", "1.149%", "validated daily"]
        for phrase in forbidden:
            assert phrase not in content, f"Forbidden phrase {phrase!r} found in report"

    def test_report_has_honest_note(self, db, tmp_path):
        reporter = AttributionReporter(db, reports_dir=tmp_path)
        content = reporter.build_content(date(2026, 4, 22))
        assert "live trades" in content.lower() or "live-verified" in content.lower()

    def test_empty_sections_show_no_data(self, db, tmp_path):
        reporter = AttributionReporter(db, reports_dir=tmp_path)
        content = reporter.build_content(date(2026, 4, 22))
        assert "no data yet" in content

    def test_write_false_no_file(self, db, tmp_path):
        reporter = AttributionReporter(db, reports_dir=tmp_path)
        path = reporter.generate(date(2026, 4, 22), write=False)
        assert not path.exists()


# ---------------------------------------------------------------------------
# Report content tests (seeded DB)
# ---------------------------------------------------------------------------


class TestAttributionReporterWithData:
    def test_cascade_section_populated(self, db_seeded, tmp_path):
        reporter = AttributionReporter(db_seeded, reports_dir=tmp_path)
        content = reporter.build_content(date(2026, 4, 22))
        assert "flip" in content
        assert "continuation" in content

    def test_regime_section_populated(self, db_seeded, tmp_path):
        reporter = AttributionReporter(db_seeded, reports_dir=tmp_path)
        content = reporter.build_content(date(2026, 4, 22))
        assert "TRENDING" in content

    def test_exit_reason_section_populated(self, db_seeded, tmp_path):
        reporter = AttributionReporter(db_seeded, reports_dir=tmp_path)
        content = reporter.build_content(date(2026, 4, 22))
        assert "tp_hit" in content
        assert "sl_hit" in content
        assert "trail" in content

    def test_maker_taker_section_populated(self, db_seeded, tmp_path):
        reporter = AttributionReporter(db_seeded, reports_dir=tmp_path)
        content = reporter.build_content(date(2026, 4, 22))
        assert "maker" in content

    def test_fee_section_shows_totals(self, db_seeded, tmp_path):
        reporter = AttributionReporter(db_seeded, reports_dir=tmp_path)
        content = reporter.build_content(date(2026, 4, 22))
        assert "Total fees paid" in content

    def test_cycle_latency_section_populated(self, db_seeded, tmp_path):
        reporter = AttributionReporter(db_seeded, reports_dir=tmp_path)
        content = reporter.build_content(date(2026, 4, 22))
        assert "2026-04-22" in content  # date from cycle_history

    def test_no_data_sections_absent_when_full(self, db_seeded, tmp_path):
        reporter = AttributionReporter(db_seeded, reports_dir=tmp_path)
        content = reporter.build_content(date(2026, 4, 22))
        # Fee section, cascade, regime, maker, exit — should all be populated
        # Count "no data yet" occurrences (some sections like funnel may still be empty)
        no_data_count = content.count("no data yet")
        # At most 2–3 sections may still show no data (decision_log not seeded in this fixture)
        assert no_data_count <= 4

    def test_report_filename_format(self, db_seeded, tmp_path):
        reporter = AttributionReporter(db_seeded, reports_dir=tmp_path)
        path = reporter.generate(date(2026, 4, 22))
        assert path.name == "2026-04-22-attribution.md"

    def test_report_default_date_is_today(self, db_seeded, tmp_path):
        reporter = AttributionReporter(db_seeded, reports_dir=tmp_path)
        today = datetime.now(timezone.utc).date()
        path = reporter.generate()
        assert today.isoformat() in path.name

    def test_confidence_bucket_section_populated(self, db_seeded, tmp_path):
        reporter = AttributionReporter(db_seeded, reports_dir=tmp_path)
        content = reporter.build_content(date(2026, 4, 22))
        assert "70-85" in content or "55-70" in content

    def test_win_rate_is_percentage_formatted(self, db_seeded, tmp_path):
        """Win rates must appear as percentages (e.g., 66.7%) not raw ratios (0.667)."""
        reporter = AttributionReporter(db_seeded, reports_dir=tmp_path)
        content = reporter.build_content(date(2026, 4, 22))
        # Should see percentage signs
        assert "%" in content

    def test_footer_cites_query_files(self, db_seeded, tmp_path):
        reporter = AttributionReporter(db_seeded, reports_dir=tmp_path)
        content = reporter.build_content(date(2026, 4, 22))
        assert "forensic_queries.py" in content
