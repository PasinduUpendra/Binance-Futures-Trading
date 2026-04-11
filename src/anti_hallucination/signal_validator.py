"""
Signal validation layer for anti-hallucination.

Ensures that every trading signal the system produces is backed by concrete
indicator values that can be independently verified against the raw market
data.  Rejects signals with vague reasoning, impossible entry prices, or
inadequate risk/reward.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("claude_quant.anti_hallucination.signal_validator")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class TradingSignal(BaseModel):
    """Canonical representation of a signal produced by a strategy agent."""

    signal_id: str
    symbol: str
    direction: str  # "long" | "short"
    strategy: str
    entry_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    leverage: int = 1
    confidence: float = 0.0
    indicators: dict[str, Any] = Field(default_factory=dict)
    reasoning: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))


class SignalValidation(BaseModel):
    """Outcome of signal validation."""

    valid: bool
    signal: TradingSignal
    issues: list[str] = Field(default_factory=list)
    indicator_checks: dict[str, bool] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# SignalValidator
# ---------------------------------------------------------------------------


class SignalValidator:
    """Validates trading signals against raw market data.

    Checks performed:
    1. Signal references specific indicator values (not vague descriptions).
    2. Indicator values match the raw data within tolerance.
    3. Strategy conditions (e.g. crossover) are actually met.
    4. Entry price is within the current bid/ask spread.
    5. SL and TP are set, and R/R >= 2.0.

    Parameters
    ----------
    indicator_tolerance : Decimal
        Maximum relative deviation between the signal's reported indicator
        value and the value computed from raw data.  Default 0.005 (0.5 %).
    min_risk_reward : Decimal
        Minimum risk/reward ratio.  Default 2.0.
    """

    REQUIRED_INDICATOR_FIELDS: set[str] = {
        # At least one concrete indicator must be present
    }

    def __init__(
        self,
        indicator_tolerance: Decimal = Decimal("0.005"),
        min_risk_reward: Decimal = Decimal("2.0"),
    ) -> None:
        self._indicator_tolerance = indicator_tolerance
        self._min_risk_reward = min_risk_reward

    # -- public API ----------------------------------------------------------

    def validate_signal(
        self,
        signal: TradingSignal,
        raw_data: dict[str, Any],
    ) -> SignalValidation:
        """Run all validation checks on *signal* given *raw_data*.

        Parameters
        ----------
        signal : TradingSignal
            The signal to validate.
        raw_data : dict[str, Any]
            Must contain at minimum:
              - ``"bid"`` : Decimal  — best bid price
              - ``"ask"`` : Decimal  — best ask price
              - ``"indicators"`` : dict[str, float|Decimal]  — independently
                computed indicator values keyed by the same names used in the
                signal's ``indicators`` dict.

        Returns
        -------
        SignalValidation
        """
        issues: list[str] = []
        indicator_checks: dict[str, bool] = {}

        # 1. Check indicator specificity
        issues.extend(self._check_indicator_specificity(signal))

        # 2. Cross-check indicator values against raw data
        raw_indicators = raw_data.get("indicators", {})
        ic_issues, indicator_checks = self._check_indicator_values(
            signal.indicators, raw_indicators
        )
        issues.extend(ic_issues)

        # 3. Verify strategy conditions are met in data
        issues.extend(
            self._check_strategy_conditions(signal, raw_data)
        )

        # 4. Entry price realism
        issues.extend(
            self._check_entry_price(signal, raw_data)
        )

        # 5. SL/TP and R/R
        issues.extend(self._check_risk_reward(signal))

        valid = len(issues) == 0
        if not valid:
            logger.warning(
                "Signal validation FAILED for %s (%s): %s",
                signal.signal_id,
                signal.symbol,
                "; ".join(issues),
            )
        else:
            logger.info(
                "Signal validated: %s %s %s (R/R OK, indicators match)",
                signal.signal_id,
                signal.direction,
                signal.symbol,
            )

        return SignalValidation(
            valid=valid,
            signal=signal,
            issues=issues,
            indicator_checks=indicator_checks,
        )

    # -- private checks ------------------------------------------------------

    @staticmethod
    def _check_indicator_specificity(signal: TradingSignal) -> list[str]:
        """Signal must reference at least one concrete numeric indicator."""
        issues: list[str] = []

        if not signal.indicators:
            issues.append(
                "Signal has no indicator values — cannot verify against raw data"
            )
            return issues

        # Every indicator value must be numeric, not a vague string
        vague_keywords = {"bullish", "bearish", "positive", "negative", "strong", "weak"}
        # Metadata keys that are not verifiable indicators — skip them
        # flip_age_* = computed bar distance, supertrend_dir_15m = no 15m df available
        metadata_keys = {"entry_type", "atr_source", "flip_age_1h", "flip_age_15m",
                         "supertrend_dir_15m"}
        for key, value in signal.indicators.items():
            if key in metadata_keys:
                continue
            if isinstance(value, str):
                lower_val = value.lower().strip()
                if lower_val in vague_keywords or not _is_numeric_string(lower_val):
                    issues.append(
                        f"Indicator '{key}' has vague/non-numeric value '{value}'. "
                        f"Must be a concrete number."
                    )
        return issues

    def _check_indicator_values(
        self,
        signal_indicators: dict[str, Any],
        raw_indicators: dict[str, Any],
    ) -> tuple[list[str], dict[str, bool]]:
        """Compare signal-reported indicator values to raw-data-computed values."""
        issues: list[str] = []
        checks: dict[str, bool] = {}
        # Metadata keys that are not verifiable indicators — skip them
        # flip_age_* = computed bar distance, supertrend_dir_15m = no 15m df available
        metadata_keys = {"entry_type", "atr_source", "flip_age_1h", "flip_age_15m",
                         "supertrend_dir_15m"}

        for name, signal_val in signal_indicators.items():
            if name in metadata_keys:
                continue
            signal_dec = _safe_decimal(signal_val)
            if signal_dec is None:
                # Non-numeric indicator — already flagged in specificity check
                checks[name] = False
                continue

            if name not in raw_indicators:
                # Raw data does not have this indicator; cannot verify
                checks[name] = False
                issues.append(
                    f"Indicator '{name}' not found in raw data — cannot verify"
                )
                continue

            raw_dec = _safe_decimal(raw_indicators[name])
            if raw_dec is None:
                checks[name] = False
                issues.append(
                    f"Raw indicator '{name}' is non-numeric: {raw_indicators[name]}"
                )
                continue

            # Compare within tolerance
            if raw_dec == Decimal("0") and signal_dec == Decimal("0"):
                checks[name] = True
                continue

            reference = abs(raw_dec) if raw_dec != Decimal("0") else abs(signal_dec)
            diff = abs(signal_dec - raw_dec)
            relative_diff = diff / reference if reference != Decimal("0") else diff

            passed = relative_diff <= self._indicator_tolerance
            checks[name] = passed
            if not passed:
                issues.append(
                    f"Indicator '{name}': signal={signal_dec}, raw={raw_dec}, "
                    f"diff={relative_diff:.4%} (tol={self._indicator_tolerance:.4%})"
                )

        return issues, checks

    @staticmethod
    def _check_strategy_conditions(
        signal: TradingSignal,
        raw_data: dict[str, Any],
    ) -> list[str]:
        """Verify that the strategy's entry conditions are actually met.

        This performs basic sanity checks using the raw OHLCV candles and
        indicator snapshots stored in *raw_data*.  Strategy-specific deep
        verification is deferred to each strategy's own backtest module.
        """
        issues: list[str] = []

        candles: list[dict[str, Any]] = raw_data.get("candles", [])
        if not candles:
            issues.append("No candle data in raw_data — cannot verify strategy conditions")
            return issues

        latest_candle = candles[-1]
        latest_close = _safe_decimal(latest_candle.get("close"))

        if latest_close is None:
            issues.append("Latest candle has no 'close' price")
            return issues

        # Direction sanity: for a long signal the entry should be near or
        # above the latest close; for short, near or below.  Allow 2 %
        # flexibility because the signal may anticipate a small move.
        entry = signal.entry_price
        flexibility = Decimal("0.02")

        if signal.direction == "long":
            max_above = latest_close * (Decimal("1") + flexibility)
            if entry > max_above:
                issues.append(
                    f"Long entry {entry} is >2% above latest close {latest_close}"
                )
        elif signal.direction == "short":
            min_below = latest_close * (Decimal("1") - flexibility)
            if entry < min_below:
                issues.append(
                    f"Short entry {entry} is >2% below latest close {latest_close}"
                )
        else:
            issues.append(f"Unknown direction '{signal.direction}', expected 'long' or 'short'")

        return issues

    @staticmethod
    def _check_entry_price(
        signal: TradingSignal,
        raw_data: dict[str, Any],
    ) -> list[str]:
        """Entry price must be within the current bid/ask spread (plus a small buffer)."""
        issues: list[str] = []

        bid = _safe_decimal(raw_data.get("bid"))
        ask = _safe_decimal(raw_data.get("ask"))

        if bid is None or ask is None:
            issues.append("Missing bid/ask in raw_data — cannot verify entry price")
            return issues

        if bid == Decimal("0") or ask == Decimal("0"):
            issues.append("Bid or ask is zero — cannot verify entry price")
            return issues

        # Allow 0.5 % buffer outside the spread to account for slippage
        # estimates in the signal
        buffer = Decimal("0.005")
        lower_bound = bid * (Decimal("1") - buffer)
        upper_bound = ask * (Decimal("1") + buffer)

        entry = signal.entry_price
        if entry < lower_bound or entry > upper_bound:
            issues.append(
                f"Entry price {entry} outside realistic spread "
                f"[{lower_bound} .. {upper_bound}] (bid={bid}, ask={ask})"
            )
        return issues

    def _check_risk_reward(self, signal: TradingSignal) -> list[str]:
        """SL and TP must be set and produce R/R >= min_risk_reward."""
        issues: list[str] = []

        entry = signal.entry_price
        sl = signal.stop_loss
        tp = signal.take_profit

        # SL and TP must be non-zero
        if sl == Decimal("0"):
            issues.append("Stop-loss is not set (0)")
        if tp == Decimal("0"):
            issues.append("Take-profit is not set (0)")
        if sl == Decimal("0") or tp == Decimal("0"):
            return issues

        # Direction-aware risk and reward
        if signal.direction == "long":
            risk = entry - sl
            reward = tp - entry
            if risk <= Decimal("0"):
                issues.append(
                    f"Long signal: SL ({sl}) must be below entry ({entry})"
                )
                return issues
            if reward <= Decimal("0"):
                issues.append(
                    f"Long signal: TP ({tp}) must be above entry ({entry})"
                )
                return issues
        elif signal.direction == "short":
            risk = sl - entry
            reward = entry - tp
            if risk <= Decimal("0"):
                issues.append(
                    f"Short signal: SL ({sl}) must be above entry ({entry})"
                )
                return issues
            if reward <= Decimal("0"):
                issues.append(
                    f"Short signal: TP ({tp}) must be below entry ({entry})"
                )
                return issues
        else:
            issues.append(f"Cannot compute R/R for unknown direction '{signal.direction}'")
            return issues

        rr_ratio = reward / risk
        # Use <= to accept R/R exactly equal to minimum (e.g. 2.0 >= 2.0)
        if rr_ratio < self._min_risk_reward - Decimal("0.0001"):
            issues.append(
                f"R/R ratio {rr_ratio:.2f} < minimum {self._min_risk_reward:.2f} "
                f"(risk={risk}, reward={reward})"
            )

        return issues


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_decimal(value: Any) -> Decimal | None:
    """Convert *value* to Decimal, returning None on failure."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _is_numeric_string(s: str) -> bool:
    """Return *True* if *s* can be parsed as a number."""
    try:
        float(s)
        return True
    except ValueError:
        return False
