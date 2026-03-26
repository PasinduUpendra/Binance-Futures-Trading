"""Comprehensive tests for the TradeJournal module.

Tests cover recording, querying, filtering, analytics (win rate, consecutive
losses), data persistence, Decimal precision, and INSERT OR REPLACE semantics.
All tests use tmp_path for full DB isolation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from src.memory.trade_journal import TradeEntry, TradeJournal


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def make_trade(
    symbol: str = "ETH/USDT:USDT",
    direction: str = "long",
    pnl: Decimal | None = None,
    strategy: str = "supertrend_trend",
    regime: str = "trending",
    **kwargs,
) -> TradeEntry:
    """Create a minimal TradeEntry for testing."""
    defaults = dict(
        symbol=symbol,
        direction=direction,
        entry_price=Decimal("2000"),
        size=Decimal("0.01"),
        pnl=pnl,
        strategy=strategy,
        regime=regime,
    )
    defaults.update(kwargs)
    return TradeEntry(**defaults)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def journal(tmp_path: Path) -> TradeJournal:
    """Return a fresh TradeJournal backed by a temporary SQLite DB."""
    db = tmp_path / "test_journal.db"
    j = TradeJournal(db_path=db)
    yield j
    j.close()


# ---------------------------------------------------------------------------
# 1. record_trade and get_trade_count
# ---------------------------------------------------------------------------


def test_record_trade_and_get_trade_count(journal: TradeJournal) -> None:
    """Recording a trade increments the trade count."""
    assert journal.get_trade_count() == 0

    journal.record_trade(make_trade())
    assert journal.get_trade_count() == 1

    journal.record_trade(make_trade())
    assert journal.get_trade_count() == 2


# ---------------------------------------------------------------------------
# 2. record_trade with full TradeEntry (all fields populated)
# ---------------------------------------------------------------------------


def test_record_trade_full_fields(journal: TradeJournal) -> None:
    """All TradeEntry fields survive a round-trip through SQLite."""
    ts = datetime(2026, 3, 14, 12, 0, 0, tzinfo=timezone.utc)
    entry = TradeEntry(
        trade_id="full_test_id_1234",
        timestamp=ts,
        symbol="SOL/USDT:USDT",
        direction="short",
        entry_price=Decimal("150.25"),
        exit_price=Decimal("148.00"),
        size=Decimal("0.5"),
        leverage=5,
        pnl=Decimal("1.125"),
        pnl_pct=Decimal("0.75"),
        strategy="mean_reversion",
        regime="ranging",
        confidence=Decimal("0.85"),
        stop_loss=Decimal("152.00"),
        take_profit=Decimal("146.00"),
        duration=3600.0,
        fees=Decimal("0.045"),
        slippage=Decimal("0.01"),
        reasoning="Mean reversion signal at upper band",
        lessons="Tighter stop would have been better",
    )

    journal.record_trade(entry)
    trades = journal.get_all_trades()
    assert len(trades) == 1
    t = trades[0]

    assert t.trade_id == "full_test_id_1234"
    assert t.timestamp == ts
    assert t.symbol == "SOL/USDT:USDT"
    assert t.direction == "short"
    assert t.entry_price == Decimal("150.25")
    assert t.exit_price == Decimal("148.00")
    assert t.size == Decimal("0.5")
    assert t.leverage == 5
    assert t.pnl == Decimal("1.125")
    assert t.pnl_pct == Decimal("0.75")
    assert t.strategy == "mean_reversion"
    assert t.regime == "ranging"
    assert t.confidence == Decimal("0.85")
    assert t.stop_loss == Decimal("152.00")
    assert t.take_profit == Decimal("146.00")
    assert t.duration == 3600.0
    assert t.fees == Decimal("0.045")
    assert t.slippage == Decimal("0.01")
    assert t.reasoning == "Mean reversion signal at upper band"
    assert t.lessons == "Tighter stop would have been better"


# ---------------------------------------------------------------------------
# 3. get_recent_trades returns correct count
# ---------------------------------------------------------------------------


def test_get_recent_trades_correct_count(journal: TradeJournal) -> None:
    """get_recent_trades(n) returns at most n trades."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(5):
        journal.record_trade(
            make_trade(timestamp=base + timedelta(hours=i))
        )

    assert len(journal.get_recent_trades(n=3)) == 3
    assert len(journal.get_recent_trades(n=5)) == 5


# ---------------------------------------------------------------------------
# 4. get_recent_trades returns in descending timestamp order
# ---------------------------------------------------------------------------


def test_get_recent_trades_descending_order(journal: TradeJournal) -> None:
    """Most recent trade comes first."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(5):
        journal.record_trade(
            make_trade(timestamp=base + timedelta(hours=i))
        )

    trades = journal.get_recent_trades(n=5)
    timestamps = [t.timestamp for t in trades]
    assert timestamps == sorted(timestamps, reverse=True)


# ---------------------------------------------------------------------------
# 5. get_recent_trades with n larger than total trades
# ---------------------------------------------------------------------------


def test_get_recent_trades_n_larger_than_total(journal: TradeJournal) -> None:
    """Requesting more trades than exist returns all trades without error."""
    journal.record_trade(make_trade())
    journal.record_trade(make_trade())

    trades = journal.get_recent_trades(n=100)
    assert len(trades) == 2


# ---------------------------------------------------------------------------
# 6. get_trades_by_strategy filters correctly
# ---------------------------------------------------------------------------


def test_get_trades_by_strategy_filters(journal: TradeJournal) -> None:
    """Only trades matching the strategy are returned."""
    journal.record_trade(make_trade(strategy="supertrend_trend"))
    journal.record_trade(make_trade(strategy="supertrend_trend"))
    journal.record_trade(make_trade(strategy="mean_reversion"))
    journal.record_trade(make_trade(strategy="breakout_trader"))

    result = journal.get_trades_by_strategy("supertrend_trend")
    assert len(result) == 2
    assert all(t.strategy == "supertrend_trend" for t in result)

    result_mr = journal.get_trades_by_strategy("mean_reversion")
    assert len(result_mr) == 1


# ---------------------------------------------------------------------------
# 7. get_trades_by_strategy returns empty list for unknown strategy
# ---------------------------------------------------------------------------


def test_get_trades_by_strategy_unknown(journal: TradeJournal) -> None:
    """Unknown strategy returns an empty list, not an error."""
    journal.record_trade(make_trade(strategy="supertrend_trend"))
    assert journal.get_trades_by_strategy("nonexistent") == []


# ---------------------------------------------------------------------------
# 8. get_trades_by_regime filters correctly
# ---------------------------------------------------------------------------


def test_get_trades_by_regime_filters(journal: TradeJournal) -> None:
    """Only trades matching the regime are returned."""
    journal.record_trade(make_trade(regime="trending"))
    journal.record_trade(make_trade(regime="trending"))
    journal.record_trade(make_trade(regime="ranging"))

    result = journal.get_trades_by_regime("trending")
    assert len(result) == 2
    assert all(t.regime == "trending" for t in result)

    result_r = journal.get_trades_by_regime("ranging")
    assert len(result_r) == 1


# ---------------------------------------------------------------------------
# 9. get_win_rate with all wins -> 1.0
# ---------------------------------------------------------------------------


def test_get_win_rate_all_wins(journal: TradeJournal) -> None:
    """Win rate is 1.0 when every trade is profitable."""
    for _ in range(5):
        journal.record_trade(make_trade(pnl=Decimal("10")))

    assert journal.get_win_rate() == 1.0


# ---------------------------------------------------------------------------
# 10. get_win_rate with all losses -> 0.0
# ---------------------------------------------------------------------------


def test_get_win_rate_all_losses(journal: TradeJournal) -> None:
    """Win rate is 0.0 when every trade is a loss."""
    for _ in range(5):
        journal.record_trade(make_trade(pnl=Decimal("-5")))

    assert journal.get_win_rate() == 0.0


# ---------------------------------------------------------------------------
# 11. get_win_rate with mixed trades -> correct ratio
# ---------------------------------------------------------------------------


def test_get_win_rate_mixed(journal: TradeJournal) -> None:
    """Win rate reflects the correct ratio of winners to total."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    pnls = [
        Decimal("10"),   # win
        Decimal("-5"),   # loss
        Decimal("3"),    # win
        Decimal("-1"),   # loss
        Decimal("7"),    # win
    ]
    for i, pnl in enumerate(pnls):
        journal.record_trade(
            make_trade(pnl=pnl, timestamp=base + timedelta(hours=i))
        )

    # 3 wins out of 5
    assert journal.get_win_rate() == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# 12. get_win_rate with last_n parameter
# ---------------------------------------------------------------------------


def test_get_win_rate_last_n(journal: TradeJournal) -> None:
    """last_n restricts win rate calculation to the most recent N trades."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Oldest -> newest: loss, loss, win, win, win
    pnls = [
        Decimal("-5"),
        Decimal("-5"),
        Decimal("10"),
        Decimal("10"),
        Decimal("10"),
    ]
    for i, pnl in enumerate(pnls):
        journal.record_trade(
            make_trade(pnl=pnl, timestamp=base + timedelta(hours=i))
        )

    # Last 3 trades (newest): all wins
    assert journal.get_win_rate(last_n=3) == 1.0
    # Last 5 trades: 3 wins / 5 = 0.6
    assert journal.get_win_rate(last_n=5) == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# 13. get_win_rate with no trades -> 0.0
# ---------------------------------------------------------------------------


def test_get_win_rate_no_trades(journal: TradeJournal) -> None:
    """Win rate is 0.0 when the journal is empty."""
    assert journal.get_win_rate() == 0.0


# ---------------------------------------------------------------------------
# 14. get_consecutive_losses with 3 losses then a win -> 3
# ---------------------------------------------------------------------------


def test_get_consecutive_losses_streak(journal: TradeJournal) -> None:
    """Consecutive loss count reflects the streak from most recent backwards."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Oldest -> newest: win, win, loss, loss, loss
    pnls = [
        Decimal("10"),
        Decimal("5"),
        Decimal("-3"),
        Decimal("-2"),
        Decimal("-1"),
    ]
    for i, pnl in enumerate(pnls):
        journal.record_trade(
            make_trade(pnl=pnl, timestamp=base + timedelta(hours=i))
        )

    assert journal.get_consecutive_losses() == 3


# ---------------------------------------------------------------------------
# 15. get_consecutive_losses with most recent win -> 0
# ---------------------------------------------------------------------------


def test_get_consecutive_losses_recent_win(journal: TradeJournal) -> None:
    """If the most recent trade is a win, consecutive losses is 0."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    journal.record_trade(
        make_trade(pnl=Decimal("-5"), timestamp=base)
    )
    journal.record_trade(
        make_trade(pnl=Decimal("10"), timestamp=base + timedelta(hours=1))
    )

    assert journal.get_consecutive_losses() == 0


# ---------------------------------------------------------------------------
# 16. get_consecutive_losses with no trades -> 0
# ---------------------------------------------------------------------------


def test_get_consecutive_losses_empty(journal: TradeJournal) -> None:
    """Consecutive losses is 0 for an empty journal."""
    assert journal.get_consecutive_losses() == 0


# ---------------------------------------------------------------------------
# 17. get_all_trades returns all
# ---------------------------------------------------------------------------


def test_get_all_trades(journal: TradeJournal) -> None:
    """get_all_trades returns every recorded trade in descending order."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(7):
        journal.record_trade(
            make_trade(timestamp=base + timedelta(hours=i))
        )

    trades = journal.get_all_trades()
    assert len(trades) == 7
    timestamps = [t.timestamp for t in trades]
    assert timestamps == sorted(timestamps, reverse=True)


# ---------------------------------------------------------------------------
# 18. record_trade duplicate trade_id replaces existing (INSERT OR REPLACE)
# ---------------------------------------------------------------------------


def test_record_trade_duplicate_replaces(journal: TradeJournal) -> None:
    """INSERT OR REPLACE overwrites a trade with the same trade_id."""
    entry = make_trade(pnl=Decimal("10"))
    trade_id = entry.trade_id

    journal.record_trade(entry)
    assert journal.get_trade_count() == 1

    # Same trade_id, different PnL
    updated = make_trade(trade_id=trade_id, pnl=Decimal("-5"))
    journal.record_trade(updated)

    assert journal.get_trade_count() == 1  # still 1
    trades = journal.get_all_trades()
    assert trades[0].pnl == Decimal("-5")


# ---------------------------------------------------------------------------
# 19. close() and reopen preserves data
# ---------------------------------------------------------------------------


def test_close_and_reopen_preserves_data(tmp_path: Path) -> None:
    """Data persists after closing and reopening the journal."""
    db = tmp_path / "persist_test.db"

    journal1 = TradeJournal(db_path=db)
    journal1.record_trade(make_trade(pnl=Decimal("42")))
    journal1.close()

    journal2 = TradeJournal(db_path=db)
    assert journal2.get_trade_count() == 1
    trades = journal2.get_all_trades()
    assert trades[0].pnl == Decimal("42")
    journal2.close()


# ---------------------------------------------------------------------------
# 20. Decimal precision preserved through round-trip
# ---------------------------------------------------------------------------


def test_decimal_precision_round_trip(journal: TradeJournal) -> None:
    """High-precision Decimal values survive storage and retrieval exactly."""
    precise_price = Decimal("2345.67890123456789")
    precise_pnl = Decimal("-0.00000001")
    precise_fee = Decimal("0.00012345")
    precise_slippage = Decimal("0.000000999")

    entry = make_trade(
        entry_price=precise_price,
        pnl=precise_pnl,
        fees=precise_fee,
        slippage=precise_slippage,
        exit_price=Decimal("2345.67890123456790"),
        confidence=Decimal("0.99999"),
    )
    journal.record_trade(entry)

    retrieved = journal.get_all_trades()[0]
    assert retrieved.entry_price == precise_price
    assert retrieved.pnl == precise_pnl
    assert retrieved.fees == precise_fee
    assert retrieved.slippage == precise_slippage
    assert retrieved.exit_price == Decimal("2345.67890123456790")
    assert retrieved.confidence == Decimal("0.99999")


# ---------------------------------------------------------------------------
# 21. update_trade_exit — basic success
# ---------------------------------------------------------------------------


def test_update_trade_exit_basic(journal: TradeJournal) -> None:
    """update_trade_exit fills in exit fields on the most recent open trade."""
    # Entry with no exit data (as produced by the orchestrator)
    entry = make_trade(symbol="BTC/USDT:USDT")
    journal.record_trade(entry)

    # Verify pnl is initially None
    trade_before = journal.get_all_trades()[0]
    assert trade_before.pnl is None
    assert trade_before.exit_price is None

    # Record exit
    result = journal.update_trade_exit(
        symbol="BTC/USDT:USDT",
        exit_price=Decimal("21000"),
        pnl=Decimal("50.0"),
        pnl_pct=Decimal("2.5"),
        duration=24.5,
        reason="trailing_stop",
    )
    assert result is True

    # Verify fields updated
    trade_after = journal.get_all_trades()[0]
    assert trade_after.exit_price == Decimal("21000")
    assert trade_after.pnl == Decimal("50.0")
    assert trade_after.pnl_pct == Decimal("2.5")
    assert trade_after.duration == 24.5
    assert trade_after.lessons == "trailing_stop"


# ---------------------------------------------------------------------------
# 22. update_trade_exit — no matching trade
# ---------------------------------------------------------------------------


def test_update_trade_exit_no_match(journal: TradeJournal) -> None:
    """Returns False when no open trade exists for the symbol."""
    result = journal.update_trade_exit(
        symbol="DOGE/USDT:USDT",
        exit_price=Decimal("0.15"),
        pnl=Decimal("-1.0"),
        pnl_pct=Decimal("-0.5"),
    )
    assert result is False


# ---------------------------------------------------------------------------
# 23. update_trade_exit — targets the most recent open trade
# ---------------------------------------------------------------------------


def test_update_trade_exit_targets_most_recent_open(journal: TradeJournal) -> None:
    """When multiple trades exist for a symbol, updates the most recent one
    that still has no exit_price."""
    # Older trade (already closed via record_trade)
    older = make_trade(
        symbol="ETH/USDT:USDT",
        pnl=Decimal("10"),
        exit_price=Decimal("2100"),
        timestamp=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    journal.record_trade(older)

    # Newer trade (still open — no exit_price/pnl)
    newer = make_trade(
        symbol="ETH/USDT:USDT",
        timestamp=datetime(2026, 3, 15, tzinfo=timezone.utc),
    )
    journal.record_trade(newer)

    # update_trade_exit should fill in the NEWER one
    result = journal.update_trade_exit(
        symbol="ETH/USDT:USDT",
        exit_price=Decimal("2200"),
        pnl=Decimal("25.0"),
        pnl_pct=Decimal("1.25"),
        reason="time_exit",
    )
    assert result is True

    trades = journal.get_all_trades()
    # Most recent (newer) should have exit data
    assert trades[0].exit_price == Decimal("2200")
    assert trades[0].pnl == Decimal("25.0")
    assert trades[0].lessons == "time_exit"

    # Older trade untouched
    assert trades[1].exit_price == Decimal("2100")
    assert trades[1].pnl == Decimal("10")


# ---------------------------------------------------------------------------
# 24. update_trade_exit — consecutive losses now tracked
# ---------------------------------------------------------------------------


def test_update_trade_exit_enables_consecutive_loss_tracking(
    journal: TradeJournal,
) -> None:
    """After update_trade_exit is called, get_consecutive_losses returns
    accurate data (previously always 0 because exits were never recorded)."""
    # Record 3 trades with no exit
    for i in range(3):
        entry = make_trade(
            symbol="SOL/USDT:USDT",
            trade_id=f"trade_{i}",
            timestamp=datetime(2026, 3, 10 + i, tzinfo=timezone.utc),
        )
        journal.record_trade(entry)

    # Close each trade as a loss
    for i in range(3):
        journal.update_trade_exit(
            symbol="SOL/USDT:USDT",
            exit_price=Decimal("150"),
            pnl=Decimal(f"-{i + 1}.0"),
            pnl_pct=Decimal("-0.5"),
            reason="sl_tp_fire",
        )

    assert journal.get_consecutive_losses() == 3


# ---------------------------------------------------------------------------
# 25. update_trade_exit — win rate after exit
# ---------------------------------------------------------------------------


def test_update_trade_exit_win_rate(journal: TradeJournal) -> None:
    """Win rate reflects exits recorded via update_trade_exit."""
    for i in range(4):
        entry = make_trade(
            symbol="BTC/USDT:USDT",
            trade_id=f"wr_{i}",
            timestamp=datetime(2026, 3, 10 + i, tzinfo=timezone.utc),
        )
        journal.record_trade(entry)

    # 3 wins, 1 loss
    pnls = [Decimal("10"), Decimal("20"), Decimal("5"), Decimal("-8")]
    for i, pnl_val in enumerate(pnls):
        journal.update_trade_exit(
            symbol="BTC/USDT:USDT",
            exit_price=Decimal("21000"),
            pnl=pnl_val,
            pnl_pct=Decimal("1.0") if pnl_val > 0 else Decimal("-0.5"),
        )

    assert journal.get_win_rate() == pytest.approx(0.75)
