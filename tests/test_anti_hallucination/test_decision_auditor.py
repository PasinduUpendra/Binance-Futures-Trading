"""Tests for DecisionAuditor — anti-hallucination Layer 4."""

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from src.anti_hallucination.decision_auditor import AuditReport, DecisionAuditor


@pytest.fixture
def auditor(tmp_path: Path) -> DecisionAuditor:
    return DecisionAuditor(db_path=str(tmp_path / "audit.db"))


def _make_signal(**overrides: object) -> dict:
    base = {
        "trade_id": "T-001",
        "symbol": "ETH/USDT:USDT",
        "direction": "LONG",
        "strategy": "SupertrendTrend",
        "entry_price": 3500,
        "stop_loss": 3400,
        "take_profit": 3700,
        "leverage": 5,
        "confidence": 75,
    }
    base.update(overrides)
    return base


def _make_regime(**overrides: object) -> dict:
    base = {"regime": "TRENDING", "confidence": 80.0}
    base.update(overrides)
    return base


def _make_risk_approval(**overrides: object) -> dict:
    base = {
        "approved": True,
        "position_size_usd": Decimal("100"),
        "risk_per_trade_pct": Decimal("0.015"),
        "risk_reward_ratio": Decimal("2.0"),
        "kelly_fraction": Decimal("0.12"),
        "notes": "",
    }
    base.update(overrides)
    return base


def _make_market_data(**overrides: object) -> dict:
    base = {
        "data_timestamps": {"4h": "2026-03-15T12:00:00Z"},
        "data_sources": ["binance_api"],
        "price_validated": True,
        "signal_validated": True,
        "sanity_checks_passed": True,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------


class TestAuditorInit:
    def test_creates_db(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "sub" / "audit.db")
        DecisionAuditor(db_path=db_path)
        assert Path(db_path).exists()

    def test_creates_table(self, auditor: DecisionAuditor) -> None:
        conn = sqlite3.connect(auditor._db_path)
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "audit_trail" in tables
        conn.close()


# ---------------------------------------------------------------------------
# audit_decision — full pipeline
# ---------------------------------------------------------------------------


class TestAuditDecision:
    def test_execute_decision_all_checks_pass(self, auditor: DecisionAuditor) -> None:
        report = auditor.audit_decision(
            signal=_make_signal(),
            regime=_make_regime(),
            risk_approval=_make_risk_approval(),
            market_data=_make_market_data(),
        )
        assert report.decision == "EXECUTE"
        assert report.symbol == "ETH/USDT:USDT"
        assert report.direction == "LONG"
        assert report.risk_approved is True

    def test_reject_risk_not_approved(self, auditor: DecisionAuditor) -> None:
        report = auditor.audit_decision(
            signal=_make_signal(),
            regime=_make_regime(),
            risk_approval=_make_risk_approval(approved=False),
            market_data=_make_market_data(),
        )
        assert report.decision == "REJECT"
        assert "Risk Manager" in report.decision_reasoning

    def test_reject_price_not_validated(self, auditor: DecisionAuditor) -> None:
        report = auditor.audit_decision(
            signal=_make_signal(),
            regime=_make_regime(),
            risk_approval=_make_risk_approval(),
            market_data=_make_market_data(price_validated=False),
        )
        assert report.decision == "REJECT"
        assert "hallucination" in report.decision_reasoning.lower()

    def test_reject_signal_not_validated(self, auditor: DecisionAuditor) -> None:
        report = auditor.audit_decision(
            signal=_make_signal(),
            regime=_make_regime(),
            risk_approval=_make_risk_approval(),
            market_data=_make_market_data(signal_validated=False),
        )
        assert report.decision == "REJECT"

    def test_reject_sanity_checks_failed(self, auditor: DecisionAuditor) -> None:
        report = auditor.audit_decision(
            signal=_make_signal(),
            regime=_make_regime(),
            risk_approval=_make_risk_approval(),
            market_data=_make_market_data(sanity_checks_passed=False),
        )
        assert report.decision == "REJECT"

    def test_reject_low_rr_ratio(self, auditor: DecisionAuditor) -> None:
        report = auditor.audit_decision(
            signal=_make_signal(),
            regime=_make_regime(),
            risk_approval=_make_risk_approval(risk_reward_ratio=Decimal("1.5")),
            market_data=_make_market_data(),
        )
        assert report.decision == "REJECT"
        assert "R/R" in report.decision_reasoning

    def test_persisted_to_db(self, auditor: DecisionAuditor) -> None:
        auditor.audit_decision(
            signal=_make_signal(),
            regime=_make_regime(),
            risk_approval=_make_risk_approval(),
            market_data=_make_market_data(),
        )
        conn = sqlite3.connect(auditor._db_path)
        count = conn.execute("SELECT COUNT(*) FROM audit_trail").fetchone()[0]
        conn.close()
        assert count == 1


# ---------------------------------------------------------------------------
# Devil's advocate
# ---------------------------------------------------------------------------


class TestDevilsAdvocate:
    def test_low_confidence_flagged(self) -> None:
        report = AuditReport(confidence=50)
        reasons = DecisionAuditor._devils_advocate(report)
        assert any("confidence" in r.lower() for r in reasons)

    def test_high_leverage_flagged(self) -> None:
        report = AuditReport(leverage=9)
        reasons = DecisionAuditor._devils_advocate(report)
        assert any("leverage" in r.lower() for r in reasons)

    def test_price_not_validated_flagged(self) -> None:
        report = AuditReport(price_validated=False)
        reasons = DecisionAuditor._devils_advocate(report)
        assert any("price" in r.lower() for r in reasons)

    def test_risk_not_approved_flagged(self) -> None:
        report = AuditReport(risk_approved=False)
        reasons = DecisionAuditor._devils_advocate(report)
        assert any("risk manager" in r.lower() for r in reasons)

    def test_low_kelly_flagged(self) -> None:
        report = AuditReport(kelly_fraction=Decimal("0.02"))
        reasons = DecisionAuditor._devils_advocate(report)
        assert any("kelly" in r.lower() for r in reasons)

    def test_regime_mismatch_trend_in_volatile(self) -> None:
        report = AuditReport(
            regime="volatile",
            strategy_name="macd_momentum",
        )
        reasons = DecisionAuditor._devils_advocate(report)
        assert any("trend-based" in r.lower() for r in reasons)

    def test_skip_on_many_concerns(self, auditor: DecisionAuditor) -> None:
        """4+ devil's advocate reasons should trigger SKIP instead of EXECUTE."""
        report = auditor.audit_decision(
            signal=_make_signal(confidence=40, leverage=9),
            regime=_make_regime(confidence=30),
            risk_approval=_make_risk_approval(
                risk_per_trade_pct=Decimal("0.05"),
                kelly_fraction=Decimal("0.03"),
            ),
            market_data=_make_market_data(),
        )
        assert report.decision == "SKIP"
        assert "devil" in report.decision_reasoning.lower()


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


class TestRetrieval:
    def test_get_audit_trail_by_trade_id(self, auditor: DecisionAuditor) -> None:
        auditor.audit_decision(
            signal=_make_signal(trade_id="T-ABC"),
            regime=_make_regime(),
            risk_approval=_make_risk_approval(),
            market_data=_make_market_data(),
        )
        result = auditor.get_audit_trail("T-ABC")
        assert result is not None
        assert result.trade_id == "T-ABC"

    def test_get_missing_trail_returns_none(self, auditor: DecisionAuditor) -> None:
        assert auditor.get_audit_trail("nonexistent") is None

    def test_get_recent_audits(self, auditor: DecisionAuditor) -> None:
        for i in range(5):
            auditor.audit_decision(
                signal=_make_signal(trade_id=f"T-{i}"),
                regime=_make_regime(),
                risk_approval=_make_risk_approval(),
                market_data=_make_market_data(),
            )
        recent = auditor.get_recent_audits(limit=3)
        assert len(recent) == 3

    def test_audit_report_json_roundtrip(self, auditor: DecisionAuditor) -> None:
        report = auditor.audit_decision(
            signal=_make_signal(entry_price=3500.12345),
            regime=_make_regime(),
            risk_approval=_make_risk_approval(),
            market_data=_make_market_data(),
        )
        retrieved = auditor.get_audit_trail(report.trade_id)
        assert retrieved is not None
        assert retrieved.entry_price == report.entry_price
        assert retrieved.strategy_name == "SupertrendTrend"
