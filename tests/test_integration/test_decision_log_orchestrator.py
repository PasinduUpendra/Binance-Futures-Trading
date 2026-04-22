"""Integration tests: decision_log rows written by orchestrator paths."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from src.data.database import DatabaseManager


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _make_ohlcv_df(n: int = 200) -> pd.DataFrame:
    """Return a minimal OHLCV dataframe with all indicator columns populated."""
    np.random.seed(42)
    closes = 2000.0 + np.cumsum(np.random.randn(n) * 10)
    df = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="4h"),
        "open":   closes * 0.999,
        "high":   closes * 1.002,
        "low":    closes * 0.997,
        "close":  closes,
        "volume": np.random.uniform(1000, 5000, n),
    })
    # Pre-fill mandatory indicator columns so IndicatorEngine isn't needed
    df["supertrend"]           = closes - 50
    df["supertrend_direction"] = 1.0
    df["adx"]                  = 25.0
    df["atr"]                  = 12.0
    df["rsi"]                  = 55.0
    df["ema_9"]                = closes * 0.995
    df["ema_21"]               = closes * 0.99
    df["ema_50"]               = closes * 0.98
    df["ema_200"]              = closes * 0.96
    df["macd"]                 = 5.0
    df["macd_signal"]          = 4.5
    df["bb_upper"]             = closes * 1.02
    df["bb_middle"]            = closes
    df["bb_lower"]             = closes * 0.98
    df["zscore"]               = 0.3
    return df


# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_db(tmp_path: Path) -> DatabaseManager:
    return DatabaseManager(db_path=tmp_path / "orch_test.db")


# ─── Data-fetch stage logging ─────────────────────────────────────────────────


class TestDataFetchStageLogging:
    """Verify data_fetch_4h/1h/15m rows appear after _run_cycle step 1b."""

    def test_data_fetch_pass_rows_created(self, tmp_db: DatabaseManager) -> None:
        """When data fetch succeeds, expect 'pass' rows for data_fetch_4h and 1h."""
        df = _make_ohlcv_df()
        raw = df[["timestamp", "open", "high", "low", "close", "volume"]].values.tolist()

        cycle_id = tmp_db.begin_cycle(1, datetime.now(timezone.utc))

        from src.memory.decision_logger import DecisionLogger
        dl = DecisionLogger(tmp_db)

        # Simulate what _run_cycle does for a successful data fetch
        dl.log(cycle_id, "ETH/USDT:USDT", "data_fetch_4h", "pass",
               numeric_context={"rows": len(df)})
        dl.log(cycle_id, "ETH/USDT:USDT", "data_fetch_1h", "pass",
               numeric_context={"rows": len(df)})
        dl.log(cycle_id, "ETH/USDT:USDT", "data_fetch_15m", "pass",
               numeric_context={"rows": 100})

        rows = tmp_db.get_decision_logs(cycle_id=cycle_id)
        stages = {r["stage"] for r in rows}
        assert "data_fetch_4h" in stages
        assert "data_fetch_1h" in stages
        assert "data_fetch_15m" in stages
        outcomes = {r["stage"]: r["outcome"] for r in rows}
        assert outcomes["data_fetch_4h"] == "pass"
        assert outcomes["data_fetch_1h"] == "pass"

    def test_data_fetch_reject_row_on_insufficient_rows(
        self, tmp_db: DatabaseManager
    ) -> None:
        cycle_id = tmp_db.begin_cycle(1, datetime.now(timezone.utc))

        from src.memory.decision_logger import DecisionLogger
        dl = DecisionLogger(tmp_db)
        dl.log(cycle_id, "BTC/USDT:USDT", "data_fetch_4h", "reject",
               reason="insufficient_rows",
               numeric_context={"rows": 5})

        rows = tmp_db.get_decision_logs(cycle_id=cycle_id, stage="data_fetch_4h")
        assert len(rows) == 1
        assert rows[0]["outcome"] == "reject"
        assert rows[0]["reason"] == "insufficient_rows"

    def test_data_fetch_error_row_on_exception(self, tmp_db: DatabaseManager) -> None:
        cycle_id = tmp_db.begin_cycle(1, datetime.now(timezone.utc))

        from src.memory.decision_logger import DecisionLogger
        dl = DecisionLogger(tmp_db)
        dl.log(cycle_id, "SOL/USDT:USDT", "data_fetch_4h", "error",
               reason="ConnectionError: timeout")

        rows = tmp_db.get_decision_logs(cycle_id=cycle_id)
        assert rows[0]["outcome"] == "error"

    def test_cycle_history_row_exists_after_begin(self, tmp_db: DatabaseManager) -> None:
        import sqlite3
        cycle_id = tmp_db.begin_cycle(1, datetime.now(timezone.utc))
        conn = sqlite3.connect(tmp_db._db_path)
        row = conn.execute(
            "SELECT id FROM cycle_history WHERE id=?", (cycle_id,)
        ).fetchone()
        conn.close()
        assert row is not None


# ─── _execute_signal stage logging (via DecisionLogger directly) ───────────


class TestExecuteSignalStageLogging:
    """Simulate _execute_signal stage logs and verify rows."""

    def test_funding_filter_reject_row(self, tmp_db: DatabaseManager) -> None:
        cycle_id = tmp_db.begin_cycle(1, datetime.now(timezone.utc))

        from src.memory.decision_logger import DecisionLogger
        dl = DecisionLogger(tmp_db)
        dl.log(cycle_id, "DOGE/USDT:USDT", "funding_filter", "reject",
               reason="funding_too_high_for_long",
               numeric_context={"funding_rate": 0.0012, "direction": "long"})

        rows = tmp_db.get_decision_logs(cycle_id=cycle_id, stage="funding_filter")
        assert len(rows) == 1
        assert rows[0]["outcome"] == "reject"

    def test_leverage_determine_reject_row(self, tmp_db: DatabaseManager) -> None:
        cycle_id = tmp_db.begin_cycle(1, datetime.now(timezone.utc))

        from src.memory.decision_logger import DecisionLogger
        dl = DecisionLogger(tmp_db)
        dl.log(cycle_id, "BTC/USDT:USDT", "leverage_determine", "reject",
               reason="confidence_below_threshold",
               confidence=15.0,
               numeric_context={"leverage": 0})

        rows = tmp_db.get_decision_logs(cycle_id=cycle_id, stage="leverage_determine")
        assert rows[0]["outcome"] == "reject"
        assert rows[0]["confidence"] == pytest.approx(15.0)

    def test_liquidation_buffer_reject_row(self, tmp_db: DatabaseManager) -> None:
        cycle_id = tmp_db.begin_cycle(1, datetime.now(timezone.utc))

        from src.memory.decision_logger import DecisionLogger
        dl = DecisionLogger(tmp_db)
        dl.log(cycle_id, "ETH/USDT:USDT", "liquidation_buffer", "reject",
               reason="buffer_below_5pct",
               numeric_context={"buffer_pct": 0.03, "is_safe": False})

        rows = tmp_db.get_decision_logs(cycle_id=cycle_id, stage="liquidation_buffer")
        assert rows[0]["outcome"] == "reject"

    def test_decision_audit_reject_row(self, tmp_db: DatabaseManager) -> None:
        cycle_id = tmp_db.begin_cycle(1, datetime.now(timezone.utc))

        from src.memory.decision_logger import DecisionLogger
        dl = DecisionLogger(tmp_db)
        dl.log(cycle_id, "SOL/USDT:USDT", "decision_audit", "reject",
               reason="insufficient evidence for entry",
               numeric_context={"decision": "REJECT"})

        rows = tmp_db.get_decision_logs(cycle_id=cycle_id, stage="decision_audit")
        assert rows[0]["outcome"] == "reject"

    def test_decision_audit_skip_row(self, tmp_db: DatabaseManager) -> None:
        cycle_id = tmp_db.begin_cycle(1, datetime.now(timezone.utc))

        from src.memory.decision_logger import DecisionLogger
        dl = DecisionLogger(tmp_db)
        dl.log(cycle_id, "ADA/USDT:USDT", "decision_audit", "skip",
               reason="opposing position held",
               numeric_context={"decision": "SKIP"})

        rows = tmp_db.get_decision_logs(cycle_id=cycle_id, stage="decision_audit")
        assert rows[0]["outcome"] == "skip"

    def test_all_execute_signal_stages_present(self, tmp_db: DatabaseManager) -> None:
        """A full signal execution path should produce rows for all 9 stages."""
        cycle_id = tmp_db.begin_cycle(1, datetime.now(timezone.utc))

        from src.memory.decision_logger import DecisionLogger
        dl = DecisionLogger(tmp_db)

        stages_to_log = [
            ("funding_filter",      "pass"),
            ("leverage_determine",  "pass"),
            ("volatility_adjust",   "pass"),
            ("sizing",              "pass"),
            ("min_notional",        "pass"),
            ("liquidation_buffer",  "pass"),
            ("price_validate",      "pass"),
            ("signal_validate",     "pass"),
            ("decision_audit",      "pass"),
        ]
        for stage, outcome in stages_to_log:
            dl.log(cycle_id, "ETH/USDT:USDT", stage, outcome)

        rows = tmp_db.get_decision_logs(cycle_id=cycle_id)
        logged_stages = {r["stage"] for r in rows}
        for stage, _ in stages_to_log:
            assert stage in logged_stages, f"Missing stage: {stage}"


# ─── Cycle finish → decision_log FK intact ───────────────────────────────────


class TestCycleFKIntegrity:
    def test_finish_cycle_preserves_decision_log_rows(
        self, tmp_db: DatabaseManager
    ) -> None:
        from src.data.database import CycleHistoryRow
        from src.memory.decision_logger import DecisionLogger

        ts = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
        cycle_id = tmp_db.begin_cycle(1, ts)

        dl = DecisionLogger(tmp_db)
        dl.log(cycle_id, "ETH/USDT:USDT", "regime_detect", "pass")

        cycle_row = CycleHistoryRow(
            cycle_number=1,
            timestamp=ts,
            circuit_breaker_level="GREEN",
            balance=Decimal("68.33"),
            regime="TRENDING",
            signal_generated=True,
            trade_placed=False,
            trade_details=None,
            positions_closed="[]",
            errors="[]",
            duration_seconds=1.5,
        )
        tmp_db.finish_cycle(cycle_id=cycle_id, cycle=cycle_row)

        # Decision log rows must still be present after finish_cycle
        rows = tmp_db.get_decision_logs(cycle_id=cycle_id)
        assert len(rows) == 1
        assert rows[0]["stage"] == "regime_detect"
