"""Tests for DecisionLogger — thin wrapper over insert_decision_log."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.data.database import DatabaseManager
from src.memory.decision_logger import VALID_OUTCOMES, VALID_STAGES, DecisionLogger


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    return DatabaseManager(db_path=tmp_path / "test_dl.db")


@pytest.fixture
def cycle_id(db: DatabaseManager) -> int:
    return db.begin_cycle(1, datetime.now(timezone.utc))


@pytest.fixture
def logger_instance(db: DatabaseManager) -> DecisionLogger:
    return DecisionLogger(db)


class TestDecisionLoggerLog:
    def test_pass_outcome_written(
        self, logger_instance: DecisionLogger, db: DatabaseManager, cycle_id: int
    ) -> None:
        logger_instance.log(cycle_id, "BTC/USDT:USDT", "regime_detect", "pass")
        rows = db.get_decision_logs(cycle_id=cycle_id)
        assert len(rows) == 1
        assert rows[0]["outcome"] == "pass"

    def test_reject_outcome_written(
        self, logger_instance: DecisionLogger, db: DatabaseManager, cycle_id: int
    ) -> None:
        logger_instance.log(cycle_id, "ETH/USDT:USDT", "signal_generate", "reject",
                            reason="no_signal")
        rows = db.get_decision_logs(cycle_id=cycle_id)
        assert rows[0]["outcome"] == "reject"
        assert rows[0]["reason"] == "no_signal"

    def test_skip_outcome_written(
        self, logger_instance: DecisionLogger, db: DatabaseManager, cycle_id: int
    ) -> None:
        logger_instance.log(cycle_id, "SOL/USDT:USDT", "position_overlap_skip", "skip")
        rows = db.get_decision_logs(cycle_id=cycle_id)
        assert rows[0]["outcome"] == "skip"

    def test_error_outcome_written(
        self, logger_instance: DecisionLogger, db: DatabaseManager, cycle_id: int
    ) -> None:
        logger_instance.log(cycle_id, "XRP/USDT:USDT", "data_fetch_4h", "error",
                            reason="connection_timeout")
        rows = db.get_decision_logs(cycle_id=cycle_id)
        assert rows[0]["outcome"] == "error"

    def test_unknown_stage_silently_dropped(
        self, logger_instance: DecisionLogger, db: DatabaseManager,
        cycle_id: int, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="src.memory.decision_logger"):
            logger_instance.log(cycle_id, "BTC/USDT:USDT", "nonexistent_stage", "pass")
        rows = db.get_decision_logs(cycle_id=cycle_id)
        assert len(rows) == 0  # Nothing written

    def test_unknown_outcome_silently_dropped(
        self, logger_instance: DecisionLogger, db: DatabaseManager,
        cycle_id: int, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="src.memory.decision_logger"):
            logger_instance.log(cycle_id, "BTC/USDT:USDT", "regime_detect", "bogus_outcome")
        rows = db.get_decision_logs(cycle_id=cycle_id)
        assert len(rows) == 0

    def test_cycle_id_zero_silently_dropped(
        self, logger_instance: DecisionLogger, db: DatabaseManager
    ) -> None:
        logger_instance.log(0, "BTC/USDT:USDT", "regime_detect", "pass")
        rows = db.get_decision_logs()
        assert len(rows) == 0

    def test_cycle_id_negative_silently_dropped(
        self, logger_instance: DecisionLogger, db: DatabaseManager
    ) -> None:
        logger_instance.log(-1, "BTC/USDT:USDT", "regime_detect", "pass")
        rows = db.get_decision_logs()
        assert len(rows) == 0

    def test_db_exception_swallowed(
        self, db: DatabaseManager, cycle_id: int
    ) -> None:
        bad_db = MagicMock()
        bad_db.insert_decision_log.side_effect = RuntimeError("DB exploded")
        dl = DecisionLogger(bad_db)
        # Must not raise
        dl.log(1, "BTC/USDT:USDT", "regime_detect", "pass")

    def test_numeric_context_json_serialized(
        self, logger_instance: DecisionLogger, db: DatabaseManager, cycle_id: int
    ) -> None:
        import json
        ctx = {"adx": 25.3, "regime": "TRENDING"}
        logger_instance.log(cycle_id, "ETH/USDT:USDT", "regime_detect", "pass",
                            numeric_context=ctx)
        rows = db.get_decision_logs(cycle_id=cycle_id)
        assert rows[0]["numeric_context"] is not None
        parsed = json.loads(rows[0]["numeric_context"])
        assert parsed["adx"] == pytest.approx(25.3)

    def test_none_numeric_context_stored_as_null(
        self, logger_instance: DecisionLogger, db: DatabaseManager, cycle_id: int
    ) -> None:
        logger_instance.log(cycle_id, "ETH/USDT:USDT", "regime_detect", "pass",
                            numeric_context=None)
        rows = db.get_decision_logs(cycle_id=cycle_id)
        assert rows[0]["numeric_context"] is None

    def test_none_reason_stored_as_null(
        self, logger_instance: DecisionLogger, db: DatabaseManager, cycle_id: int
    ) -> None:
        logger_instance.log(cycle_id, "ETH/USDT:USDT", "data_fetch_4h", "pass", reason=None)
        rows = db.get_decision_logs(cycle_id=cycle_id)
        assert rows[0]["reason"] is None

    def test_confidence_stored(
        self, logger_instance: DecisionLogger, db: DatabaseManager, cycle_id: int
    ) -> None:
        logger_instance.log(cycle_id, "ETH/USDT:USDT", "signal_generate", "pass",
                            confidence=72.5)
        rows = db.get_decision_logs(cycle_id=cycle_id)
        assert rows[0]["confidence"] == pytest.approx(72.5)

    def test_regime_stored(
        self, logger_instance: DecisionLogger, db: DatabaseManager, cycle_id: int
    ) -> None:
        logger_instance.log(cycle_id, "BTC/USDT:USDT", "regime_detect", "pass",
                            regime="TRENDING")
        rows = db.get_decision_logs(cycle_id=cycle_id)
        assert rows[0]["regime"] == "TRENDING"

    def test_cascade_level_stored(
        self, logger_instance: DecisionLogger, db: DatabaseManager, cycle_id: int
    ) -> None:
        logger_instance.log(cycle_id, "BTC/USDT:USDT", "signal_generate", "pass",
                            cascade_level="4H")
        rows = db.get_decision_logs(cycle_id=cycle_id)
        assert rows[0]["cascade_level"] == "4H"

    def test_valid_stages_constant_contains_all_spec_stages(self) -> None:
        required = {
            "data_fetch_4h", "data_fetch_1h", "data_fetch_15m",
            "regime_detect", "signal_generate", "confidence_gate",
            "cross_asset_consensus_adjust", "position_overlap_skip",
            "funding_filter", "leverage_determine", "volatility_adjust",
            "sizing", "min_notional", "liquidation_buffer",
            "price_validate", "signal_validate", "decision_audit",
        }
        assert required.issubset(VALID_STAGES)

    def test_valid_outcomes_contains_four_values(self) -> None:
        assert VALID_OUTCOMES == {"pass", "reject", "skip", "error"}
