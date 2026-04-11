from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.execution.fee_calculator import DEFAULT_TAKER_FEE
from src.orchestrator import main as orchestrator_main
from src.orchestrator.main import Orchestrator, TrailingStopState


@pytest.fixture
def isolated_orchestrator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Orchestrator:
    monkeypatch.setattr(orchestrator_main, "AGENT_STATE_DIR", tmp_path)
    monkeypatch.setattr(Orchestrator, "_DAILY_STATE_FILE", tmp_path / "daily_state.json")
    monkeypatch.setattr(Orchestrator, "_TRAILING_STATE_FILE", tmp_path / "trailing_stops.json")
    return Orchestrator()


def test_trailing_stop_state_round_trip_persists_best_price_and_activation(
    isolated_orchestrator: Orchestrator,
) -> None:
    isolated_orchestrator._trailing_stops = {
        "AVAX/USDT:USDT": TrailingStopState(
            symbol="AVAX/USDT:USDT",
            direction="short",
            entry_price=9.5,
            best_price=8.8,
            atr_4h=0.25,
            activated=True,
            strategy_name="SupertrendTrend",
            take_profit=8.2,
        )
    }

    isolated_orchestrator._persist_trailing_stop_state()
    isolated_orchestrator._trailing_stops = {}
    isolated_orchestrator._load_trailing_stop_state()

    restored = isolated_orchestrator._trailing_stops["AVAX/USDT:USDT"]
    assert restored.best_price == 8.8
    assert restored.activated is True
    assert restored.atr_4h == 0.25
    assert restored.take_profit == 8.2


@pytest.mark.asyncio
async def test_detect_preexisting_positions_preserves_persisted_trailing_state(
    isolated_orchestrator: Orchestrator,
) -> None:
    isolated_orchestrator._trailing_stops = {
        "ADA/USDT:USDT": TrailingStopState(
            symbol="ADA/USDT:USDT",
            direction="short",
            entry_price=0.72,
            best_price=0.66,
            atr_4h=0.02,
            activated=True,
            strategy_name="SupertrendTrend",
            take_profit=0.61,
        )
    }
    isolated_orchestrator.position_tracker.get_open_positions = AsyncMock(
        return_value=[
            SimpleNamespace(
                symbol="ADA/USDT:USDT",
                side="short",
                entry_price=0.72,
                current_price=0.69,
            )
        ]
    )
    isolated_orchestrator.order_manager.get_open_orders = AsyncMock(return_value=[{"id": "1"}])

    await isolated_orchestrator._detect_preexisting_positions()

    restored = isolated_orchestrator._trailing_stops["ADA/USDT:USDT"]
    assert restored.best_price == 0.66
    assert restored.activated is True
    assert restored.atr_4h == 0.02
    assert restored.take_profit == 0.61


@pytest.mark.asyncio
async def test_configure_fee_calculator_disables_discount_without_bnb(
    isolated_orchestrator: Orchestrator,
) -> None:
    # Mock fetch_commission_rate to return defaults (no real API call)
    isolated_orchestrator.market_data.fetch_commission_rate = AsyncMock(
        side_effect=Exception("no exchange")
    )
    await isolated_orchestrator._configure_fee_calculator(
        [SimpleNamespace(asset="USDT", wallet_balance=1)]
    )

    assert isolated_orchestrator.fee_calculator.taker_fee_rate == DEFAULT_TAKER_FEE


@pytest.mark.asyncio
async def test_configure_fee_calculator_enables_discount_with_bnb(
    isolated_orchestrator: Orchestrator,
) -> None:
    isolated_orchestrator.market_data.fetch_commission_rate = AsyncMock(
        side_effect=Exception("no exchange")
    )
    await isolated_orchestrator._configure_fee_calculator(
        [SimpleNamespace(asset="BNB", wallet_balance=0.5)]
    )

    assert isolated_orchestrator.fee_calculator.taker_fee_rate < DEFAULT_TAKER_FEE


@pytest.mark.asyncio
async def test_configure_fee_calculator_uses_live_rates(
    isolated_orchestrator: Orchestrator,
) -> None:
    """F7: Live commission rates from API override hardcoded defaults."""
    from decimal import Decimal

    isolated_orchestrator.market_data.fetch_commission_rate = AsyncMock(
        return_value={
            "maker": Decimal("0.00016"),
            "taker": Decimal("0.00040"),
        }
    )
    await isolated_orchestrator._configure_fee_calculator(
        [SimpleNamespace(asset="USDT", wallet_balance=100)]
    )

    assert isolated_orchestrator.fee_calculator.maker_fee_rate == Decimal("0.00016")
    assert isolated_orchestrator.fee_calculator.taker_fee_rate == Decimal("0.00040")


@pytest.mark.asyncio
async def test_configure_fee_calculator_falls_back_on_api_error(
    isolated_orchestrator: Orchestrator,
) -> None:
    """F7: If commission rate API fails, use hardcoded VIP-0 defaults safely."""
    from decimal import Decimal
    from src.execution.fee_calculator import DEFAULT_MAKER_FEE

    isolated_orchestrator.market_data.fetch_commission_rate = AsyncMock(
        side_effect=Exception("API timeout"),
    )
    await isolated_orchestrator._configure_fee_calculator(
        [SimpleNamespace(asset="USDT", wallet_balance=100)]
    )

    assert isolated_orchestrator.fee_calculator.maker_fee_rate == DEFAULT_MAKER_FEE
    assert isolated_orchestrator.fee_calculator.taker_fee_rate == DEFAULT_TAKER_FEE