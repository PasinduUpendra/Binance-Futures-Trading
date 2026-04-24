"""Phase 2B reduced live mode tests.

These tests assert the *outcome* of reduced-live-mode flags at the points
that actually drive live behavior:

- Orchestrator ``TRADING_PAIRS`` is filtered to {SOL, SUI}.
- Disabled cascade levels (15m fast, aligned trend) do not fire.
- Disabled strategy routes (AdaptiveTrend, BreakoutTrader) return NONE.
- Cross-asset consensus is neutralized (empty map OR zero per pair).
- Dynamic +1 position override does not grant an extra slot.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.orchestrator import reduced_live_mode as rlm
from src.risk.circuit_breaker import (
    CircuitBreakerConstraints,
    CircuitBreakerLevel,
)
from src.strategies.adaptive_strategy import AdaptiveStrategy
from src.strategies.base_strategy import Signal, SignalDirection
from src.strategies.regime_detector import MarketRegime, RegimeState


# ---------------------------------------------------------------------------
# Flag sanity
# ---------------------------------------------------------------------------


def test_reduced_mode_is_active() -> None:
    assert rlm.REDUCED_LIVE_MODE is True
    assert rlm.is_reduced_mode() is True


def test_allowed_symbols_are_only_sol_and_sui() -> None:
    assert rlm.ALLOWED_SYMBOLS == frozenset({
        "SOL/USDT:USDT", "SUI/USDT:USDT",
    })


def test_disabled_flags_are_disabled() -> None:
    assert rlm.ALLOW_15M_FAST is False
    assert rlm.ALLOW_ALIGNED_TREND is False
    assert rlm.ALLOW_ADAPTIVE_TREND_ROUTE is False
    assert rlm.ALLOW_BREAKOUT_TRADER_ROUTE is False
    assert rlm.ALLOW_CONSENSUS_ADJUST is False
    assert rlm.ALLOW_DYNAMIC_POS_OVERRIDE is False


def test_enabled_flags_are_enabled() -> None:
    assert rlm.ALLOW_4H_FLIP is True
    assert rlm.ALLOW_1H_CONTINUATION is True


# ---------------------------------------------------------------------------
# Pair filtering
# ---------------------------------------------------------------------------


def test_filter_trading_pairs_keeps_only_sol_sui() -> None:
    full = [
        "ETH/USDT:USDT", "SOL/USDT:USDT", "DOGE/USDT:USDT",
        "XRP/USDT:USDT", "LINK/USDT:USDT", "AVAX/USDT:USDT",
        "SUI/USDT:USDT", "ADA/USDT:USDT",
    ]
    assert rlm.filter_trading_pairs(full) == [
        "SOL/USDT:USDT", "SUI/USDT:USDT",
    ]


def test_filter_trading_pairs_preserves_order() -> None:
    # SUI first in input → SUI first in output
    assert rlm.filter_trading_pairs([
        "SUI/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
    ]) == ["SUI/USDT:USDT", "SOL/USDT:USDT"]


def test_filter_trading_pairs_does_not_inject_missing() -> None:
    # SUI absent from input → SUI NOT injected
    assert rlm.filter_trading_pairs(["SOL/USDT:USDT"]) == ["SOL/USDT:USDT"]


def test_orchestrator_trading_pairs_respects_reduced_mode() -> None:
    # Import the live symbol list that the running orchestrator actually
    # iterates on for signal generation. Under reduced mode this must be
    # exactly SOL+SUI. Note: the FULL ``TRADING_PAIRS`` universe is
    # preserved for orphan-order / startup cleanup — only the active
    # signal loop is narrowed.
    from src.orchestrator.main import ACTIVE_TRADING_PAIRS, TRADING_PAIRS

    assert set(ACTIVE_TRADING_PAIRS) == {"SOL/USDT:USDT", "SUI/USDT:USDT"}
    # Reconciliation set remains full so orphans on previously-allowed
    # pairs keep getting cleaned.
    assert "ETH/USDT:USDT" in TRADING_PAIRS
    assert "DOGE/USDT:USDT" in TRADING_PAIRS


def test_is_pair_allowed() -> None:
    assert rlm.is_pair_allowed("SOL/USDT:USDT") is True
    assert rlm.is_pair_allowed("SUI/USDT:USDT") is True
    assert rlm.is_pair_allowed("ETH/USDT:USDT") is False
    assert rlm.is_pair_allowed("BTC/USDT:USDT") is False


# ---------------------------------------------------------------------------
# Cascade-level gating
# ---------------------------------------------------------------------------


def test_cascade_flags_gate() -> None:
    assert rlm.is_cascade_level_allowed("4h_flip") is True
    assert rlm.is_cascade_level_allowed("1h_continuation") is True
    assert rlm.is_cascade_level_allowed("15m_fast") is False
    assert rlm.is_cascade_level_allowed("aligned_trend") is False
    # unknown level: fail-open
    assert rlm.is_cascade_level_allowed("future_unknown") is True


def _regime(reg: MarketRegime, adx: float = 20.0) -> RegimeState:
    return RegimeState(
        regime=reg,
        adx=adx,
        bb_width_ratio=1.0,
        atr_ratio=1.0,
        volume_ratio=1.0,
        confidence=1.0,
    )


def test_supertrend_fast_and_aligned_cascade_not_called_in_reduced_mode() -> None:
    """Under reduced mode, only 4H flip + 1H continuation should be tried."""

    strat = AdaptiveStrategy()
    # Force regime detection to return TRENDING (ADX ≥ 18 → SupertrendTrend)
    strat._regime_detector = MagicMock()
    strat._regime_detector.detect.return_value = _regime(
        MarketRegime.TRENDING, adx=25.0,
    )

    none_sig = Signal(
        direction=SignalDirection.NONE,
        confidence=0.0,
        entry_price=100.0,
        stop_loss=99.0,
        take_profit=101.0,
        strategy_name="SupertrendTrend",
        regime="trending",
        reasoning="no flip",
    )
    strat._supertrend_trend = MagicMock()
    strat._supertrend_trend.generate_signal.return_value = none_sig
    strat._supertrend_trend.generate_continuation_signal.return_value = none_sig
    strat._supertrend_trend.generate_fast_signal.return_value = none_sig
    strat._supertrend_trend.generate_aligned_signal.return_value = none_sig
    # Mark as SupertrendTrend instance so isinstance() in router passes
    from src.strategies.supertrend_trend import SupertrendTrend
    strat._supertrend_trend.__class__ = SupertrendTrend

    df_4h = pd.DataFrame({"close": [100.0] * 50})
    df_1h = pd.DataFrame({"close": [100.0] * 50})
    df_15m = pd.DataFrame({"close": [100.0] * 50})

    result = strat.get_signal_multi_tf(df_4h, df_1h, df_15m)

    assert result is None
    # 4H flip + 1H continuation MUST be tried
    assert strat._supertrend_trend.generate_signal.called
    assert strat._supertrend_trend.generate_continuation_signal.called
    # 15m fast + aligned MUST NOT be tried in reduced mode
    assert not strat._supertrend_trend.generate_fast_signal.called
    assert not strat._supertrend_trend.generate_aligned_signal.called


# ---------------------------------------------------------------------------
# Strategy-route gating
# ---------------------------------------------------------------------------


def test_route_flags_gate() -> None:
    assert rlm.is_strategy_route_allowed("supertrend_trend") is True
    assert rlm.is_strategy_route_allowed("adaptive_trend") is False
    assert rlm.is_strategy_route_allowed("breakout_trader") is False
    # unknown route: fail-open
    assert rlm.is_strategy_route_allowed("future_unknown") is True


def test_adaptive_trend_route_returns_none_in_reduced_mode() -> None:
    strat = AdaptiveStrategy()
    # RANGING with ADX < 18 would normally route to AdaptiveTrend
    result = strat.select_strategy(_regime(MarketRegime.RANGING, adx=15.0))
    assert result is None


def test_breakout_trader_route_returns_none_in_reduced_mode() -> None:
    strat = AdaptiveStrategy()
    # VOLATILE with ADX ≥ 15 would normally route to BreakoutTrader
    result = strat.select_strategy(_regime(MarketRegime.VOLATILE, adx=20.0))
    assert result is None


def test_supertrend_trend_route_still_allowed() -> None:
    strat = AdaptiveStrategy()
    # TRENDING with ADX ≥ 18 must still route to SupertrendTrend
    result = strat.select_strategy(_regime(MarketRegime.TRENDING, adx=25.0))
    assert result is strat._supertrend_trend


# ---------------------------------------------------------------------------
# Consensus adjustment neutralized
# ---------------------------------------------------------------------------


def test_is_consensus_adjust_allowed_returns_false() -> None:
    assert rlm.is_consensus_adjust_allowed() is False


def test_consensus_compute_not_called_when_disabled() -> None:
    """Orchestrator must not invoke cross_asset_consensus.compute() in
    reduced mode. We simulate the exact branch from main.py."""

    from src.orchestrator.main import is_consensus_adjust_allowed as gate

    mock_consensus = MagicMock()
    mock_consensus.compute.return_value = {"SOL/USDT:USDT": 10.0}

    # This mirrors the code in main.py Step 3:
    if gate():
        consensus_adj = mock_consensus.compute({})
    else:
        consensus_adj = {}

    assert consensus_adj == {}
    assert not mock_consensus.compute.called


# ---------------------------------------------------------------------------
# Dynamic +1 position override disabled
# ---------------------------------------------------------------------------


def test_is_dynamic_pos_override_allowed_returns_false() -> None:
    assert rlm.is_dynamic_pos_override_allowed() is False


def test_effective_max_positions_does_not_exceed_cb_cap_in_reduced_mode() -> None:
    """Even at GREEN + conf=100 + large balance, reduced mode caps at 3."""

    from src.orchestrator.main import Orchestrator

    # Build a minimal Orchestrator without hitting __init__ side effects
    orch = Orchestrator.__new__(Orchestrator)

    green = CircuitBreakerConstraints(
        level=CircuitBreakerLevel.GREEN,
        max_leverage=10,
        max_positions=3,
        size_multiplier=Decimal("1.0"),
        trading_allowed=True,
        reason="GREEN",
    )

    # Would normally grant +1 (conf ≥ 60, balance ≥ $60)
    effective = orch._get_effective_max_positions(
        constraints=green,
        signal_confidence=95.0,
        balance=Decimal("10000"),
    )
    assert effective == 3, (
        "Reduced mode must NOT grant a 4th slot above the CB cap"
    )


def test_effective_max_positions_still_respects_cb_yellow_red() -> None:
    """Reduced mode does not loosen YELLOW/RED either."""

    from src.orchestrator.main import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)

    for level, cap in (
        (CircuitBreakerLevel.YELLOW, 2),
        (CircuitBreakerLevel.RED, 1),
    ):
        cons = CircuitBreakerConstraints(
            level=level,
            max_leverage=5,
            max_positions=cap,
            size_multiplier=Decimal("0.5"),
            trading_allowed=True,
            reason=level.value,
        )
        effective = orch._get_effective_max_positions(
            constraints=cons,
            signal_confidence=95.0,
            balance=Decimal("10000"),
        )
        assert effective == cap
