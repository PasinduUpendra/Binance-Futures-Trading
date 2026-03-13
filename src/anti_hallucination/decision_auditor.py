"""
Decision audit trail for anti-hallucination.

Creates and persists a full audit record for every trading decision, including
the data timestamps, strategy context, regime assessment, risk math, and a
programmatic "devil's advocate" analysis of reasons *not* to take the trade.
All records are persisted to SQLite for post-trade forensic review.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("claude_quant.anti_hallucination.decision_auditor")

# Default database path — inside user_data so it persists across restarts.
_DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "user_data", "audit_trail.db"
)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class AuditReport(BaseModel):
    """Full audit trail for a single trading decision."""

    audit_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    trade_id: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))

    # Data provenance
    data_timestamps: dict[str, str] = Field(default_factory=dict)
    data_sources: list[str] = Field(default_factory=list)

    # Signal
    symbol: str = ""
    direction: str = ""
    strategy_name: str = ""
    entry_price: Decimal = Decimal("0")
    stop_loss: Decimal = Decimal("0")
    take_profit: Decimal = Decimal("0")
    leverage: int = 1
    confidence: float = 0.0

    # Regime
    regime: str = ""
    regime_confidence: float = 0.0

    # Risk math
    position_size_usd: Decimal = Decimal("0")
    risk_per_trade_pct: Decimal = Decimal("0")
    risk_reward_ratio: Decimal = Decimal("0")
    kelly_fraction: Decimal = Decimal("0")

    # Risk approval
    risk_approved: bool = False
    risk_notes: str = ""

    # Anti-hallucination
    price_validated: bool = False
    signal_validated: bool = False
    sanity_checks_passed: bool = False

    # Devil's advocate
    reasons_against: list[str] = Field(default_factory=list)

    # Final decision
    decision: str = ""  # "EXECUTE" | "REJECT" | "SKIP"
    decision_reasoning: str = ""


# ---------------------------------------------------------------------------
# DecisionAuditor
# ---------------------------------------------------------------------------


class DecisionAuditor:
    """Records and persists full audit trails for every trading decision.

    Parameters
    ----------
    db_path : str | None
        Path to the SQLite database.  Defaults to
        ``user_data/audit_trail.db`` relative to the project root.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or _DEFAULT_DB_PATH
        self._ensure_db()

    # -- database setup ------------------------------------------------------

    def _ensure_db(self) -> None:
        """Create the audit table if it does not exist."""
        db_dir = os.path.dirname(os.path.abspath(self._db_path))
        Path(db_dir).mkdir(parents=True, exist_ok=True)

        with self._connect() as conn:
            conn.execute(
                """
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
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_audit_trade_id
                ON audit_trail(trade_id)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_audit_timestamp
                ON audit_trail(timestamp)
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    # -- public API ----------------------------------------------------------

    def audit_decision(
        self,
        signal: dict[str, Any],
        regime: dict[str, Any],
        risk_approval: dict[str, Any],
        market_data: dict[str, Any],
    ) -> AuditReport:
        """Create a full audit report for a proposed trade, persist it, and return it.

        Parameters
        ----------
        signal : dict
            Must contain at minimum: ``symbol``, ``direction``, ``strategy``,
            ``entry_price``, ``stop_loss``, ``take_profit``, ``leverage``,
            ``confidence``.
        regime : dict
            Must contain: ``regime`` (str), ``confidence`` (float).
        risk_approval : dict
            Must contain: ``approved`` (bool), ``position_size_usd`` (Decimal),
            ``risk_per_trade_pct`` (Decimal), ``risk_reward_ratio`` (Decimal),
            ``kelly_fraction`` (Decimal), ``notes`` (str).
        market_data : dict
            Must contain: ``data_timestamps`` (dict), ``data_sources`` (list),
            ``price_validated`` (bool), ``signal_validated`` (bool),
            ``sanity_checks_passed`` (bool).

        Returns
        -------
        AuditReport
        """
        now = datetime.now(tz=timezone.utc)
        trade_id = signal.get("trade_id", signal.get("signal_id", str(uuid.uuid4())))

        # Build the report
        report = AuditReport(
            trade_id=str(trade_id),
            timestamp=now,
            # Data provenance
            data_timestamps=market_data.get("data_timestamps", {}),
            data_sources=market_data.get("data_sources", []),
            # Signal
            symbol=signal.get("symbol", ""),
            direction=signal.get("direction", ""),
            strategy_name=signal.get("strategy", ""),
            entry_price=Decimal(str(signal.get("entry_price", 0))),
            stop_loss=Decimal(str(signal.get("stop_loss", 0))),
            take_profit=Decimal(str(signal.get("take_profit", 0))),
            leverage=int(signal.get("leverage", 1)),
            confidence=float(signal.get("confidence", 0)),
            # Regime
            regime=regime.get("regime", "unknown"),
            regime_confidence=float(regime.get("confidence", 0)),
            # Risk math
            position_size_usd=Decimal(str(risk_approval.get("position_size_usd", 0))),
            risk_per_trade_pct=Decimal(str(risk_approval.get("risk_per_trade_pct", 0))),
            risk_reward_ratio=Decimal(str(risk_approval.get("risk_reward_ratio", 0))),
            kelly_fraction=Decimal(str(risk_approval.get("kelly_fraction", 0))),
            risk_approved=bool(risk_approval.get("approved", False)),
            risk_notes=str(risk_approval.get("notes", "")),
            # Anti-hallucination checks
            price_validated=bool(market_data.get("price_validated", False)),
            signal_validated=bool(market_data.get("signal_validated", False)),
            sanity_checks_passed=bool(market_data.get("sanity_checks_passed", False)),
        )

        # Generate devil's advocate reasons
        report.reasons_against = self._devils_advocate(report)

        # Determine final decision
        report.decision, report.decision_reasoning = self._resolve_decision(report)

        # Persist
        self._persist(report)

        logger.info(
            "Audit [%s] %s %s %s — decision=%s (devil's advocate: %d reasons against)",
            report.audit_id[:8],
            report.direction,
            report.symbol,
            report.strategy_name,
            report.decision,
            len(report.reasons_against),
        )

        return report

    def get_audit_trail(self, trade_id: str) -> AuditReport | None:
        """Retrieve the most recent audit report for *trade_id*.

        Returns None if no audit record exists.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT report_json FROM audit_trail WHERE trade_id = ? ORDER BY timestamp DESC LIMIT 1",
                (trade_id,),
            ).fetchone()

        if row is None:
            return None

        data = json.loads(row[0])
        return AuditReport(**data)

    def get_recent_audits(self, limit: int = 50) -> list[AuditReport]:
        """Return the most recent *limit* audit reports, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT report_json FROM audit_trail ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()

        reports: list[AuditReport] = []
        for (report_json,) in rows:
            try:
                reports.append(AuditReport(**json.loads(report_json)))
            except Exception as exc:
                logger.warning("Failed to deserialize audit row: %s", exc)
        return reports

    # -- devil's advocate ----------------------------------------------------

    @staticmethod
    def _devils_advocate(report: AuditReport) -> list[str]:
        """Generate concrete reasons NOT to take the trade.

        This is a programmatic check — not AI-generated — so it cannot
        hallucinate. It flags structural concerns that an eager agent might
        overlook.
        """
        reasons: list[str] = []

        # Low confidence
        if report.confidence < 60:
            reasons.append(
                f"Signal confidence is low ({report.confidence:.0f}% < 60%)"
            )

        # Regime mismatch
        if report.regime_confidence < 50:
            reasons.append(
                f"Regime detection confidence is low ({report.regime_confidence:.0f}% < 50%)"
            )

        # Weak risk/reward
        if report.risk_reward_ratio < Decimal("2"):
            reasons.append(
                f"Risk/reward ratio {report.risk_reward_ratio:.2f} is below 2.0"
            )

        # Large position relative to risk budget
        if report.risk_per_trade_pct > Decimal("0.02"):
            reasons.append(
                f"Risk per trade {report.risk_per_trade_pct:.2%} exceeds 2% guideline"
            )

        # High leverage
        if report.leverage >= 8:
            reasons.append(
                f"Leverage {report.leverage}x is high — liquidation risk elevated"
            )

        # Anti-hallucination flags
        if not report.price_validated:
            reasons.append("Price was NOT validated against exchange API")
        if not report.signal_validated:
            reasons.append("Signal indicators were NOT cross-validated with raw data")
        if not report.sanity_checks_passed:
            reasons.append("Sanity checks did NOT all pass")

        # Risk manager did not approve
        if not report.risk_approved:
            reasons.append("Risk Manager did NOT approve this trade")

        # Volatile regime with trend strategy
        trending_strategies = {"ema_crossover", "macd_momentum", "trend_follow"}
        ranging_strategies = {"mean_reversion", "bollinger_bounce", "rsi_reversal"}
        if report.regime in ("volatile", "choppy"):
            if report.strategy_name.lower() in trending_strategies:
                reasons.append(
                    f"Strategy '{report.strategy_name}' is trend-based but regime is '{report.regime}'"
                )
        if report.regime in ("strong_trend", "moderate_trend"):
            if report.strategy_name.lower() in ranging_strategies:
                reasons.append(
                    f"Strategy '{report.strategy_name}' is mean-reversion but regime is '{report.regime}'"
                )

        # Kelly fraction very low
        if Decimal("0") < report.kelly_fraction < Decimal("0.05"):
            reasons.append(
                f"Kelly fraction {report.kelly_fraction:.3f} suggests very small edge"
            )

        return reasons

    @staticmethod
    def _resolve_decision(report: AuditReport) -> tuple[str, str]:
        """Decide EXECUTE / REJECT / SKIP based on the audit checks.

        Returns ``(decision, reasoning)`` tuple.
        """
        # Hard rejects
        if not report.risk_approved:
            return "REJECT", "Risk Manager did not approve"
        if not report.price_validated:
            return "REJECT", "Price not validated — possible hallucination"
        if not report.signal_validated:
            return "REJECT", "Signal not validated against raw data"
        if not report.sanity_checks_passed:
            return "REJECT", "Math sanity checks failed"
        if report.risk_reward_ratio < Decimal("2"):
            return "REJECT", f"R/R {report.risk_reward_ratio:.2f} below minimum 2.0"

        # Soft concerns — too many devil's advocate flags triggers SKIP
        critical_count = len(report.reasons_against)
        if critical_count >= 4:
            return (
                "SKIP",
                f"{critical_count} concerns raised by devil's advocate — trade is marginal",
            )

        return "EXECUTE", "All checks passed"

    # -- persistence ---------------------------------------------------------

    def _persist(self, report: AuditReport) -> None:
        """Write the audit report to SQLite."""
        report_json = report.model_dump_json()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO audit_trail
                        (audit_id, trade_id, timestamp, symbol, direction,
                         strategy_name, regime, decision, report_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        report.audit_id,
                        report.trade_id,
                        report.timestamp.isoformat(),
                        report.symbol,
                        report.direction,
                        report.strategy_name,
                        report.regime,
                        report.decision,
                        report_json,
                    ),
                )
            logger.debug("Persisted audit %s to %s", report.audit_id[:8], self._db_path)
        except sqlite3.Error as exc:
            logger.error("Failed to persist audit %s: %s", report.audit_id[:8], exc)
