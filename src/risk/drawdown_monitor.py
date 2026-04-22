"""
Drawdown Monitor — tracks peak balance, current drawdown, and maximum
historical drawdown.

Sprint 1: primary persistence is now the ``system_state`` table inside
``user_data/claude_quant.db``. The legacy JSON file is read ONCE at
startup (to migrate state forward) and is no longer written to by this
module — eliminating the dual-write risk called out in
``docs/DRIFT_MAP.md §9``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("claude_quant.risk.drawdown_monitor")

_DEFAULT_STATE_PATH = Path("user_data/agent_state/drawdown_state.json")
_STATE_PREFIX = "drawdown."


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
        db: Any | None = None,
    ) -> None:
        self._state_path = Path(state_path)
        self._db = db  # DatabaseManager; primary persistence if attached
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

    def attach_db(self, db: Any) -> None:
        """Attach a ``DatabaseManager`` as the primary persistence target.

        Once attached, ``_persist_state`` writes to the ``system_state``
        table (keys prefixed ``drawdown.``) and stops writing the legacy
        JSON file. The JSON file remains on disk as a read-only artifact
        for older tooling (``watchdog_tools.py`` still reads it).

        Persistence is lazy — state is written on the next ``update()`` or
        ``reset()`` call, not on attach. This keeps tests that construct
        ``Orchestrator()`` side-effect-free when they later override
        in-memory state.
        """
        self._db = db

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

    @property
    def peak_balance(self) -> Decimal:
        """The all-time high balance tracked by this monitor."""
        return self._peak_balance

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
        """Write current tracking data — DB-first, JSON-fallback."""
        data = {
            "peak_balance": str(self._peak_balance),
            "current_balance": str(self._current_balance),
            "max_drawdown_pct": str(self._max_drawdown_pct),
            "max_drawdown_balance": str(self._max_drawdown_balance),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        if self._db is not None:
            # Primary: system_state table inside the canonical DB.
            try:
                for k, v in data.items():
                    self._db.set_state(f"{_STATE_PREFIX}{k}", v)
                return
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "DB persist failed — falling back to JSON: %s", exc,
                )

        # Fallback: atomic JSON write (legacy, pre-Sprint-1 behaviour).
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._state_path.with_suffix(".tmp")
            tmp_path.write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )
            tmp_path.rename(self._state_path)  # atomic on POSIX
        except OSError as exc:
            logger.error("Failed to persist drawdown state: %s", exc)

    def _load_state(self) -> bool:
        """Attempt to restore state. Tries DB first, then legacy JSON."""
        if self._db is not None:
            try:
                peak = self._db.get_state(f"{_STATE_PREFIX}peak_balance")
                if peak is not None:
                    self._peak_balance = Decimal(peak)
                    self._current_balance = Decimal(
                        self._db.get_state(f"{_STATE_PREFIX}current_balance", "0")
                    )
                    self._max_drawdown_pct = Decimal(
                        self._db.get_state(f"{_STATE_PREFIX}max_drawdown_pct", "0")
                    )
                    self._max_drawdown_balance = Decimal(
                        self._db.get_state(
                            f"{_STATE_PREFIX}max_drawdown_balance", "0",
                        )
                    )
                    logger.info(
                        "Drawdown state restored from DB. "
                        "Peak=$%.2f, MaxDD=%.2f%%",
                        self._peak_balance,
                        self._max_drawdown_pct * 100,
                    )
                    return True
            except Exception as exc:  # noqa: BLE001
                logger.warning("DB load failed — trying JSON: %s", exc)

        if not self._state_path.exists():
            return False
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            self._peak_balance = Decimal(raw["peak_balance"])
            self._current_balance = Decimal(raw["current_balance"])
            self._max_drawdown_pct = Decimal(raw["max_drawdown_pct"])
            self._max_drawdown_balance = Decimal(raw["max_drawdown_balance"])
            logger.info(
                "Drawdown state restored from legacy JSON. "
                "Peak=$%.2f, MaxDD=%.2f%%",
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

    # ------------------------------------------------------------------
    # Rolling equity drawdown halt (R-C2)
    # ------------------------------------------------------------------
    def should_halt_rolling(
        self,
        rolling_threshold: Decimal = Decimal("0.15"),
    ) -> bool:
        """Return True if current drawdown from peak exceeds rolling threshold.

        The existing daily-loss halt covers intraday disasters. This check
        covers multi-day equity erosion: if equity drops > *rolling_threshold*
        (default 15%) from the all-time high-water mark, halt for 24h.

        Parameters
        ----------
        rolling_threshold : Decimal
            Maximum allowed drawdown from peak as a fraction (e.g. 0.15 = 15%).

        Returns
        -------
        bool
            True if trading should be halted.
        """
        dd = self._compute_drawdown(self._peak_balance, self._current_balance)
        if dd >= rolling_threshold:
            logger.warning(
                "ROLLING DRAWDOWN HALT: %.2f%% drawdown from peak $%.2f "
                "(current $%.2f, threshold %.0f%%)",
                float(dd) * 100,
                self._peak_balance,
                self._current_balance,
                float(rolling_threshold) * 100,
            )
            return True
        return False
