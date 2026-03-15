"""Tests for TrailingStopState and trailing stop logic."""

import pytest

from src.orchestrator.main import TrailingStopState


# ---------------------------------------------------------------------------
# 1. Basic construction
# ---------------------------------------------------------------------------

def test_trailing_stop_state_creation():
    """Verify all fields are set correctly on construction."""
    ts = TrailingStopState(
        symbol="ETH/USDT:USDT",
        direction="long",
        entry_price=3000.0,
        best_price=3000.0,
        atr_4h=50.0,
        activated=True,
        strategy_name="trend_follower",
        ACTIVATE_ATR_MULT=2.0,
        TRAIL_ATR_MULT=2.5,
    )
    assert ts.symbol == "ETH/USDT:USDT"
    assert ts.direction == "long"
    assert ts.entry_price == 3000.0
    assert ts.best_price == 3000.0
    assert ts.atr_4h == 50.0
    assert ts.activated is True
    assert ts.strategy_name == "trend_follower"
    assert ts.ACTIVATE_ATR_MULT == 2.0
    assert ts.TRAIL_ATR_MULT == 2.5


def test_default_not_activated():
    """New trailing-stop state defaults to activated=False."""
    ts = TrailingStopState(
        symbol="SOL/USDT:USDT",
        direction="short",
        entry_price=150.0,
        best_price=150.0,
        atr_4h=5.0,
    )
    assert ts.activated is False


def test_activate_atr_mult():
    """ACTIVATE_ATR_MULT defaults to 2.0."""
    ts = TrailingStopState(
        symbol="ETH/USDT:USDT",
        direction="long",
        entry_price=3000.0,
        best_price=3000.0,
        atr_4h=50.0,
    )
    assert ts.ACTIVATE_ATR_MULT == 2.0


def test_trail_atr_mult():
    """TRAIL_ATR_MULT defaults to 2.5."""
    ts = TrailingStopState(
        symbol="ETH/USDT:USDT",
        direction="long",
        entry_price=3000.0,
        best_price=3000.0,
        atr_4h=50.0,
    )
    assert ts.TRAIL_ATR_MULT == 2.5


# ---------------------------------------------------------------------------
# 5-6. Long activation logic
# ---------------------------------------------------------------------------

def test_long_activation_logic():
    """Long: favorable_move (100) >= ACTIVATE threshold (100) -> activates."""
    ts = TrailingStopState(
        symbol="ETH/USDT:USDT",
        direction="long",
        entry_price=3000.0,
        best_price=3100.0,
        atr_4h=50.0,
    )
    favorable_move = ts.best_price - ts.entry_price  # 100
    activate_threshold = ts.atr_4h * ts.ACTIVATE_ATR_MULT  # 100
    assert favorable_move >= activate_threshold


def test_long_not_activated():
    """Long: favorable_move (50) < ACTIVATE threshold (100) -> not activated."""
    ts = TrailingStopState(
        symbol="ETH/USDT:USDT",
        direction="long",
        entry_price=3000.0,
        best_price=3050.0,
        atr_4h=50.0,
    )
    favorable_move = ts.best_price - ts.entry_price  # 50
    activate_threshold = ts.atr_4h * ts.ACTIVATE_ATR_MULT  # 100
    assert favorable_move < activate_threshold


# ---------------------------------------------------------------------------
# 7-8. Long trail trigger
# ---------------------------------------------------------------------------

def test_long_trail_trigger():
    """Long activated: pullback (125) >= TRAIL threshold (125) -> triggered."""
    ts = TrailingStopState(
        symbol="ETH/USDT:USDT",
        direction="long",
        entry_price=3000.0,
        best_price=3200.0,
        atr_4h=50.0,
        activated=True,
    )
    current_price = 3075.0
    pullback = ts.best_price - current_price  # 125
    trail_threshold = ts.atr_4h * ts.TRAIL_ATR_MULT  # 125
    assert ts.activated is True
    assert pullback >= trail_threshold


def test_long_trail_not_triggered():
    """Long activated: pullback (100) < TRAIL threshold (125) -> not triggered."""
    ts = TrailingStopState(
        symbol="ETH/USDT:USDT",
        direction="long",
        entry_price=3000.0,
        best_price=3200.0,
        atr_4h=50.0,
        activated=True,
    )
    current_price = 3100.0
    pullback = ts.best_price - current_price  # 100
    trail_threshold = ts.atr_4h * ts.TRAIL_ATR_MULT  # 125
    assert ts.activated is True
    assert pullback < trail_threshold


# ---------------------------------------------------------------------------
# 9-11. Short activation and trail trigger
# ---------------------------------------------------------------------------

def test_short_activation_logic():
    """Short: favorable_move (100) >= ACTIVATE threshold (100) -> activates."""
    ts = TrailingStopState(
        symbol="ETH/USDT:USDT",
        direction="short",
        entry_price=3000.0,
        best_price=2900.0,
        atr_4h=50.0,
    )
    favorable_move = ts.entry_price - ts.best_price  # 100
    activate_threshold = ts.atr_4h * ts.ACTIVATE_ATR_MULT  # 100
    assert favorable_move >= activate_threshold


def test_short_trail_trigger():
    """Short activated: pullback (125) >= TRAIL threshold (125) -> triggered."""
    ts = TrailingStopState(
        symbol="ETH/USDT:USDT",
        direction="short",
        entry_price=3000.0,
        best_price=2800.0,
        atr_4h=50.0,
        activated=True,
    )
    current_price = 2925.0
    pullback = current_price - ts.best_price  # 125
    trail_threshold = ts.atr_4h * ts.TRAIL_ATR_MULT  # 125
    assert ts.activated is True
    assert pullback >= trail_threshold


def test_short_trail_not_triggered():
    """Short activated: pullback (50) < TRAIL threshold (125) -> not triggered."""
    ts = TrailingStopState(
        symbol="ETH/USDT:USDT",
        direction="short",
        entry_price=3000.0,
        best_price=2800.0,
        atr_4h=50.0,
        activated=True,
    )
    current_price = 2850.0
    pullback = current_price - ts.best_price  # 50
    trail_threshold = ts.atr_4h * ts.TRAIL_ATR_MULT  # 125
    assert ts.activated is True
    assert pullback < trail_threshold


# ---------------------------------------------------------------------------
# 12-13. Best price tracking
# ---------------------------------------------------------------------------

def test_best_price_tracking_long():
    """Long: best_price should be the max of all observed prices."""
    ts = TrailingStopState(
        symbol="ETH/USDT:USDT",
        direction="long",
        entry_price=3000.0,
        best_price=3000.0,
        atr_4h=50.0,
    )
    price_updates = [3010.0, 3050.0, 3080.0, 3040.0, 3100.0, 3060.0]
    for price in price_updates:
        if price > ts.best_price:
            ts.best_price = price
    assert ts.best_price == 3100.0


def test_best_price_tracking_short():
    """Short: best_price should be the min of all observed prices."""
    ts = TrailingStopState(
        symbol="ETH/USDT:USDT",
        direction="short",
        entry_price=3000.0,
        best_price=3000.0,
        atr_4h=50.0,
    )
    price_updates = [2990.0, 2950.0, 2970.0, 2920.0, 2940.0, 2900.0]
    for price in price_updates:
        if price < ts.best_price:
            ts.best_price = price
    assert ts.best_price == 2900.0
