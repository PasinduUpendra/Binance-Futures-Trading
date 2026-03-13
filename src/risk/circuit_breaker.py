"""
Circuit Breaker — the single most critical safety component of Claude Quant.

Thresholds are HARDCODED as module-level constants and frozen Pydantic models.
No agent, configuration file, or runtime code path may alter them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Final

from pydantic import BaseModel, Field

logger = logging.getLogger("claude_quant.risk.circuit_breaker")

# ---------------------------------------------------------------------------
# IMMUTABLE THRESHOLD CONSTANTS — DO NOT MODIFY
# ---------------------------------------------------------------------------
_GREEN_BALANCE_MIN: Final[Decimal] = Decimal("60")
_YELLOW_BALANCE_MIN: Final[Decimal] = Decimal("45")
_RED_BALANCE_MIN: Final[Decimal] = Decimal("30")
# Below RED → DEAD

_GREEN_MAX_LEVERAGE: Final[int] = 10
_YELLOW_MAX_LEVERAGE: Final[int] = 5
_RED_MAX_LEVERAGE: Final[int] = 3

_GREEN_MAX_POSITIONS: Final[int] = 3
_YELLOW_MAX_POSITIONS: Final[int] = 2
_RED_MAX_POSITIONS: Final[int] = 1

_GREEN_SIZE_MULTIPLIER: Final[Decimal] = Decimal("1.0")
_YELLOW_SIZE_MULTIPLIER: Final[Decimal] = Decimal("0.5")
_RED_SIZE_MULTIPLIER: Final[Decimal] = Decimal("0.25")

_RED_MIN_WIN_RATE_LAST_10: Final[Decimal] = Decimal("0.6667")  # 2/3

_DAILY_LOSS_HALT_PCT: Final[Decimal] = Decimal("0.10")
_CONSECUTIVE_LOSS_PAUSE_THRESHOLD: Final[int] = 5
_CONSECUTIVE_LOSS_PAUSE_SECONDS: Final[int] = 7200  # 2 hours


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class CircuitBreakerLevel(str, Enum):
    """Severity levels for the circuit breaker — ordered GREEN → DEAD."""

    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"
    DEAD = "DEAD"


# ---------------------------------------------------------------------------
# Pydantic models (frozen where possible)
# ---------------------------------------------------------------------------
class CircuitBreakerConstraints(BaseModel):
    """Constraints imposed by the current circuit-breaker level."""

    model_config = {"frozen": True}

    level: CircuitBreakerLevel
    max_leverage: int = Field(ge=0)
    max_positions: int = Field(ge=0)
    size_multiplier: Decimal = Field(ge=Decimal("0"))
    trading_allowed: bool
    reason: str = ""


class CircuitBreakerState(BaseModel):
    """Full snapshot of the circuit breaker at a point in time."""

    model_config = {"frozen": True}

    level: CircuitBreakerLevel
    constraints: CircuitBreakerConstraints
    balance: Decimal
    daily_loss_pct: Decimal = Decimal("0")
    daily_loss_halt: bool = False
    consecutive_loss_pause: bool = False
    pause_until_utc: datetime | None = None
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TradeResult(BaseModel):
    """Minimal trade result used for win-rate lookups."""

    is_win: bool
    closed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------
class CircuitBreaker:
    """
    Enforces hard safety limits on trading activity.

    All thresholds are baked into module-level ``Final`` constants.
    This class exposes read-only queries; it never mutates its own limits.
    """

    # Expose constants as class-level read-only properties for introspection
    # but provide no setters.
    @staticmethod
    def thresholds_summary() -> dict[str, object]:
        """Return a read-only snapshot of every threshold (for dashboards)."""
        return {
            "green_balance_min": _GREEN_BALANCE_MIN,
            "yellow_balance_min": _YELLOW_BALANCE_MIN,
            "red_balance_min": _RED_BALANCE_MIN,
            "green_max_leverage": _GREEN_MAX_LEVERAGE,
            "yellow_max_leverage": _YELLOW_MAX_LEVERAGE,
            "red_max_leverage": _RED_MAX_LEVERAGE,
            "green_max_positions": _GREEN_MAX_POSITIONS,
            "yellow_max_positions": _YELLOW_MAX_POSITIONS,
            "red_max_positions": _RED_MAX_POSITIONS,
            "green_size_multiplier": _GREEN_SIZE_MULTIPLIER,
            "yellow_size_multiplier": _YELLOW_SIZE_MULTIPLIER,
            "red_size_multiplier": _RED_SIZE_MULTIPLIER,
            "red_min_win_rate_last_10": _RED_MIN_WIN_RATE_LAST_10,
            "daily_loss_halt_pct": _DAILY_LOSS_HALT_PCT,
            "consecutive_loss_pause_threshold": _CONSECUTIVE_LOSS_PAUSE_THRESHOLD,
            "consecutive_loss_pause_seconds": _CONSECUTIVE_LOSS_PAUSE_SECONDS,
        }

    # ------------------------------------------------------------------
    # Core queries
    # ------------------------------------------------------------------
    @staticmethod
    def check_level(balance: Decimal) -> CircuitBreakerLevel:
        """Determine the circuit-breaker level based solely on balance."""
        balance = Decimal(str(balance))
        if balance >= _GREEN_BALANCE_MIN:
            return CircuitBreakerLevel.GREEN
        if balance >= _YELLOW_BALANCE_MIN:
            return CircuitBreakerLevel.YELLOW
        if balance >= _RED_BALANCE_MIN:
            return CircuitBreakerLevel.RED
        return CircuitBreakerLevel.DEAD

    @staticmethod
    def get_constraints(balance: Decimal) -> CircuitBreakerConstraints:
        """Return the hard constraints for the given balance."""
        balance = Decimal(str(balance))
        level = CircuitBreaker.check_level(balance)

        if level == CircuitBreakerLevel.GREEN:
            return CircuitBreakerConstraints(
                level=level,
                max_leverage=_GREEN_MAX_LEVERAGE,
                max_positions=_GREEN_MAX_POSITIONS,
                size_multiplier=_GREEN_SIZE_MULTIPLIER,
                trading_allowed=True,
                reason="Normal trading — all systems green.",
            )
        if level == CircuitBreakerLevel.YELLOW:
            return CircuitBreakerConstraints(
                level=level,
                max_leverage=_YELLOW_MAX_LEVERAGE,
                max_positions=_YELLOW_MAX_POSITIONS,
                size_multiplier=_YELLOW_SIZE_MULTIPLIER,
                trading_allowed=True,
                reason="Reduced-risk mode. Size halved, leverage capped at 5x.",
            )
        if level == CircuitBreakerLevel.RED:
            return CircuitBreakerConstraints(
                level=level,
                max_leverage=_RED_MAX_LEVERAGE,
                max_positions=_RED_MAX_POSITIONS,
                size_multiplier=_RED_SIZE_MULTIPLIER,
                trading_allowed=True,  # may be overridden by win-rate gate
                reason=(
                    "Survival mode. 1 position max, 3x leverage. "
                    "Must maintain >=2/3 win rate in last 10 trades."
                ),
            )
        # DEAD
        return CircuitBreakerConstraints(
            level=level,
            max_leverage=0,
            max_positions=0,
            size_multiplier=Decimal("0"),
            trading_allowed=False,
            reason="DEAD — balance below $30. ALL TRADING HALTED.",
        )

    # ------------------------------------------------------------------
    # Win-rate gate for RED level
    # ------------------------------------------------------------------
    @staticmethod
    def _check_red_win_rate(recent_trades: list[TradeResult]) -> bool:
        """Return True if the win rate in the last 10 trades meets the RED gate."""
        last_10 = recent_trades[-10:] if len(recent_trades) >= 10 else recent_trades
        if not last_10:
            # No history — cautiously deny trading in RED.
            return False
        wins = sum(1 for t in last_10 if t.is_win)
        win_rate = Decimal(wins) / Decimal(len(last_10))
        return win_rate >= _RED_MIN_WIN_RATE_LAST_10

    # ------------------------------------------------------------------
    # Consecutive-loss pause
    # ------------------------------------------------------------------
    @staticmethod
    def _check_consecutive_losses(recent_trades: list[TradeResult]) -> bool:
        """Return True if there is an active pause due to consecutive losses."""
        if len(recent_trades) < _CONSECUTIVE_LOSS_PAUSE_THRESHOLD:
            return False
        tail = recent_trades[-_CONSECUTIVE_LOSS_PAUSE_THRESHOLD:]
        return all(not t.is_win for t in tail)

    @staticmethod
    def _consecutive_loss_pause_end(recent_trades: list[TradeResult]) -> datetime | None:
        """Compute the UTC timestamp when the pause lifts (if applicable)."""
        if not CircuitBreaker._check_consecutive_losses(recent_trades):
            return None
        # Pause is anchored to the close time of the most recent loss.
        from datetime import timedelta

        last_close = recent_trades[-1].closed_at
        return last_close + timedelta(seconds=_CONSECUTIVE_LOSS_PAUSE_SECONDS)

    # ------------------------------------------------------------------
    # Daily loss check
    # ------------------------------------------------------------------
    @staticmethod
    def check_daily_loss(
        start_balance: Decimal, current_balance: Decimal
    ) -> tuple[bool, Decimal]:
        """
        Check whether daily loss exceeds the halt threshold.

        Returns:
            (halt: bool, loss_pct: Decimal)
        """
        start_balance = Decimal(str(start_balance))
        current_balance = Decimal(str(current_balance))
        if start_balance <= Decimal("0"):
            return True, Decimal("1")
        loss_pct = (start_balance - current_balance) / start_balance
        halt = loss_pct >= _DAILY_LOSS_HALT_PCT
        if halt:
            logger.critical(
                "DAILY LOSS HALT triggered. start=%.2f current=%.2f loss_pct=%.4f",
                start_balance,
                current_balance,
                loss_pct,
            )
        return halt, loss_pct

    # ------------------------------------------------------------------
    # Composite readiness check
    # ------------------------------------------------------------------
    @staticmethod
    def is_trading_allowed(
        balance: Decimal,
        recent_trades: list[TradeResult] | None = None,
        start_of_day_balance: Decimal | None = None,
        now: datetime | None = None,
    ) -> CircuitBreakerState:
        """
        The single authoritative gate for whether a new trade may be opened.

        Parameters
        ----------
        balance:
            Current account balance (USD).
        recent_trades:
            Ordered list of recent trade results (oldest first).
        start_of_day_balance:
            Balance at UTC midnight for daily-loss computation.
        now:
            Override for current UTC time (useful in tests).

        Returns
        -------
        CircuitBreakerState
            Frozen model with the full safety picture.
        """
        balance = Decimal(str(balance))
        now = now or datetime.now(timezone.utc)
        recent_trades = recent_trades or []
        constraints = CircuitBreaker.get_constraints(balance)

        # --- 1. DEAD check (overrides everything) --------------------------
        if constraints.level == CircuitBreakerLevel.DEAD:
            logger.critical(
                "Circuit breaker DEAD — balance $%.2f < $%.2f. TRADING HALTED.",
                balance,
                _RED_BALANCE_MIN,
            )
            return CircuitBreakerState(
                level=CircuitBreakerLevel.DEAD,
                constraints=constraints,
                balance=balance,
                checked_at=now,
            )

        # --- 2. Daily-loss halt -------------------------------------------
        daily_loss_halt = False
        daily_loss_pct = Decimal("0")
        if start_of_day_balance is not None:
            daily_loss_halt, daily_loss_pct = CircuitBreaker.check_daily_loss(
                start_of_day_balance, balance
            )
            if daily_loss_halt:
                halt_constraints = CircuitBreakerConstraints(
                    level=constraints.level,
                    max_leverage=0,
                    max_positions=0,
                    size_multiplier=Decimal("0"),
                    trading_allowed=False,
                    reason=(
                        f"Daily loss halt: {daily_loss_pct:.2%} loss exceeds "
                        f"{_DAILY_LOSS_HALT_PCT:.0%} threshold. Resume next UTC day."
                    ),
                )
                return CircuitBreakerState(
                    level=constraints.level,
                    constraints=halt_constraints,
                    balance=balance,
                    daily_loss_pct=daily_loss_pct,
                    daily_loss_halt=True,
                    checked_at=now,
                )

        # --- 3. Consecutive-loss pause ------------------------------------
        consecutive_loss_pause = CircuitBreaker._check_consecutive_losses(recent_trades)
        pause_until: datetime | None = None
        if consecutive_loss_pause:
            pause_until = CircuitBreaker._consecutive_loss_pause_end(recent_trades)
            if pause_until is not None and now < pause_until:
                pause_constraints = CircuitBreakerConstraints(
                    level=constraints.level,
                    max_leverage=0,
                    max_positions=0,
                    size_multiplier=Decimal("0"),
                    trading_allowed=False,
                    reason=(
                        f"{_CONSECUTIVE_LOSS_PAUSE_THRESHOLD} consecutive losses. "
                        f"Paused until {pause_until.isoformat()}."
                    ),
                )
                return CircuitBreakerState(
                    level=constraints.level,
                    constraints=pause_constraints,
                    balance=balance,
                    daily_loss_pct=daily_loss_pct,
                    consecutive_loss_pause=True,
                    pause_until_utc=pause_until,
                    checked_at=now,
                )
            # Pause has already elapsed — clear it.
            consecutive_loss_pause = False
            pause_until = None

        # --- 4. RED win-rate gate -----------------------------------------
        if constraints.level == CircuitBreakerLevel.RED:
            if not CircuitBreaker._check_red_win_rate(recent_trades):
                gate_constraints = CircuitBreakerConstraints(
                    level=CircuitBreakerLevel.RED,
                    max_leverage=0,
                    max_positions=0,
                    size_multiplier=Decimal("0"),
                    trading_allowed=False,
                    reason=(
                        "RED level: win rate in last 10 trades is below 2/3. "
                        "Trading blocked until win rate improves."
                    ),
                )
                return CircuitBreakerState(
                    level=CircuitBreakerLevel.RED,
                    constraints=gate_constraints,
                    balance=balance,
                    daily_loss_pct=daily_loss_pct,
                    checked_at=now,
                )

        # --- 5. All clear -------------------------------------------------
        logger.info(
            "Circuit breaker %s — balance $%.2f, leverage cap %dx, %d positions max.",
            constraints.level.value,
            balance,
            constraints.max_leverage,
            constraints.max_positions,
        )
        return CircuitBreakerState(
            level=constraints.level,
            constraints=constraints,
            balance=balance,
            daily_loss_pct=daily_loss_pct,
            checked_at=now,
        )
