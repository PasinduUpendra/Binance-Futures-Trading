"""Tests for orchestrator fixes: TP validation, reconciliation SL/TP
discrimination, emergency TP, smart position swap, ATR-based emergency SL,
tp_pending retry, cached API calls, and float precision.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import pandas as pd

from src.orchestrator import main as orchestrator_main
from src.orchestrator.main import Orchestrator, TrailingStopState
from src.execution.order_manager import OrderState


@pytest.fixture
def isolated_orchestrator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(orchestrator_main, "AGENT_STATE_DIR", tmp_path)
    monkeypatch.setattr(Orchestrator, "_DAILY_STATE_FILE", tmp_path / "daily_state.json")
    monkeypatch.setattr(Orchestrator, "_TRAILING_STATE_FILE", tmp_path / "trailing_stops.json")
    orch = Orchestrator()
    # Use isolated DB to avoid loading production data
    from src.data.database import DatabaseManager
    orch.db = DatabaseManager(db_path=tmp_path / "test.db")
    return orch


# ────────────────────────────────────────────────────────────────────
# Fix 1: TrailingStopState model_validator
# ────────────────────────────────────────────────────────────────────


def test_ts_state_rejects_long_tp_below_entry() -> None:
    with pytest.raises(ValueError, match="LONG TP .* must be above entry"):
        TrailingStopState(
            symbol="ETH/USDT:USDT",
            direction="long",
            entry_price=2000.0,
            best_price=2050.0,
            atr_4h=30.0,
            take_profit=1900.0,
        )


def test_ts_state_rejects_short_tp_above_entry() -> None:
    with pytest.raises(ValueError, match="SHORT TP .* must be below entry"):
        TrailingStopState(
            symbol="ADA/USDT:USDT",
            direction="short",
            entry_price=0.26,
            best_price=0.24,
            atr_4h=0.005,
            take_profit=0.61,
        )


def test_ts_state_accepts_long_tp_above_entry() -> None:
    state = TrailingStopState(
        symbol="ETH/USDT:USDT",
        direction="long",
        entry_price=2000.0,
        best_price=2050.0,
        atr_4h=30.0,
        take_profit=2180.0,
    )
    assert state.take_profit == 2180.0


def test_ts_state_accepts_short_tp_below_entry() -> None:
    state = TrailingStopState(
        symbol="ADA/USDT:USDT",
        direction="short",
        entry_price=0.72,
        best_price=0.66,
        atr_4h=0.02,
        take_profit=0.61,
    )
    assert state.take_profit == 0.61


def test_ts_state_accepts_zero_tp() -> None:
    state = TrailingStopState(
        symbol="SOL/USDT:USDT",
        direction="long",
        entry_price=100.0,
        best_price=100.0,
        atr_4h=5.0,
        take_profit=0.0,
    )
    assert state.take_profit == 0.0


def test_ts_state_tp_pending_default_false() -> None:
    state = TrailingStopState(
        symbol="DOGE/USDT:USDT",
        direction="long",
        entry_price=0.1,
        best_price=0.1,
        atr_4h=0.005,
    )
    assert state.tp_pending is False


# ────────────────────────────────────────────────────────────────────
# Fix 1C: Defensive loading skips corrupted entries
# ────────────────────────────────────────────────────────────────────


def test_load_trailing_stops_skips_corrupted_entry(
    isolated_orchestrator: Orchestrator,
    tmp_path: Path,
) -> None:
    """One corrupted entry should not block loading valid ones."""
    data = {
        "GOOD/USDT:USDT": {
            "symbol": "GOOD/USDT:USDT",
            "direction": "long",
            "entry_price": 100.0,
            "best_price": 110.0,
            "atr_4h": 5.0,
            "take_profit": 130.0,
            "tp_pending": False,
        },
        "BAD/USDT:USDT": {
            "symbol": "BAD/USDT:USDT",
            "direction": "long",
            "entry_price": 100.0,
            "best_price": 110.0,
            "atr_4h": 5.0,
            "take_profit": 50.0,  # Invalid: LONG TP below entry
            "tp_pending": False,
        },
    }
    (tmp_path / "trailing_stops.json").write_text(json.dumps(data))
    isolated_orchestrator._load_trailing_stop_state()

    assert "GOOD/USDT:USDT" in isolated_orchestrator._trailing_stops
    assert "BAD/USDT:USDT" not in isolated_orchestrator._trailing_stops


def test_load_trailing_stops_persists_tp_pending(
    isolated_orchestrator: Orchestrator,
) -> None:
    """tp_pending field round-trips through persist/load."""
    isolated_orchestrator._trailing_stops = {
        "XRP/USDT:USDT": TrailingStopState(
            symbol="XRP/USDT:USDT",
            direction="long",
            entry_price=0.5,
            best_price=0.5,
            atr_4h=0.01,
            tp_pending=True,
        ),
    }
    isolated_orchestrator._persist_trailing_stop_state()
    isolated_orchestrator._trailing_stops = {}
    isolated_orchestrator._load_trailing_stop_state()

    restored = isolated_orchestrator._trailing_stops["XRP/USDT:USDT"]
    assert restored.tp_pending is True


# ────────────────────────────────────────────────────────────────────
# Fix 2B: Reconciliation SL/TP discrimination
# ────────────────────────────────────────────────────────────────────


def _make_order(order_type: str, stop_price: float | None = None) -> SimpleNamespace:
    return SimpleNamespace(order_type=order_type, stop_price=stop_price)


@pytest.mark.asyncio
async def test_reconcile_places_emergency_sl_when_missing(
    isolated_orchestrator: Orchestrator,
) -> None:
    """If position has TP but no SL, place emergency SL."""
    pos = SimpleNamespace(
        symbol="ETH/USDT:USDT", side="long",
        entry_price=Decimal("2000"), current_price=Decimal("2050"),
        size=Decimal("0.01"), unrealized_pnl=Decimal("0.5"),
    )
    isolated_orchestrator.position_tracker.get_open_positions = AsyncMock(
        return_value=[pos]
    )
    isolated_orchestrator._trailing_stops["ETH/USDT:USDT"] = TrailingStopState(
        symbol="ETH/USDT:USDT", direction="long",
        entry_price=2000.0, best_price=2050.0, atr_4h=30.0,
        take_profit=2180.0,
    )
    # Only a TP order (stop_price above entry=2000 → detected as TP for long), no SL
    isolated_orchestrator.order_manager.get_open_orders = AsyncMock(
        return_value=[_make_order("take_profit_market", stop_price=2180.0)]
    )
    isolated_orchestrator.order_manager.place_stop_loss = AsyncMock(
        return_value=SimpleNamespace(order_id="sl1")
    )
    isolated_orchestrator.order_manager.place_take_profit = AsyncMock(
        return_value=SimpleNamespace(order_id="tp1")
    )
    isolated_orchestrator.trade_journal.get_recent_trades = MagicMock(return_value=[])

    await isolated_orchestrator._reconcile_positions_and_orders()

    isolated_orchestrator.order_manager.place_stop_loss.assert_called_once()
    isolated_orchestrator.order_manager.place_take_profit.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_places_emergency_tp_when_missing(
    isolated_orchestrator: Orchestrator,
) -> None:
    """If position has SL but no TP, place emergency TP."""
    pos = SimpleNamespace(
        symbol="SOL/USDT:USDT", side="short",
        entry_price=Decimal("150"), current_price=Decimal("145"),
        size=Decimal("0.1"), unrealized_pnl=Decimal("0.5"),
    )
    isolated_orchestrator.position_tracker.get_open_positions = AsyncMock(
        return_value=[pos]
    )
    isolated_orchestrator._trailing_stops["SOL/USDT:USDT"] = TrailingStopState(
        symbol="SOL/USDT:USDT", direction="short",
        entry_price=150.0, best_price=145.0, atr_4h=5.0,
        take_profit=120.0,
    )
    # Only an SL order (stop_price above entry=150 → detected as SL for short), no TP
    isolated_orchestrator.order_manager.get_open_orders = AsyncMock(
        return_value=[_make_order("stop_market", stop_price=165.0)]
    )
    isolated_orchestrator.order_manager.place_stop_loss = AsyncMock()
    isolated_orchestrator.order_manager.place_take_profit = AsyncMock(
        return_value=SimpleNamespace(order_id="tp1")
    )
    isolated_orchestrator.trade_journal.get_recent_trades = MagicMock(return_value=[])

    await isolated_orchestrator._reconcile_positions_and_orders()

    isolated_orchestrator.order_manager.place_stop_loss.assert_not_called()
    isolated_orchestrator.order_manager.place_take_profit.assert_called_once()


@pytest.mark.asyncio
async def test_reconcile_no_action_when_both_orders_present(
    isolated_orchestrator: Orchestrator,
) -> None:
    """If position has both SL and TP, no emergency placement needed."""
    pos = SimpleNamespace(
        symbol="BTC/USDT:USDT", side="long",
        entry_price=Decimal("95000"), current_price=Decimal("96000"),
        size=Decimal("0.001"), unrealized_pnl=Decimal("1.0"),
    )
    isolated_orchestrator.position_tracker.get_open_positions = AsyncMock(
        return_value=[pos]
    )
    isolated_orchestrator._trailing_stops["BTC/USDT:USDT"] = TrailingStopState(
        symbol="BTC/USDT:USDT", direction="long",
        entry_price=95000.0, best_price=96000.0, atr_4h=500.0,
        take_profit=98000.0,
    )
    # SL below entry=95000, TP above entry=95000 → both detected for long
    isolated_orchestrator.order_manager.get_open_orders = AsyncMock(
        return_value=[
            _make_order("stop_market", stop_price=93500.0),
            _make_order("take_profit_market", stop_price=98000.0),
        ]
    )
    isolated_orchestrator.order_manager.place_stop_loss = AsyncMock()
    isolated_orchestrator.order_manager.place_take_profit = AsyncMock()
    isolated_orchestrator.trade_journal.get_recent_trades = MagicMock(return_value=[])

    await isolated_orchestrator._reconcile_positions_and_orders()

    isolated_orchestrator.order_manager.place_stop_loss.assert_not_called()
    isolated_orchestrator.order_manager.place_take_profit.assert_not_called()


# ────────────────────────────────────────────────────────────────────
# Fix 2A: Emergency TP
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_emergency_tp_uses_stored_tp(
    isolated_orchestrator: Orchestrator,
) -> None:
    pos = SimpleNamespace(
        symbol="ETH/USDT:USDT", side="long",
        entry_price=Decimal("2000"), size=Decimal("0.01"),
    )
    isolated_orchestrator._trailing_stops["ETH/USDT:USDT"] = TrailingStopState(
        symbol="ETH/USDT:USDT", direction="long",
        entry_price=2000.0, best_price=2050.0, atr_4h=30.0,
        take_profit=2180.0,
    )
    isolated_orchestrator.order_manager.place_take_profit = AsyncMock(
        return_value=SimpleNamespace(order_id="tp1")
    )

    await isolated_orchestrator._place_emergency_take_profit(pos)

    call_args = isolated_orchestrator.order_manager.place_take_profit.call_args
    assert float(call_args.kwargs["stop_price"]) == pytest.approx(2180.0)


@pytest.mark.asyncio
async def test_emergency_tp_computes_from_atr(
    isolated_orchestrator: Orchestrator,
) -> None:
    pos = SimpleNamespace(
        symbol="SOL/USDT:USDT", side="long",
        entry_price=Decimal("100"), size=Decimal("1.0"),
    )
    isolated_orchestrator._trailing_stops["SOL/USDT:USDT"] = TrailingStopState(
        symbol="SOL/USDT:USDT", direction="long",
        entry_price=100.0, best_price=105.0, atr_4h=5.0,
        take_profit=0.0,  # No stored TP
    )
    isolated_orchestrator.order_manager.place_take_profit = AsyncMock(
        return_value=SimpleNamespace(order_id="tp1")
    )

    await isolated_orchestrator._place_emergency_take_profit(pos)

    call_args = isolated_orchestrator.order_manager.place_take_profit.call_args
    # 100 + 6*5 = 130
    assert float(call_args.kwargs["stop_price"]) == pytest.approx(130.0)


@pytest.mark.asyncio
async def test_emergency_tp_sets_pending_when_no_atr(
    isolated_orchestrator: Orchestrator,
) -> None:
    pos = SimpleNamespace(
        symbol="DOGE/USDT:USDT", side="long",
        entry_price=Decimal("0.1"), size=Decimal("100"),
    )
    isolated_orchestrator._trailing_stops["DOGE/USDT:USDT"] = TrailingStopState(
        symbol="DOGE/USDT:USDT", direction="long",
        entry_price=0.1, best_price=0.1, atr_4h=0.0,
        take_profit=0.0,
    )
    isolated_orchestrator.order_manager.place_take_profit = AsyncMock()

    await isolated_orchestrator._place_emergency_take_profit(pos)

    isolated_orchestrator.order_manager.place_take_profit.assert_not_called()
    assert isolated_orchestrator._trailing_stops["DOGE/USDT:USDT"].tp_pending is True


# ────────────────────────────────────────────────────────────────────
# Fix 5: Emergency SL uses ATR
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_emergency_sl_uses_atr_when_available(
    isolated_orchestrator: Orchestrator,
) -> None:
    pos = SimpleNamespace(
        symbol="ETH/USDT:USDT", side="long",
        entry_price=Decimal("2000"), size=Decimal("0.01"),
    )
    isolated_orchestrator._trailing_stops["ETH/USDT:USDT"] = TrailingStopState(
        symbol="ETH/USDT:USDT", direction="long",
        entry_price=2000.0, best_price=2050.0, atr_4h=30.0,
    )
    isolated_orchestrator.order_manager.place_stop_loss = AsyncMock(
        return_value=SimpleNamespace(order_id="sl1")
    )

    await isolated_orchestrator._place_emergency_stop_loss(pos)

    call_args = isolated_orchestrator.order_manager.place_stop_loss.call_args
    # 2000 - 3*30 = 1910
    assert float(call_args.kwargs["stop_price"]) == pytest.approx(1910.0)


@pytest.mark.asyncio
async def test_emergency_sl_falls_back_to_breakeven(
    isolated_orchestrator: Orchestrator,
) -> None:
    pos = SimpleNamespace(
        symbol="LINK/USDT:USDT", side="long",
        entry_price=Decimal("10"), size=Decimal("1.0"),
    )
    isolated_orchestrator._trailing_stops["LINK/USDT:USDT"] = TrailingStopState(
        symbol="LINK/USDT:USDT", direction="long",
        entry_price=10.0, best_price=10.5, atr_4h=0.0,  # No ATR
    )
    isolated_orchestrator.order_manager.place_stop_loss = AsyncMock(
        return_value=SimpleNamespace(order_id="sl1")
    )

    await isolated_orchestrator._place_emergency_stop_loss(pos)

    call_args = isolated_orchestrator.order_manager.place_stop_loss.call_args
    # Breakeven at entry price
    assert float(call_args.kwargs["stop_price"]) == pytest.approx(10.0)


# ────────────────────────────────────────────────────────────────────
# Fix 4: Smart Position Swap
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_swap_requires_minimum_confidence(
    isolated_orchestrator: Orchestrator,
) -> None:
    """Signals below the absolute minimum gate (40) should not trigger swap."""
    positions = [
        SimpleNamespace(
            symbol="ADA/USDT:USDT", side="long",
            unrealized_pnl=Decimal("-5"), current_price=Decimal("0.25"),
            entry_price=Decimal("0.30"), size=Decimal("100"),
        ),
    ]
    isolated_orchestrator._trailing_stops["ADA/USDT:USDT"] = TrailingStopState(
        symbol="ADA/USDT:USDT", direction="long",
        entry_price=0.30, best_price=0.30, atr_4h=0.01,
    )
    result = await isolated_orchestrator._find_swap_candidate(
        new_confidence=35.0, open_positions=positions,
    )
    assert result is None


@pytest.mark.asyncio
async def test_swap_rejects_profitable_positions(
    isolated_orchestrator: Orchestrator,
) -> None:
    """Positions with positive PnL should not be swapped."""
    positions = [
        SimpleNamespace(
            symbol="ETH/USDT:USDT", side="long",
            unrealized_pnl=Decimal("10"), current_price=Decimal("2100"),
            entry_price=Decimal("2000"), size=Decimal("0.01"),
        ),
    ]
    isolated_orchestrator._trailing_stops["ETH/USDT:USDT"] = TrailingStopState(
        symbol="ETH/USDT:USDT", direction="long",
        entry_price=2000.0, best_price=2100.0, atr_4h=30.0,
    )
    isolated_orchestrator.trade_journal.get_recent_trades = MagicMock(return_value=[])
    result = await isolated_orchestrator._find_swap_candidate(
        new_confidence=90.0, open_positions=positions,
    )
    assert result is None


@pytest.mark.asyncio
async def test_swap_rejects_activated_trailing(
    isolated_orchestrator: Orchestrator,
) -> None:
    """Positions with activated trailing stops should not be swapped."""
    positions = [
        SimpleNamespace(
            symbol="SOL/USDT:USDT", side="long",
            unrealized_pnl=Decimal("-2"), current_price=Decimal("98"),
            entry_price=Decimal("100"), size=Decimal("1.0"),
        ),
    ]
    isolated_orchestrator._trailing_stops["SOL/USDT:USDT"] = TrailingStopState(
        symbol="SOL/USDT:USDT", direction="long",
        entry_price=100.0, best_price=110.0, atr_4h=5.0,
        activated=True,
    )
    isolated_orchestrator.trade_journal.get_recent_trades = MagicMock(return_value=[])
    result = await isolated_orchestrator._find_swap_candidate(
        new_confidence=90.0, open_positions=positions,
    )
    assert result is None


@pytest.mark.asyncio
async def test_swap_requires_confidence_delta_15(
    isolated_orchestrator: Orchestrator,
) -> None:
    """New signal needs >= 15 point confidence advantage to swap."""
    positions = [
        SimpleNamespace(
            symbol="ADA/USDT:USDT", side="long",
            unrealized_pnl=Decimal("-3"), current_price=Decimal("0.28"),
            entry_price=Decimal("0.30"), size=Decimal("100"),
        ),
    ]
    isolated_orchestrator._trailing_stops["ADA/USDT:USDT"] = TrailingStopState(
        symbol="ADA/USDT:USDT", direction="long",
        entry_price=0.30, best_price=0.30, atr_4h=0.01,
    )
    # Entry confidence = 60, new = 70 → delta = 10 < 15
    mock_trade = SimpleNamespace(
        symbol="ADA/USDT:USDT", exit_price=None, confidence=Decimal("60"),
    )
    isolated_orchestrator.trade_journal.get_recent_trades = MagicMock(
        return_value=[mock_trade]
    )
    result = await isolated_orchestrator._find_swap_candidate(
        new_confidence=70.0, open_positions=positions,
    )
    assert result is None

    # Now with delta >= 15 (new=80 - old=60 = 20)
    result = await isolated_orchestrator._find_swap_candidate(
        new_confidence=80.0, open_positions=positions,
    )
    assert result is not None
    assert result.symbol == "ADA/USDT:USDT"


@pytest.mark.asyncio
async def test_close_position_for_swap(
    isolated_orchestrator: Orchestrator,
) -> None:
    pos = SimpleNamespace(
        symbol="ADA/USDT:USDT", side="long",
        unrealized_pnl=Decimal("-3"), current_price=Decimal("0.28"),
        entry_price=Decimal("0.30"), size=Decimal("100"),
        leverage=5,
    )
    isolated_orchestrator._trailing_stops["ADA/USDT:USDT"] = TrailingStopState(
        symbol="ADA/USDT:USDT", direction="long",
        entry_price=0.30, best_price=0.30, atr_4h=0.01,
    )
    isolated_orchestrator.order_manager.cancel_open_orders = AsyncMock(return_value=(2, True))
    isolated_orchestrator.order_manager.place_market_order = AsyncMock(
        return_value=SimpleNamespace(order_id="close1", filled=Decimal("100"))
    )

    success = await isolated_orchestrator._close_position_for_swap(
        pos, "XRP/USDT:USDT", "4h_close",
    )

    assert success is True
    assert "ADA/USDT:USDT" not in isolated_orchestrator._trailing_stops
    isolated_orchestrator.order_manager.cancel_open_orders.assert_called_once()
    isolated_orchestrator.order_manager.place_market_order.assert_called_once()


# ────────────────────────────────────────────────────────────────────
# Fix 6: TP pending retry in reconciliation
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_retries_tp_pending(
    isolated_orchestrator: Orchestrator,
) -> None:
    """Position with tp_pending=True and SL present should trigger TP placement."""
    pos = SimpleNamespace(
        symbol="LINK/USDT:USDT", side="long",
        entry_price=Decimal("10"), current_price=Decimal("10.5"),
        size=Decimal("1.0"), unrealized_pnl=Decimal("0.5"),
    )
    isolated_orchestrator.position_tracker.get_open_positions = AsyncMock(
        return_value=[pos]
    )
    isolated_orchestrator._trailing_stops["LINK/USDT:USDT"] = TrailingStopState(
        symbol="LINK/USDT:USDT", direction="long",
        entry_price=10.0, best_price=10.5, atr_4h=0.2,
        take_profit=11.2, tp_pending=True,
    )
    # Has SL only (stop_price below entry=10 → detected as SL for long)
    isolated_orchestrator.order_manager.get_open_orders = AsyncMock(
        return_value=[_make_order("stop_market", stop_price=9.4)]
    )
    isolated_orchestrator.order_manager.place_stop_loss = AsyncMock()
    isolated_orchestrator.order_manager.place_take_profit = AsyncMock(
        return_value=SimpleNamespace(order_id="tp1")
    )
    isolated_orchestrator.trade_journal.get_recent_trades = MagicMock(return_value=[])

    await isolated_orchestrator._reconcile_positions_and_orders()

    # SL should NOT be placed (already present)
    isolated_orchestrator.order_manager.place_stop_loss.assert_not_called()
    # TP should be placed (was pending)
    isolated_orchestrator.order_manager.place_take_profit.assert_called_once()
    # tp_pending should now be cleared
    assert isolated_orchestrator._trailing_stops["LINK/USDT:USDT"].tp_pending is False


# ────────────────────────────────────────────────────────────────────
# Fix 8: Float precision — SL uses Decimal
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_emergency_sl_uses_decimal_stop_price(
    isolated_orchestrator: Orchestrator,
) -> None:
    """Emergency SL stop_price should be a Decimal, not float."""
    pos = SimpleNamespace(
        symbol="ETH/USDT:USDT", side="long",
        entry_price=Decimal("1970.63"), size=Decimal("0.01"),
    )
    isolated_orchestrator._trailing_stops["ETH/USDT:USDT"] = TrailingStopState(
        symbol="ETH/USDT:USDT", direction="long",
        entry_price=1970.63, best_price=1980.0, atr_4h=29.15,
    )
    isolated_orchestrator.order_manager.place_stop_loss = AsyncMock(
        return_value=SimpleNamespace(order_id="sl1")
    )

    await isolated_orchestrator._place_emergency_stop_loss(pos)

    call_args = isolated_orchestrator.order_manager.place_stop_loss.call_args
    assert isinstance(call_args.kwargs["stop_price"], Decimal)


# ════════════════════════════════════════════════════════════════════════
# Live-readiness audit tests (2026-04-02)
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_execute_signal_returns_none_on_null_order_result(
    isolated_orchestrator: Orchestrator,
) -> None:
    """Fix 1 (C1): If place_market_order returns None, _execute_signal
    should abort gracefully instead of crashing on order_result.order_id."""
    orch = isolated_orchestrator
    orch.market_data.get_margin_balance = AsyncMock(return_value=Decimal("6000"))
    orch.position_tracker.get_open_positions = AsyncMock(return_value=[])
    orch.order_manager.set_leverage = AsyncMock()
    orch.order_manager.place_market_order = AsyncMock(return_value=None)

    signal = SimpleNamespace(
        direction=SimpleNamespace(value="long"),
        confidence=65,
        entry_price=100.0,
        stop_loss=97.0,
        take_profit=106.0,
        strategy_name="SupertrendTrend",
        regime="trending",
    )
    cb_state = SimpleNamespace(
        level="GREEN",
        constraints=SimpleNamespace(
            trading_allowed=True,
            max_positions=3,
            max_leverage=10,
            size_multiplier=Decimal("1.0"),
            reason="",
        ),
    )
    df = pd.DataFrame({"atr": [1.5], "close": [100.0]})

    result = await orch._execute_signal(
        signal=signal, symbol="ETH/USDT:USDT",
        df_4h=df, df_1h=df, cb_state=cb_state, trigger="test",
    )
    assert result is None


@pytest.mark.asyncio
async def test_execute_signal_returns_none_on_zero_fill(
    isolated_orchestrator: Orchestrator,
) -> None:
    """Fix 2 (C4): If order is CLOSED but filled == 0, abort."""
    orch = isolated_orchestrator
    orch.market_data.get_margin_balance = AsyncMock(return_value=Decimal("6000"))
    orch.position_tracker.get_open_positions = AsyncMock(return_value=[])
    orch.order_manager.set_leverage = AsyncMock()
    orch.order_manager.place_market_order = AsyncMock(
        return_value=SimpleNamespace(
            order_id="test123", filled=Decimal("0"),
            average_fill_price=Decimal("100"),
        )
    )
    orch.order_manager.get_order_status = AsyncMock(
        return_value=SimpleNamespace(status=OrderState.CLOSED)
    )

    signal = SimpleNamespace(
        direction=SimpleNamespace(value="long"),
        confidence=65,
        entry_price=100.0,
        stop_loss=97.0,
        take_profit=106.0,
        strategy_name="SupertrendTrend",
        regime="trending",
    )
    cb_state = SimpleNamespace(
        level="GREEN",
        constraints=SimpleNamespace(
            trading_allowed=True,
            max_positions=3,
            max_leverage=10,
            size_multiplier=Decimal("1.0"),
            reason="",
        ),
    )
    df = pd.DataFrame({"atr": [1.5], "close": [100.0]})

    result = await orch._execute_signal(
        signal=signal, symbol="ETH/USDT:USDT",
        df_4h=df, df_1h=df, cb_state=cb_state, trigger="test",
    )
    assert result is None


def test_daily_trade_counter_initialized(
    isolated_orchestrator: Orchestrator,
) -> None:
    """Fix 4 (C3): Orchestrator must have daily trade counter attributes."""
    orch = isolated_orchestrator
    assert hasattr(orch, "_daily_trade_count")
    assert hasattr(orch, "_daily_trade_date")
    assert orch._daily_trade_count == 0


@pytest.mark.asyncio
async def test_daily_trade_limit_blocks_at_20(
    isolated_orchestrator: Orchestrator,
) -> None:
    """Fix 4 (C3): After 20 trades in a day, _execute_signal rejects."""
    orch = isolated_orchestrator
    orch._daily_trade_count = 20
    orch.market_data.get_margin_balance = AsyncMock(return_value=Decimal("6000"))
    orch.position_tracker.get_open_positions = AsyncMock(return_value=[])

    signal = SimpleNamespace(
        direction=SimpleNamespace(value="long"),
        confidence=65,
        entry_price=100.0,
        stop_loss=97.0,
        take_profit=106.0,
        strategy_name="SupertrendTrend",
        regime="trending",
    )
    cb_state = SimpleNamespace(
        level="GREEN",
        constraints=SimpleNamespace(
            trading_allowed=True,
            max_positions=3,
            max_leverage=10,
            size_multiplier=Decimal("1.0"),
            reason="",
        ),
    )
    df = pd.DataFrame({"atr": [1.5], "close": [100.0]})

    result = await orch._execute_signal(
        signal=signal, symbol="SOL/USDT:USDT",
        df_4h=df, df_1h=df, cb_state=cb_state, trigger="test",
    )
    assert result is None


# ────────────────────────────────────────────────────────────────────
# Fix 3 (C2): SL returns None → emergency close
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_signal_aborts_on_sl_none(
    isolated_orchestrator: Orchestrator,
) -> None:
    """Fix 3 (C2): If place_stop_loss returns None, emergency close the
    position and return None to prevent naked exposure."""
    orch = isolated_orchestrator
    # Set state so CB re-check inside _execute_signal passes
    orch.state.daily_start_balance = Decimal("6000")
    orch.drawdown_monitor._peak_balance = Decimal("6000")

    orch.market_data.get_margin_balance = AsyncMock(return_value=Decimal("6000"))
    orch.position_tracker.get_open_positions = AsyncMock(return_value=[])
    orch.order_manager.set_leverage = AsyncMock()
    orch.order_manager.place_market_order = AsyncMock(
        return_value=SimpleNamespace(
            order_id="fill1", filled=Decimal("0.5"),
            average_fill_price=Decimal("100"),
        )
    )
    orch.order_manager.get_order_status = AsyncMock(
        return_value=SimpleNamespace(status=OrderState.CLOSED)
    )
    # SL returns None (graceful failure)
    orch.order_manager.place_stop_loss = AsyncMock(return_value=None)

    signal = SimpleNamespace(
        direction=SimpleNamespace(value="long"),
        confidence=65,
        entry_price=100.0,
        stop_loss=97.0,
        take_profit=106.0,
        strategy_name="SupertrendTrend",
        regime="trending",
    )
    cb_state = SimpleNamespace(
        level="GREEN",
        constraints=SimpleNamespace(
            trading_allowed=True,
            max_positions=3,
            max_leverage=10,
            size_multiplier=Decimal("1.0"),
            reason="",
        ),
    )
    df = pd.DataFrame({"atr": [1.5], "close": [100.0]})

    result = await orch._execute_signal(
        signal=signal, symbol="ETH/USDT:USDT",
        df_4h=df, df_1h=df, cb_state=cb_state, trigger="test",
    )
    assert result is None

    # Emergency close should have been called (second call to place_market_order)
    assert orch.order_manager.place_market_order.await_count == 2
    emergency_call = orch.order_manager.place_market_order.await_args_list[1]
    assert emergency_call.kwargs["side"] == "sell"  # close long
    assert emergency_call.kwargs["amount"] == Decimal("0.5")


# ────────────────────────────────────────────────────────────────────
# Fix 5 (C5): Startup cleans all stale orders
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_startup_cancels_all_stale_orders(
    isolated_orchestrator: Orchestrator,
) -> None:
    """Fix 5 (C5): start() must cancel all open orders on every TRADING_PAIR
    before subscribing to WebSocket kline streams."""
    orch = isolated_orchestrator
    orch.market_data.connect = AsyncMock()
    orch.position_tracker.connect = AsyncMock()
    orch.order_manager.connect = AsyncMock()
    orch.market_data.get_margin_balance = AsyncMock(return_value=Decimal("6000"))
    orch.market_data.get_all_assets = AsyncMock(return_value=[])
    orch.market_data.fetch_commission_rate = AsyncMock(
        side_effect=Exception("no exchange in test"),
    )
    orch.position_tracker.get_open_positions = AsyncMock(return_value=[])
    orch.order_manager.cancel_open_orders = AsyncMock(return_value=(0, True))
    orch.order_manager.get_open_orders = AsyncMock(return_value=[])
    orch.market_data.subscribe_kline_close = AsyncMock()

    # Prevent the main loop from running
    orch._shutdown_event.set()

    await orch.start()

    # cancel_open_orders should have been called for each TRADING_PAIR
    from src.orchestrator.main import TRADING_PAIRS
    called_symbols = [
        call.args[0] for call in orch.order_manager.cancel_open_orders.await_args_list
    ]
    for pair in TRADING_PAIRS:
        assert pair in called_symbols, f"cancel_open_orders not called for {pair}"


@pytest.mark.asyncio
async def test_startup_preserves_orders_for_active_positions(
    isolated_orchestrator: Orchestrator,
) -> None:
    """Startup must NOT cancel SL/TP orders on symbols that have open positions."""
    orch = isolated_orchestrator
    orch.market_data.connect = AsyncMock()
    orch.position_tracker.connect = AsyncMock()
    orch.order_manager.connect = AsyncMock()
    orch.market_data.get_margin_balance = AsyncMock(return_value=Decimal("6000"))
    orch.market_data.get_all_assets = AsyncMock(return_value=[])
    orch.market_data.fetch_commission_rate = AsyncMock(
        side_effect=Exception("no exchange in test"),
    )

    # SOL has an open position — its orders must NOT be wiped
    sol_position = SimpleNamespace(
        symbol="SOL/USDT:USDT",
        side="long",
        entry_price=Decimal("84.61"),
        current_price=Decimal("84.80"),
        contracts=Decimal("0.35"),
        unrealized_pnl=Decimal("0.07"),
        leverage=5,
        margin=Decimal("5.92"),
    )
    orch.position_tracker.get_open_positions = AsyncMock(return_value=[sol_position])
    orch.order_manager.cancel_open_orders = AsyncMock(return_value=(0, True))

    # SOL has 2 conditional orders (SL+TP)
    async def mock_get_open_orders(symbol, **kwargs):
        if symbol == "SOL/USDT:USDT":
            return [SimpleNamespace(order_id="sl1"), SimpleNamespace(order_id="tp1")]
        return []
    orch.order_manager.get_open_orders = AsyncMock(side_effect=mock_get_open_orders)
    orch.market_data.subscribe_kline_close = AsyncMock()
    orch._shutdown_event.set()

    await orch.start()

    from src.orchestrator.main import TRADING_PAIRS
    called_symbols = [
        call.args[0] for call in orch.order_manager.cancel_open_orders.await_args_list
    ]
    # SOL should NOT have been cancelled (has open position)
    assert "SOL/USDT:USDT" not in called_symbols, (
        "cancel_open_orders was called for SOL — should be skipped (has open position)"
    )
    # All OTHER pairs should have been cancelled
    for pair in TRADING_PAIRS:
        if pair != "SOL/USDT:USDT":
            assert pair in called_symbols, f"cancel_open_orders not called for {pair}"


# ────────────────────────────────────────────────────────────────────
# Fix 5b (C6): Orphan orders cleaned during reconciliation
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_orphan_orders_cleaned_on_reconciliation(
    isolated_orchestrator: Orchestrator,
) -> None:
    """Fix 5b: If a symbol has open orders but no matching position,
    reconciliation must cancel those orphan orders."""
    orch = isolated_orchestrator
    orch.position_tracker.get_open_positions = AsyncMock(return_value=[])

    # DOGE has orphan orders (no position)
    orphan_order = SimpleNamespace(
        order_id="orphan1",
        order_type="STOP_MARKET",
        side="sell",
        amount=Decimal("100"),
        stop_price=Decimal("0.10"),
        price=None,
        status=OrderState.OPEN,
        is_conditional=True,
    )

    async def mock_get_open_orders(symbol, **kwargs):
        if symbol == "DOGE/USDT:USDT":
            return [orphan_order]
        return []

    orch.order_manager.get_open_orders = AsyncMock(side_effect=mock_get_open_orders)
    orch.order_manager.cancel_open_orders = AsyncMock(return_value=(1, True))

    await orch._reconcile_positions_and_orders()

    # cancel_open_orders should have been called for DOGE (orphan)
    cancel_symbols = [
        call.args[0] for call in orch.order_manager.cancel_open_orders.await_args_list
    ]
    assert "DOGE/USDT:USDT" in cancel_symbols


# ────────────────────────────────────────────────────────────────────
# v6.20: Wrong-side swap path
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_swap_wrong_side_at_confidence_40(
    isolated_orchestrator: Orchestrator,
) -> None:
    """Wrong-side swap should trigger at confidence >= 40 when direction opposes."""
    positions = [
        SimpleNamespace(
            symbol="DOGE/USDT:USDT", side="short",
            unrealized_pnl=Decimal("-8"), current_price=Decimal("0.18"),
            entry_price=Decimal("0.15"), size=Decimal("200"),
        ),
    ]
    isolated_orchestrator._trailing_stops["DOGE/USDT:USDT"] = TrailingStopState(
        symbol="DOGE/USDT:USDT", direction="short",
        entry_price=0.15, best_price=0.15, atr_4h=0.005,
    )
    isolated_orchestrator.trade_journal.get_recent_trades = MagicMock(
        return_value=[
            SimpleNamespace(
                symbol="DOGE/USDT:USDT", exit_price=None,
                confidence=Decimal("69"),
            ),
        ]
    )
    # Long signal at 45% confidence — wrong-side swap should work
    result = await isolated_orchestrator._find_swap_candidate(
        new_confidence=45.0, open_positions=positions,
        new_direction="long",
    )
    assert result is not None
    assert result.symbol == "DOGE/USDT:USDT"


@pytest.mark.asyncio
async def test_swap_wrong_side_requires_negative_pnl(
    isolated_orchestrator: Orchestrator,
) -> None:
    """Wrong-side swap should NOT trigger if position is profitable."""
    positions = [
        SimpleNamespace(
            symbol="ETH/USDT:USDT", side="short",
            unrealized_pnl=Decimal("5"), current_price=Decimal("1900"),
            entry_price=Decimal("2000"), size=Decimal("0.01"),
        ),
    ]
    isolated_orchestrator._trailing_stops["ETH/USDT:USDT"] = TrailingStopState(
        symbol="ETH/USDT:USDT", direction="short",
        entry_price=2000.0, best_price=1900.0, atr_4h=50.0,
    )
    isolated_orchestrator.trade_journal.get_recent_trades = MagicMock(return_value=[])
    result = await isolated_orchestrator._find_swap_candidate(
        new_confidence=60.0, open_positions=positions,
        new_direction="long",
    )
    assert result is None


@pytest.mark.asyncio
async def test_swap_same_direction_needs_delta_15(
    isolated_orchestrator: Orchestrator,
) -> None:
    """Same-direction swap requires confidence >= 50 AND delta >= 15."""
    positions = [
        SimpleNamespace(
            symbol="ADA/USDT:USDT", side="long",
            unrealized_pnl=Decimal("-3"), current_price=Decimal("0.28"),
            entry_price=Decimal("0.30"), size=Decimal("100"),
        ),
    ]
    isolated_orchestrator._trailing_stops["ADA/USDT:USDT"] = TrailingStopState(
        symbol="ADA/USDT:USDT", direction="long",
        entry_price=0.30, best_price=0.30, atr_4h=0.01,
    )
    mock_trade = SimpleNamespace(
        symbol="ADA/USDT:USDT", exit_price=None, confidence=Decimal("50"),
    )
    isolated_orchestrator.trade_journal.get_recent_trades = MagicMock(
        return_value=[mock_trade]
    )
    # Same direction, confidence 60, delta = 60-50 = 10 < 15 → reject
    result = await isolated_orchestrator._find_swap_candidate(
        new_confidence=60.0, open_positions=positions,
        new_direction="long",
    )
    assert result is None

    # Same direction, confidence 70, delta = 70-50 = 20 >= 15 → swap OK
    result = await isolated_orchestrator._find_swap_candidate(
        new_confidence=70.0, open_positions=positions,
        new_direction="long",
    )
    assert result is not None


# ────────────────────────────────────────────────────────────────────
# v6.20: Dynamic position limit
# ────────────────────────────────────────────────────────────────────


def test_dynamic_pos_limit_green_high_confidence(
    isolated_orchestrator: Orchestrator,
) -> None:
    """GREEN + high confidence + sufficient balance → +1 position."""
    from src.risk.circuit_breaker import CircuitBreakerConstraints, CircuitBreakerLevel

    constraints = CircuitBreakerConstraints(
        level=CircuitBreakerLevel.GREEN,
        max_leverage=10, max_positions=3,
        size_multiplier=Decimal("1.0"), trading_allowed=True,
    )
    eff = isolated_orchestrator._get_effective_max_positions(
        constraints, signal_confidence=65.0, balance=Decimal("100"),
    )
    assert eff == 4


def test_dynamic_pos_limit_low_confidence_stays_base(
    isolated_orchestrator: Orchestrator,
) -> None:
    """Confidence below 60 should not trigger dynamic increase."""
    from src.risk.circuit_breaker import CircuitBreakerConstraints, CircuitBreakerLevel

    constraints = CircuitBreakerConstraints(
        level=CircuitBreakerLevel.GREEN,
        max_leverage=10, max_positions=3,
        size_multiplier=Decimal("1.0"), trading_allowed=True,
    )
    eff = isolated_orchestrator._get_effective_max_positions(
        constraints, signal_confidence=55.0, balance=Decimal("200"),
    )
    assert eff == 3


def test_dynamic_pos_limit_yellow_not_increased(
    isolated_orchestrator: Orchestrator,
) -> None:
    """Non-GREEN levels must never get dynamic increase."""
    from src.risk.circuit_breaker import CircuitBreakerConstraints, CircuitBreakerLevel

    constraints = CircuitBreakerConstraints(
        level=CircuitBreakerLevel.YELLOW,
        max_leverage=5, max_positions=2,
        size_multiplier=Decimal("0.5"), trading_allowed=True,
    )
    eff = isolated_orchestrator._get_effective_max_positions(
        constraints, signal_confidence=80.0, balance=Decimal("200"),
    )
    assert eff == 2


def test_dynamic_pos_limit_insufficient_balance(
    isolated_orchestrator: Orchestrator,
) -> None:
    """Balance too low should prevent dynamic increase."""
    from src.risk.circuit_breaker import CircuitBreakerConstraints, CircuitBreakerLevel

    constraints = CircuitBreakerConstraints(
        level=CircuitBreakerLevel.GREEN,
        max_leverage=10, max_positions=3,
        size_multiplier=Decimal("1.0"), trading_allowed=True,
    )
    # Need $15 per slot × 4 = $60. Balance=$50 → stays at 3
    eff = isolated_orchestrator._get_effective_max_positions(
        constraints, signal_confidence=70.0, balance=Decimal("50"),
    )
    assert eff == 3


# ────────────────────────────────────────────────────────────────────
# v6.20: Reversal exit deduplication
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reversal_exit_dedup_skips_when_sl_at_breakeven(
    isolated_orchestrator: Orchestrator,
) -> None:
    """If SL is already at breakeven (within 0.1%), skip redundant cancel+replace."""
    orch = isolated_orchestrator
    pos = SimpleNamespace(
        symbol="SUI/USDT:USDT", side="long",
        entry_price=Decimal("3.50"), current_price=Decimal("3.40"),
        size=Decimal("10"), unrealized_pnl=Decimal("-1"),
    )
    orch.position_tracker.get_open_positions = AsyncMock(return_value=[pos])
    orch._trailing_stops["SUI/USDT:USDT"] = TrailingStopState(
        symbol="SUI/USDT:USDT", direction="long",
        entry_price=3.50, best_price=3.60, atr_4h=0.1,
        take_profit=4.0,
    )
    orch.adaptive_strategy.check_supertrend_reversal = MagicMock(return_value=True)

    # SL already at entry price (breakeven) — within 0.1%
    existing_sl = SimpleNamespace(
        order_type="stop_market",
        stop_price=Decimal("3.50"),  # exactly at entry = breakeven
    )
    orch.order_manager.get_open_orders = AsyncMock(return_value=[existing_sl])
    orch.order_manager.cancel_open_orders = AsyncMock()
    orch.order_manager.place_stop_loss = AsyncMock()

    # Build pair data with at least a 4H dataframe
    pair_data_4h = {"SUI/USDT:USDT": pd.DataFrame({"close": [3.5]})}
    result = SimpleNamespace(positions_closed=[], errors=[])

    await orch._check_supertrend_reversal_exits(
        pair_data_4h, result, Decimal("100"),
    )

    # Should NOT cancel/replace since SL is already at breakeven
    orch.order_manager.cancel_open_orders.assert_not_called()
    orch.order_manager.place_stop_loss.assert_not_called()


@pytest.mark.asyncio
async def test_reversal_exit_dedup_handles_real_order_status_objects(
    isolated_orchestrator: Orchestrator,
) -> None:
    """Regression test for F9: OrderStatus objects must not crash .get()."""
    from src.execution.order_manager import OrderStatus, OrderState as OState

    orch = isolated_orchestrator
    pos = SimpleNamespace(
        symbol="SUI/USDT:USDT", side="long",
        entry_price=Decimal("3.50"), current_price=Decimal("3.40"),
        size=Decimal("10"), unrealized_pnl=Decimal("-1"),
    )
    orch.position_tracker.get_open_positions = AsyncMock(return_value=[pos])
    orch._trailing_stops["SUI/USDT:USDT"] = TrailingStopState(
        symbol="SUI/USDT:USDT", direction="long",
        entry_price=3.50, best_price=3.60, atr_4h=0.1,
        take_profit=4.0,
    )
    orch.adaptive_strategy.check_supertrend_reversal = MagicMock(return_value=True)

    # Use REAL OrderStatus objects (the crash was calling .get() on these)
    from datetime import datetime, timezone
    existing_sl = OrderStatus(
        order_id="123",
        symbol="SUI/USDT:USDT",
        side="sell",
        order_type="stop_market",
        amount=Decimal("10"),
        filled=Decimal("0"),
        remaining=Decimal("10"),
        stop_price=Decimal("3.50"),
        status=OState.OPEN,
        timestamp=datetime.now(tz=timezone.utc),
        is_conditional=True,
    )
    orch.order_manager.get_open_orders = AsyncMock(return_value=[existing_sl])
    orch.order_manager.cancel_open_orders = AsyncMock()
    orch.order_manager.place_stop_loss = AsyncMock()

    pair_data_4h = {"SUI/USDT:USDT": pd.DataFrame({"close": [3.5]})}
    result = SimpleNamespace(positions_closed=[], errors=[])

    # This must NOT raise 'OrderStatus' object has no attribute 'get'
    await orch._check_supertrend_reversal_exits(
        pair_data_4h, result, Decimal("100"),
    )

    # SL at breakeven → should skip
    orch.order_manager.cancel_open_orders.assert_not_called()


# ════════════════════════════════════════════════════════════════════════
# F1/F2: Audit gate integration tests
# ════════════════════════════════════════════════════════════════════════


def _make_execute_signal_fixtures(orch: Orchestrator):
    """Set up common mocks for _execute_signal tests."""
    orch.state.daily_start_balance = Decimal("6000")
    orch.drawdown_monitor._peak_balance = Decimal("6000")
    orch.market_data.get_margin_balance = AsyncMock(return_value=Decimal("6000"))
    orch.position_tracker.get_open_positions = AsyncMock(return_value=[])
    orch.order_manager.set_leverage = AsyncMock()
    orch.market_data.fetch_funding_rate = AsyncMock(return_value=Decimal("0.0001"))

    signal = SimpleNamespace(
        direction=SimpleNamespace(value="long"),
        confidence=65,
        entry_price=100.0,
        stop_loss=97.0,
        take_profit=106.0,
        strategy_name="SupertrendTrend",
        regime="trending",
        indicators_used={"adx": 25.0, "atr": 1.5},
    )
    cb_state = SimpleNamespace(
        level="GREEN",
        constraints=SimpleNamespace(
            trading_allowed=True,
            max_positions=3,
            max_leverage=10,
            size_multiplier=Decimal("1.0"),
            reason="",
        ),
    )
    df = pd.DataFrame({"atr": [1.5], "close": [100.0], "adx": [25.0]})
    return signal, cb_state, df


@pytest.mark.asyncio
async def test_audit_reject_blocks_execution(
    isolated_orchestrator: Orchestrator,
) -> None:
    """F1/F2: If audit_decision returns REJECT, _execute_signal returns None
    and place_market_order is NEVER called."""
    orch = isolated_orchestrator
    signal, cb_state, df = _make_execute_signal_fixtures(orch)
    orch.order_manager.place_market_order = AsyncMock()

    # Validators pass — test the audit gate itself
    orch.price_validator.validate_price = AsyncMock(
        return_value=SimpleNamespace(valid=True, issues=[])
    )
    orch.signal_validator.validate_signal = MagicMock(
        return_value=SimpleNamespace(valid=True, issues=[])
    )

    # Force audit REJECT
    from src.anti_hallucination.decision_auditor import AuditReport

    orch.decision_auditor.audit_decision = MagicMock(
        return_value=AuditReport(
            decision="REJECT",
            decision_reasoning="Test: forced rejection",
            price_validated=True,
            signal_validated=True,
            sanity_checks_passed=True,
            risk_approved=True,
        )
    )

    result = await orch._execute_signal(
        signal=signal, symbol="ETH/USDT:USDT",
        df_4h=df, df_1h=df, cb_state=cb_state, trigger="test",
    )
    assert result is None
    orch.order_manager.place_market_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_validators_called_and_results_flow_into_audit(
    isolated_orchestrator: Orchestrator,
) -> None:
    """F1/F2: PriceValidator and SignalValidator are invoked before audit,
    their real results flow into audit_decision's market_data dict."""
    orch = isolated_orchestrator
    signal, cb_state, df = _make_execute_signal_fixtures(orch)

    # Price validator returns FAILED
    orch.price_validator.validate_price = AsyncMock(
        return_value=SimpleNamespace(valid=False, issues=["Price outside 24h range"])
    )
    orch.signal_validator.validate_signal = MagicMock(
        return_value=SimpleNamespace(valid=True, issues=[])
    )

    # Spy on audit_decision to capture the market_data it receives
    from src.anti_hallucination.decision_auditor import AuditReport

    orch.decision_auditor.audit_decision = MagicMock(
        return_value=AuditReport(
            decision="REJECT",
            decision_reasoning="Price not validated — possible hallucination",
        )
    )

    result = await orch._execute_signal(
        signal=signal, symbol="ETH/USDT:USDT",
        df_4h=df, df_1h=df, cb_state=cb_state, trigger="test",
    )
    assert result is None

    # Verify validators were actually called
    orch.price_validator.validate_price.assert_awaited_once()
    orch.signal_validator.validate_signal.assert_called_once()

    # Verify the REAL validator result (False) flowed into audit
    audit_kwargs = orch.decision_auditor.audit_decision.call_args.kwargs
    assert audit_kwargs["market_data"]["price_validated"] is False
    assert audit_kwargs["market_data"]["signal_validated"] is True


# ════════════════════════════════════════════════════════════════════════
# R-A3: Native trailing stop wired into post-entry path
# ════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_native_trailing_stop_placed_on_new_position(
    isolated_orchestrator: Orchestrator,
) -> None:
    """R-A3: After SL+TP, place_trailing_stop_market is called with
    callback_rate derived from ATR and activation_price from 2.0*ATR."""
    orch = isolated_orchestrator
    signal, cb_state, df = _make_execute_signal_fixtures(orch)

    # Validators + audit pass
    orch.price_validator.validate_price = AsyncMock(
        return_value=SimpleNamespace(valid=True, issues=[])
    )
    orch.signal_validator.validate_signal = MagicMock(
        return_value=SimpleNamespace(valid=True, issues=[])
    )
    from src.anti_hallucination.decision_auditor import AuditReport

    orch.decision_auditor.audit_decision = MagicMock(
        return_value=AuditReport(
            decision="EXECUTE",
            decision_reasoning="All checks passed",
            price_validated=True,
            signal_validated=True,
            sanity_checks_passed=True,
            risk_approved=True,
            risk_reward_ratio=Decimal("3.0"),
        )
    )
    # Mock orderbook slippage
    orch.market_data.fetch_orderbook = AsyncMock(
        return_value=SimpleNamespace(
            model_dump=lambda: {
                "bids": [{"price": 100.0, "amount": 10.0}],
                "asks": [{"price": 100.01, "amount": 10.0}],
            }
        )
    )
    orch.slippage_estimator.estimate_slippage = MagicMock(
        return_value=SimpleNamespace(
            slippage_pct=Decimal("0.01"),
            fully_fillable=True,
            levels_consumed=1,
            expected_fill_price=Decimal("100.0"),
        )
    )
    # Execution mocks
    orch.order_manager.place_market_order = AsyncMock(
        return_value=SimpleNamespace(
            order_id="fill1", filled=Decimal("0.5"),
            average_fill_price=Decimal("100"),
        )
    )
    orch.order_manager.get_order_status = AsyncMock(
        return_value=SimpleNamespace(status=OrderState.CLOSED)
    )
    orch.order_manager.place_stop_loss = AsyncMock(
        return_value=SimpleNamespace(order_id="sl1")
    )
    orch.order_manager.place_take_profit = AsyncMock(
        return_value=SimpleNamespace(order_id="tp1")
    )
    orch.order_manager.place_trailing_stop_market = AsyncMock(
        return_value=SimpleNamespace(order_id="trail1")
    )
    orch.alert_system.send_alert = AsyncMock()

    result = await orch._execute_signal(
        signal=signal, symbol="ETH/USDT:USDT",
        df_4h=df, df_1h=df, cb_state=cb_state, trigger="test",
    )

    assert result is not None
    # Trailing stop must have been called
    orch.order_manager.place_trailing_stop_market.assert_awaited_once()
    call_kwargs = orch.order_manager.place_trailing_stop_market.call_args.kwargs
    assert call_kwargs["symbol"] == "ETH/USDT:USDT"
    assert call_kwargs["side"] == "sell"  # long position → sell trailing
    assert call_kwargs["amount"] == Decimal("0.5")
    # callback_rate = 2.5 * 1.5 / 100 * 100 = 3.75%
    assert call_kwargs["callback_rate"] == pytest.approx(3.75, abs=0.01)
    # activation_price = 100 + 2.0 * 1.5 = 103.0
    assert float(call_kwargs["activation_price"]) == pytest.approx(103.0, abs=0.01)


# ════════════════════════════════════════════════════════════════════════
# R-A5: Post-only maker entry with taker fallback
# ════════════════════════════════════════════════════════════════════════


def _setup_full_execution_mocks(orch: Orchestrator):
    """Configure mocks for a full _execute_signal happy path."""
    signal, cb_state, df = _make_execute_signal_fixtures(orch)

    orch.price_validator.validate_price = AsyncMock(
        return_value=SimpleNamespace(valid=True, issues=[])
    )
    orch.signal_validator.validate_signal = MagicMock(
        return_value=SimpleNamespace(valid=True, issues=[])
    )
    from src.anti_hallucination.decision_auditor import AuditReport

    orch.decision_auditor.audit_decision = MagicMock(
        return_value=AuditReport(
            decision="EXECUTE",
            decision_reasoning="All checks passed",
            price_validated=True,
            signal_validated=True,
            sanity_checks_passed=True,
            risk_approved=True,
            risk_reward_ratio=Decimal("3.0"),
        )
    )
    orch.market_data.fetch_orderbook = AsyncMock(
        return_value=SimpleNamespace(
            bids=[SimpleNamespace(price=Decimal("99.95"))],
            asks=[SimpleNamespace(price=Decimal("100.05"))],
            model_dump=lambda: {
                "bids": [{"price": 99.95, "amount": 10.0}],
                "asks": [{"price": 100.05, "amount": 10.0}],
            },
        )
    )
    orch.slippage_estimator.estimate_slippage = MagicMock(
        return_value=SimpleNamespace(
            slippage_pct=Decimal("0.01"),
            fully_fillable=True,
            levels_consumed=1,
            expected_fill_price=Decimal("100.0"),
        )
    )
    orch.order_manager.place_stop_loss = AsyncMock(
        return_value=SimpleNamespace(order_id="sl1")
    )
    orch.order_manager.place_take_profit = AsyncMock(
        return_value=SimpleNamespace(order_id="tp1")
    )
    orch.order_manager.place_trailing_stop_market = AsyncMock(
        return_value=SimpleNamespace(order_id="trail1")
    )
    orch.alert_system.send_alert = AsyncMock()

    return signal, cb_state, df


@pytest.mark.asyncio
async def test_post_only_limit_fills_uses_maker(
    isolated_orchestrator: Orchestrator,
) -> None:
    """R-A5: When post-only limit fills within timeout, no market order placed."""
    orch = isolated_orchestrator
    signal, cb_state, df = _setup_full_execution_mocks(orch)

    # Post-only limit fills successfully
    orch.order_manager.place_limit_order = AsyncMock(
        return_value=SimpleNamespace(
            order_id="limit1", filled=Decimal("0.5"),
            average_fill_price=Decimal("99.95"),
        )
    )
    orch.order_manager.get_order_status = AsyncMock(
        return_value=SimpleNamespace(status=OrderState.CLOSED)
    )
    orch.order_manager.place_market_order = AsyncMock()

    with patch("src.orchestrator.main.asyncio.sleep", new_callable=AsyncMock):
        result = await orch._execute_signal(
            signal=signal, symbol="ETH/USDT:USDT",
            df_4h=df, df_1h=df, cb_state=cb_state, trigger="test",
        )

    assert result is not None
    assert result["filled_via"] == "maker"
    # Limit order was used, market order should NOT have been called
    orch.order_manager.place_limit_order.assert_awaited_once()
    orch.order_manager.place_market_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_only_unfilled_falls_back_to_market(
    isolated_orchestrator: Orchestrator,
) -> None:
    """R-A5: When post-only remains OPEN after timeout, cancel it and use market."""
    orch = isolated_orchestrator
    signal, cb_state, df = _setup_full_execution_mocks(orch)

    # Post-only limit does NOT fill (stays OPEN)
    orch.order_manager.place_limit_order = AsyncMock(
        return_value=SimpleNamespace(
            order_id="limit1", filled=Decimal("0"),
            average_fill_price=Decimal("0"),
        )
    )
    # First call: check limit status (OPEN), later calls: verify market fill (CLOSED)
    orch.order_manager.get_order_status = AsyncMock(
        side_effect=[
            SimpleNamespace(status=OrderState.OPEN),   # limit check
            SimpleNamespace(status=OrderState.CLOSED),  # market verify
        ]
    )
    orch.order_manager.cancel_order = AsyncMock()
    orch.order_manager.place_market_order = AsyncMock(
        return_value=SimpleNamespace(
            order_id="mkt1", filled=Decimal("0.5"),
            average_fill_price=Decimal("100"),
        )
    )

    with patch("src.orchestrator.main.asyncio.sleep", new_callable=AsyncMock):
        result = await orch._execute_signal(
            signal=signal, symbol="ETH/USDT:USDT",
            df_4h=df, df_1h=df, cb_state=cb_state, trigger="test",
        )

    assert result is not None
    assert result["filled_via"] == "market"
    # Limit tried and cancelled, then market used
    orch.order_manager.place_limit_order.assert_awaited_once()
    orch.order_manager.cancel_order.assert_awaited_once()
    orch.order_manager.place_market_order.assert_awaited_once()
