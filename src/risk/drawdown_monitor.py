"""
Drawdown Monitor — tracks peak balance, current drawdown, and maximum
historical drawdown.  State is persisted to disk as JSON so that
restarts do not lose the high-water mark.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger("claude_quant.risk.drawdown_monitor")

_DEFAULT_STATE_PATH = Path("user_data/agent_state/drawdown_state.json")


class DrawdownState(BaseModel):
    """Snapshot of drawdown metrics at a point in time."""

    model_config = {"frozen": True}

    current_balance: Decimal
    peak_balance: Decimal
    current_drawdown_pct: Decimal = Field(
        description="Current drawdown from peak, as a fraction (0-1)."
    )
    max_drawdown_pct: Decimal = Field(
        description="Worst observed drawdown from peak, as a fraction (0-1)."
    )
    max_drawdown_balance: Decimal = Field(
        description="Balance at the deepest drawdown point."
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class DrawdownMonitor:
    """
    Maintains a high-water mark and computes drawdown statistics.

    State is loaded from (and persisted to) a JSON file so that
    the peak-balance survives process restarts.
    """

    def __init__(
        self,
        state_path: Path | str = _DEFAULT_STATE_PATH,
        initial_balance: Decimal | float | str | None = None,
    ) -> None:
        self._state_path = Path(state_path)
        self._peak_balance: Decimal = Decimal("0")
        self._max_drawdown_pct: Decimal = Decimal("0")
        self._max_drawdown_balance: Decimal = Decimal("0")
        self._current_balance: Decimal = Decimal("0")

        loaded = self._load_state()
        if not loaded and initial_balance is not None:
            bal = Decimal(str(initial_balance))
            self._peak_balance = bal
            self._current_balance = bal
            self._persist_state()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def update(self, current_balance: Decimal | float | str) -> DrawdownState:
        """
        Feed a new balance observation and return updated drawdown state.

        This should be called after every trade close or on a regular
        polling interval.
        """
        bal = Decimal(str(current_balance))
        self._current_balance = bal

        # Update peak (high-water mark).
        if bal > self._peak_balance:
            self._peak_balance = bal
            logger.info("New peak balance: $%.2f", bal)

        # Current drawdown from peak.
        dd_pct = self._compute_drawdown(self._peak_balance, bal)

        # Update max drawdown.
        if dd_pct > self._max_drawdown_pct:
            self._max_drawdown_pct = dd_pct
            self._max_drawdown_balance = bal
            logger.warning(
                "New maximum drawdown: %.2f%% (balance $%.2f, peak $%.2f)",
                dd_pct * 100,
                bal,
                self._peak_balance,
            )

        self._persist_state()

        return DrawdownState(
            current_balance=bal,
            peak_balance=self._peak_balance,
            current_drawdown_pct=dd_pct,
            max_drawdown_pct=self._max_drawdown_pct,
            max_drawdown_balance=self._max_drawdown_balance,
        )

    def get_drawdown_pct(self) -> Decimal:
        """Return the current drawdown percentage as a fraction (0-1)."""
        return self._compute_drawdown(self._peak_balance, self._current_balance)

    def get_state(self) -> DrawdownState:
        """Return the last-known drawdown state without updating."""
        dd_pct = self._compute_drawdown(self._peak_balance, self._current_balance)
        return DrawdownState(
            current_balance=self._current_balance,
            peak_balance=self._peak_balance,
            current_drawdown_pct=dd_pct,
            max_drawdown_pct=self._max_drawdown_pct,
            max_drawdown_balance=self._max_drawdown_balance,
        )

    def reset(self, new_balance: Decimal | float | str) -> None:
        """
        Hard-reset all tracking (e.g. after a manual capital injection).

        Use sparingly — this wipes max-drawdown history.
        """
        bal = Decimal(str(new_balance))
        self._peak_balance = bal
        self._current_balance = bal
        self._max_drawdown_pct = Decimal("0")
        self._max_drawdown_balance = bal
        self._persist_state()
        logger.info("Drawdown monitor reset. New baseline: $%.2f", bal)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _persist_state(self) -> None:
        """Write current tracking data to disk."""
        data = {
            "peak_balance": str(self._peak_balance),
            "current_balance": str(self._current_balance),
            "max_drawdown_pct": str(self._max_drawdown_pct),
            "max_drawdown_balance": str(self._max_drawdown_balance),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.error("Failed to persist drawdown state: %s", exc)

    def _load_state(self) -> bool:
        """Attempt to restore state from disk. Returns True on success."""
        if not self._state_path.exists():
            return False
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            self._peak_balance = Decimal(raw["peak_balance"])
            self._current_balance = Decimal(raw["current_balance"])
            self._max_drawdown_pct = Decimal(raw["max_drawdown_pct"])
            self._max_drawdown_balance = Decimal(raw["max_drawdown_balance"])
            logger.info(
                "Drawdown state restored. Peak=$%.2f, MaxDD=%.2f%%",
                self._peak_balance,
                self._max_drawdown_pct * 100,
            )
            return True
        except (json.JSONDecodeError, KeyError, Exception) as exc:
            logger.warning("Could not load drawdown state: %s. Starting fresh.", exc)
            return False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    @staticmethod
    def _compute_drawdown(peak: Decimal, current: Decimal) -> Decimal:
        if peak <= Decimal("0"):
            return Decimal("0")
        dd = (peak - current) / peak
        return max(dd, Decimal("0")).quantize(Decimal("0.0001"))
