"""
Tests for DrawdownMonitor — tracks peak balance, drawdown percentages,
and persists state across restarts.
"""

import json
import pytest
from decimal import Decimal
from pathlib import Path

from src.risk.drawdown_monitor import DrawdownMonitor, DrawdownState


# ─── Fixtures ───


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    """Return a temporary path for drawdown state JSON."""
    return tmp_path / "drawdown_state.json"


@pytest.fixture
def monitor(state_path: Path) -> DrawdownMonitor:
    """Return a DrawdownMonitor initialised with a $100 balance."""
    return DrawdownMonitor(state_path=state_path, initial_balance=100)


# ─── Initialization ───


class TestInitialization:
    """Tests for DrawdownMonitor construction."""

    def test_fresh_init_with_initial_balance(self, state_path: Path):
        """Fresh init with initial_balance sets peak and current correctly."""
        mon = DrawdownMonitor(state_path=state_path, initial_balance=100)
        state = mon.get_state()
        assert state.peak_balance == Decimal("100")
        assert state.current_balance == Decimal("100")
        assert state.current_drawdown_pct == Decimal("0")
        assert state.max_drawdown_pct == Decimal("0")

    def test_fresh_init_without_initial_balance_no_file(self, state_path: Path):
        """Fresh init without initial_balance and no file starts at zero."""
        mon = DrawdownMonitor(state_path=state_path)
        state = mon.get_state()
        assert state.peak_balance == Decimal("0")
        assert state.current_balance == Decimal("0")
        assert state.current_drawdown_pct == Decimal("0")
        assert state.max_drawdown_pct == Decimal("0")

    def test_initial_balance_accepts_float(self, state_path: Path):
        """Accepts float for initial_balance and converts to Decimal."""
        mon = DrawdownMonitor(state_path=state_path, initial_balance=50.5)
        state = mon.get_state()
        assert state.peak_balance == Decimal("50.5")

    def test_initial_balance_accepts_string(self, state_path: Path):
        """Accepts string for initial_balance and converts to Decimal."""
        mon = DrawdownMonitor(state_path=state_path, initial_balance="75.25")
        state = mon.get_state()
        assert state.peak_balance == Decimal("75.25")


# ─── Update / High-Water Mark ───


class TestUpdate:
    """Tests for the update() method — balance tracking & drawdown calculation."""

    def test_increasing_balance_updates_peak(self, monitor: DrawdownMonitor):
        """update() with increasing balance should raise the peak."""
        state = monitor.update(110)
        assert state.peak_balance == Decimal("110")
        assert state.current_balance == Decimal("110")

    def test_decreasing_balance_does_not_change_peak(self, monitor: DrawdownMonitor):
        """update() with decreasing balance must NOT lower the peak."""
        monitor.update(110)
        state = monitor.update(95)
        assert state.peak_balance == Decimal("110")
        assert state.current_balance == Decimal("95")

    def test_drawdown_pct_calculation(self, monitor: DrawdownMonitor):
        """peak=100, current=90 -> drawdown 0.1 (10%)."""
        state = monitor.update(90)
        expected = Decimal("0.1000")
        assert state.current_drawdown_pct == expected

    def test_max_drawdown_tracked_across_updates(self, monitor: DrawdownMonitor):
        """max_drawdown_pct should reflect the deepest trough seen."""
        monitor.update(90)  # 10% dd
        monitor.update(95)  # 5% dd (recovery)
        state = monitor.update(80)  # 20% dd — new max
        assert state.max_drawdown_pct == Decimal("0.2000")

    def test_max_drawdown_only_increases(self, monitor: DrawdownMonitor):
        """max_drawdown_pct must never decrease, even after recovery."""
        monitor.update(80)  # 20% dd
        state = monitor.update(100)  # full recovery
        assert state.current_drawdown_pct == Decimal("0")
        assert state.max_drawdown_pct == Decimal("0.2000")


# ─── Getters ───


class TestGetters:
    """Tests for get_drawdown_pct() and get_state()."""

    def test_get_drawdown_pct_returns_correct_value(self, monitor: DrawdownMonitor):
        """get_drawdown_pct() should reflect the latest update."""
        monitor.update(90)
        assert monitor.get_drawdown_pct() == Decimal("0.1000")

    def test_get_state_returns_snapshot_without_modification(
        self, monitor: DrawdownMonitor
    ):
        """get_state() returns current state and does not mutate internal data."""
        monitor.update(90)
        state1 = monitor.get_state()
        state2 = monitor.get_state()
        assert state1.current_balance == state2.current_balance
        assert state1.peak_balance == state2.peak_balance
        assert state1.current_drawdown_pct == state2.current_drawdown_pct
        assert state1.max_drawdown_pct == state2.max_drawdown_pct


# ─── Reset ───


class TestReset:
    """Tests for reset() — hard wipe of drawdown history."""

    def test_reset_wipes_peak_and_max_drawdown(self, monitor: DrawdownMonitor):
        """reset() should clear max_drawdown history."""
        monitor.update(80)  # 20% dd
        monitor.reset(50)
        state = monitor.get_state()
        assert state.max_drawdown_pct == Decimal("0")

    def test_reset_sets_new_baseline(self, monitor: DrawdownMonitor):
        """reset() sets both peak and current to the new balance."""
        monitor.update(120)
        monitor.reset(50)
        state = monitor.get_state()
        assert state.peak_balance == Decimal("50")
        assert state.current_balance == Decimal("50")
        assert state.current_drawdown_pct == Decimal("0")


# ─── Persistence ───


class TestPersistence:
    """Tests for JSON state persistence across restarts."""

    def test_state_survives_reinitialization(self, state_path: Path):
        """Write state, create a new instance, and verify it loads correctly."""
        mon1 = DrawdownMonitor(state_path=state_path, initial_balance=100)
        mon1.update(110)
        mon1.update(85)  # dd from peak 110

        # New instance reads from the same file — should restore state.
        mon2 = DrawdownMonitor(state_path=state_path)
        state = mon2.get_state()
        assert state.peak_balance == Decimal("110")
        assert state.current_balance == Decimal("85")
        assert state.max_drawdown_pct > Decimal("0")

    def test_corrupted_json_handled_gracefully(self, state_path: Path):
        """Corrupted JSON file should not crash; monitor starts fresh."""
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("{{{invalid json!!!", encoding="utf-8")
        mon = DrawdownMonitor(state_path=state_path, initial_balance=50)
        state = mon.get_state()
        # Should fall back to initial_balance since load failed.
        assert state.peak_balance == Decimal("50")
        assert state.current_balance == Decimal("50")

    def test_missing_file_handled_gracefully(self, state_path: Path):
        """Missing file on init should not crash."""
        assert not state_path.exists()
        mon = DrawdownMonitor(state_path=state_path)
        state = mon.get_state()
        assert state.peak_balance == Decimal("0")
        assert state.current_balance == Decimal("0")

    def test_persisted_file_is_valid_json(self, state_path: Path):
        """State file written by _persist_state is valid JSON with expected keys."""
        mon = DrawdownMonitor(state_path=state_path, initial_balance=100)
        mon.update(90)
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        assert "peak_balance" in raw
        assert "current_balance" in raw
        assert "max_drawdown_pct" in raw
        assert "max_drawdown_balance" in raw
        assert "updated_at" in raw


# ─── Edge Cases ───


class TestEdgeCases:
    """Edge-case scenarios."""

    def test_zero_peak_returns_zero_drawdown(self, state_path: Path):
        """If peak is zero, drawdown should be 0 (avoid division by zero)."""
        mon = DrawdownMonitor(state_path=state_path)
        # Peak is 0 by default with no initial balance.
        assert mon.get_drawdown_pct() == Decimal("0")

    def test_negative_balance(self, state_path: Path):
        """Negative balance should produce a drawdown capped sensibly."""
        mon = DrawdownMonitor(state_path=state_path, initial_balance=100)
        state = mon.update(-10)
        # Drawdown = (100 - (-10)) / 100 = 1.1 = 110%
        assert state.current_drawdown_pct == Decimal("1.1000")
        assert state.peak_balance == Decimal("100")


# ─── Full Sequence ───


class TestDrawdownSequence:
    """End-to-end drawdown sequence verifying peak, current_dd, max_dd at each step."""

    def test_full_lifecycle(self, state_path: Path):
        """
        Sequence: $100 -> $90 -> $95 -> $80 -> $110 -> $100
        Verify peak, current_drawdown_pct, max_drawdown_pct at every step.
        """
        mon = DrawdownMonitor(state_path=state_path, initial_balance=100)

        # Step 1: $100 -> $90  (peak=100, dd=10%, max_dd=10%)
        s = mon.update(90)
        assert s.peak_balance == Decimal("100")
        assert s.current_drawdown_pct == Decimal("0.1000")
        assert s.max_drawdown_pct == Decimal("0.1000")

        # Step 2: $90 -> $95  (peak=100, dd=5%, max_dd=10% unchanged)
        s = mon.update(95)
        assert s.peak_balance == Decimal("100")
        assert s.current_drawdown_pct == Decimal("0.0500")
        assert s.max_drawdown_pct == Decimal("0.1000")

        # Step 3: $95 -> $80  (peak=100, dd=20%, max_dd=20% new worst)
        s = mon.update(80)
        assert s.peak_balance == Decimal("100")
        assert s.current_drawdown_pct == Decimal("0.2000")
        assert s.max_drawdown_pct == Decimal("0.2000")
        assert s.max_drawdown_balance == Decimal("80")

        # Step 4: $80 -> $110  (new peak=110, dd=0%, max_dd=20% unchanged)
        s = mon.update(110)
        assert s.peak_balance == Decimal("110")
        assert s.current_drawdown_pct == Decimal("0")
        assert s.max_drawdown_pct == Decimal("0.2000")

        # Step 5: $110 -> $100  (peak=110, dd ~9.09%, max_dd=20% unchanged)
        s = mon.update(100)
        assert s.peak_balance == Decimal("110")
        expected_dd = ((Decimal("110") - Decimal("100")) / Decimal("110")).quantize(
            Decimal("0.0001")
        )
        assert s.current_drawdown_pct == expected_dd
        assert s.max_drawdown_pct == Decimal("0.2000")


# ═══════════════════════════════════════════════════════════════════
# R-C2: Rolling equity drawdown halt
# ═══════════════════════════════════════════════════════════════════


class TestRollingDrawdownHalt:
    """should_halt_rolling triggers at threshold and clears below."""

    def test_halt_when_above_threshold(self, tmp_path):
        mon = DrawdownMonitor(state_path=tmp_path / "dd.json", initial_balance=100)
        mon.update(100)
        mon.update(80)  # 20% drawdown
        assert mon.should_halt_rolling(Decimal("0.15")) is True

    def test_no_halt_when_below_threshold(self, tmp_path):
        mon = DrawdownMonitor(state_path=tmp_path / "dd.json", initial_balance=100)
        mon.update(100)
        mon.update(90)  # 10% drawdown
        assert mon.should_halt_rolling(Decimal("0.15")) is False

    def test_halt_exactly_at_threshold(self, tmp_path):
        mon = DrawdownMonitor(state_path=tmp_path / "dd.json", initial_balance=100)
        mon.update(100)
        mon.update(85)  # 15% drawdown exactly
        assert mon.should_halt_rolling(Decimal("0.15")) is True

    def test_halt_clears_after_new_peak(self, tmp_path):
        mon = DrawdownMonitor(state_path=tmp_path / "dd.json", initial_balance=100)
        mon.update(100)
        mon.update(80)  # 20% dd → halt
        assert mon.should_halt_rolling(Decimal("0.15")) is True
        mon.update(110)  # new peak
        assert mon.should_halt_rolling(Decimal("0.15")) is False
