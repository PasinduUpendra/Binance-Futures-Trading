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

from src.orchestrator import main as orchestrator_main
from src.orchestrator.main import Orchestrator, TrailingStopState


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


def _make_order(order_type: str) -> SimpleNamespace:
    return SimpleNamespace(order_type=order_type)


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
    # Only a TP order, no SL
    isolated_orchestrator.order_manager.get_open_orders = AsyncMock(
        return_value=[_make_order("take_profit_market")]
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
    # Only an SL order, no TP
    isolated_orchestrator.order_manager.get_open_orders = AsyncMock(
        return_value=[_make_order("stop_market")]
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
    isolated_orchestrator.order_manager.get_open_orders = AsyncMock(
        return_value=[_make_order("stop_market"), _make_order("take_profit_market")]
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
async def test_swap_requires_confidence_70(
    isolated_orchestrator: Orchestrator,
) -> None:
    """Low-confidence signals should not trigger swap."""
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
        new_confidence=65.0, open_positions=positions,
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
async def test_swap_requires_confidence_delta_20(
    isolated_orchestrator: Orchestrator,
) -> None:
    """New signal needs >= 20 point confidence advantage to swap."""
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
    # Entry confidence = 60, new = 75 → delta = 15 < 20
    mock_trade = SimpleNamespace(
        symbol="ADA/USDT:USDT", exit_price=None, confidence=Decimal("60"),
    )
    isolated_orchestrator.trade_journal.get_recent_trades = MagicMock(
        return_value=[mock_trade]
    )
    result = await isolated_orchestrator._find_swap_candidate(
        new_confidence=75.0, open_positions=positions,
    )
    assert result is None

    # Now with delta >= 20 (new=85 - old=60 = 25)
    result = await isolated_orchestrator._find_swap_candidate(
        new_confidence=85.0, open_positions=positions,
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
    )
    isolated_orchestrator._trailing_stops["ADA/USDT:USDT"] = TrailingStopState(
        symbol="ADA/USDT:USDT", direction="long",
        entry_price=0.30, best_price=0.30, atr_4h=0.01,
    )
    isolated_orchestrator.order_manager.cancel_open_orders = AsyncMock(return_value=2)
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
    # Has SL only
    isolated_orchestrator.order_manager.get_open_orders = AsyncMock(
        return_value=[_make_order("stop_market")]
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
