"""
DecisionLogger — per-stage decision funnel recorder.

One row per (cycle_id, symbol, stage, outcome) written to the
``decision_log`` table in the canonical DB.  Every caller uses
``DecisionLogger.log()`` rather than calling DatabaseManager directly.

Design goals
------------
- Never raises into a hot path.  All exceptions are caught and logged.
- Validates stage and outcome against their enumerations.
- Serialises ``numeric_context`` dict to JSON.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.data.database import DatabaseManager

logger = logging.getLogger("claude_quant.memory.decision_logger")

# ---------------------------------------------------------------------------
# Stage enum (matches LIVE_FORENSICS_SPEC.md §2.1)
# ---------------------------------------------------------------------------

VALID_STAGES: frozenset[str] = frozenset(
    {
        "data_fetch_4h",
        "data_fetch_1h",
        "data_fetch_15m",
        "regime_detect",
        "signal_generate",
        "confidence_gate",
        "cross_asset_consensus_adjust",
        "position_overlap_skip",
        "funding_filter",
        "leverage_determine",
        "volatility_adjust",
        "sizing",
        "min_notional",
        "liquidation_buffer",
        "price_validate",
        "signal_validate",
        "decision_audit",
        # Phase-1B stages (not wired yet — listed so validation passes)
        "post_only_attempt",
        "market_fallback",
        "sl_place",
        "tp_place",
        "native_trail_place",
        "position_open_recorded",
    }
)

VALID_OUTCOMES: frozenset[str] = frozenset({"pass", "reject", "skip", "error"})


# ---------------------------------------------------------------------------
# DecisionLogger
# ---------------------------------------------------------------------------


class DecisionLogger:
    """Thin wrapper that writes one ``decision_log`` row per call.

    Parameters
    ----------
    db : DatabaseManager
        The canonical database manager instance shared with the orchestrator.
    """

    def __init__(self, db: "DatabaseManager") -> None:
        self._db = db

    def log(
        self,
        cycle_id: int,
        symbol: str,
        stage: str,
        outcome: str,
        reason: str = "",
        numeric_context: dict[str, Any] | None = None,
        cascade_level: str | None = None,
        confidence: float | None = None,
        regime: str | None = None,
    ) -> None:
        """Write one decision-funnel row.

        Parameters
        ----------
        cycle_id : int
            Row-id from ``cycle_history``.  Must be > 0; calls with
            ``cycle_id=0`` are silently dropped (no valid FK target).
        symbol : str
            Trading pair in ccxt format, e.g. ``"ETH/USDT:USDT"``.
        stage : str
            One of ``VALID_STAGES``.
        outcome : str
            One of ``'pass'``, ``'reject'``, ``'skip'``, ``'error'``.
        reason : str
            Short human-readable description (no newlines).
        numeric_context : dict | None
            Arbitrary JSON-serialisable key/value pairs.  Keep small.
        cascade_level : str | None
            Signal cascade level when relevant, e.g. ``"continuation"``.
        confidence : float | None
            Signal confidence score when relevant (0–100).
        regime : str | None
            Detected market regime when relevant.
        """
        if cycle_id <= 0:
            # No valid FK target — skip silently.
            return

        if stage not in VALID_STAGES:
            logger.warning("DecisionLogger: unknown stage %r — skipping", stage)
            return

        if outcome not in VALID_OUTCOMES:
            logger.warning("DecisionLogger: unknown outcome %r — skipping", outcome)
            return

        nc_json: str | None = None
        if numeric_context:
            try:
                nc_json = json.dumps(numeric_context, default=str)
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "DecisionLogger: could not serialise numeric_context: %s", exc
                )

        ts = datetime.now(timezone.utc).isoformat()

        try:
            self._db.insert_decision_log(
                cycle_id=cycle_id,
                timestamp_utc=ts,
                symbol=symbol,
                stage=stage,
                outcome=outcome,
                reason=reason or None,
                numeric_context=nc_json,
                cascade_level=cascade_level,
                confidence=confidence,
                regime=regime,
            )
        except Exception as exc:  # noqa: BLE001 — never block the trading path
            logger.warning(
                "DecisionLogger: DB write failed for %s/%s/%s: %s",
                symbol, stage, outcome, exc,
            )
