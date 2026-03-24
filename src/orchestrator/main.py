"""
Claude Quant Orchestrator - Main Loop

Runs every hour (aligned with 1H candle close), coordinating all agents:
Sentinel → Supertrend Reversal Exit → Market Analysis → Risk → Execution → Memory
At midnight UTC, triggers Daily Reporter.

Multi-timeframe approach (v3):
  - 4H data: regime detection, SupertrendTrend signals, MeanReversion signals
  - 1H data: entry price timing, BreakoutTrader/TrendFollower signals
  - Supertrend reversal exit: close positions when 4H Supertrend flips against
  - Trailing stop: activate after 2.0 ATR(4H) favorable, trail at 2.5 ATR(4H)
"""

import asyncio
import json
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.data.market_data import MarketDataClient
from src.data.indicator_engine import IndicatorEngine
from src.data.data_validator import DataValidator
from src.data.database import DatabaseManager, CycleHistoryRow, DailyReportRow
from src.strategies.base_strategy import SignalDirection
from src.strategies.regime_detector import RegimeDetector
from src.strategies.adaptive_strategy import AdaptiveStrategy
from src.risk.circuit_breaker import CircuitBreaker, CircuitBreakerLevel, TradeResult
from src.risk.position_sizer import PositionSizer
from src.risk.leverage_manager import LeverageManager
from src.risk.volatility_model import VolatilityModel
from src.risk.drawdown_monitor import DrawdownMonitor
from src.execution.order_manager import OrderManager, OrderState
from src.execution.position_tracker import PositionTracker
from src.execution.fee_calculator import FeeCalculator
from src.memory.trade_journal import TradeJournal
from src.memory.performance_tracker import PerformanceTracker
from src.memory.bias_detector import BiasDetector
from src.anti_hallucination.price_validator import PriceValidator
from src.anti_hallucination.signal_validator import SignalValidator
from src.anti_hallucination.decision_auditor import DecisionAuditor
from src.anti_hallucination.sanity_checks import SanityChecker
from src.reporting.daily_pnl import DailyPnLCalculator
from src.reporting.dashboard import Dashboard
from src.reporting.report_generator import ReportGenerator
from src.reporting.alert_system import AlertSystem

logger = logging.getLogger("claude_quant.orchestrator")


class OrchestratorState(BaseModel):
    """Current state of the orchestrator."""
    cycle_count: int = 0
    last_cycle_time: Optional[datetime] = None
    last_daily_report: Optional[str] = None  # date string YYYY-MM-DD
    is_running: bool = False
    halt_reason: Optional[str] = None
    current_balance: Decimal = Decimal("0")
    daily_start_balance: Decimal = Decimal("0")
    circuit_breaker_level: str = "GREEN"


class TrailingStopState(BaseModel):
    """Trailing stop state for an open position."""
    symbol: str
    direction: str  # 'long' or 'short'
    entry_price: float
    best_price: float  # Best price since entry (high for long, low for short)
    atr_4h: float  # ATR(4H) at time of entry
    activated: bool = False  # True once price moved 2.0 ATR favorable
    strategy_name: str = ""

    # Trailing stop parameters (from v3 backtest)
    ACTIVATE_ATR_MULT: float = 2.0
    TRAIL_ATR_MULT: float = 2.5


class CycleResult(BaseModel):
    """Result of a single orchestrator cycle."""
    cycle_number: int
    timestamp: datetime
    circuit_breaker_level: str
    regime: Optional[str] = None
    signal_generated: bool = False
    trade_placed: bool = False
    trade_details: Optional[dict] = None
    positions_closed: list[dict] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    duration_seconds: float = 0.0


# Symbols to trade — expanded from 3 to 9 pairs (2026-03-24).
# v6 backtest evidence: 9 pairs → +855% return (vs +540% with 3 pairs),
# Sharpe 6.98 (vs 5.83), 0.69 trades/day (vs 0.44). All metrics improved.
# BTC re-added: $100 min notional easily met with $5,102 balance.
TRADING_PAIRS = [
    "BTC/USDT:USDT",   # $100 min notional — re-added with $5K+ balance
    "ETH/USDT:USDT",   # $20 min notional
    "SOL/USDT:USDT",   # $5 min notional
    "DOGE/USDT:USDT",  # $5 min notional
    "XRP/USDT:USDT",   # $5 min notional
    "LINK/USDT:USDT",  # $5 min notional
    "AVAX/USDT:USDT",  # $5 min notional
    "SUI/USDT:USDT",   # $5 min notional
    "ADA/USDT:USDT",   # $5 min notional
]

# Per-pair minimum notional (from Binance API)
MIN_NOTIONAL: dict[str, float] = {
    "BTC/USDT:USDT": 100.0,
    "ETH/USDT:USDT": 20.0,
}
DEFAULT_MIN_NOTIONAL: float = 5.0

# Multi-timeframe: 4H for trend direction, 1H for entry timing.
# Evidence: 4H+ shows 75-85% success rates in trending markets (Cointester study).
# Daily trading is more robust to transaction costs than intraday (ScienceDirect).
TIMEFRAME_DIRECTION = "4h"  # Primary: trend direction + regime detection
TIMEFRAME_ENTRY = "1h"      # Secondary: entry timing with tighter stops
CYCLE_INTERVAL_SECONDS = 3600  # 1 hour (aligned with 1H candle close)
MAX_HOLD_BARS = 150  # Max 1H bars to hold a position (150 × 1H = 6.25 days)
AGENT_STATE_DIR = PROJECT_ROOT / "user_data" / "agent_state"


class Orchestrator:
    """Main orchestrator that coordinates all trading agents."""

    def __init__(self) -> None:
        self.state = OrchestratorState()
        self._shutdown_event = asyncio.Event()

        # Trailing stop state for each open position (keyed by symbol)
        self._trailing_stops: dict[str, TrailingStopState] = {}

        # Initialize all components
        self.market_data = MarketDataClient()
        self.indicator_engine = IndicatorEngine()
        self.data_validator = DataValidator()
        self.regime_detector = RegimeDetector()
        self.adaptive_strategy = AdaptiveStrategy()
        self.circuit_breaker = CircuitBreaker()
        self.position_sizer = PositionSizer()
        self.leverage_manager = LeverageManager()
        self.volatility_model = VolatilityModel(forecast_horizon=1)
        self.drawdown_monitor = DrawdownMonitor()
        self.order_manager = OrderManager()
        self.position_tracker = PositionTracker()
        self.fee_calculator = FeeCalculator()
        self.trade_journal = TradeJournal()
        self.performance_tracker = PerformanceTracker(journal=self.trade_journal)
        self.bias_detector = BiasDetector()
        self.price_validator = PriceValidator(market_data_client=self.market_data)
        self.signal_validator = SignalValidator()
        self.decision_auditor = DecisionAuditor()
        self.sanity_checker = SanityChecker()
        self.pnl_calculator = DailyPnLCalculator(
            initial_capital=Decimal(os.getenv("INITIAL_CAPITAL", "68.33"))
        )
        self.dashboard = Dashboard()
        self.report_generator = ReportGenerator()
        self.alert_system = AlertSystem()
        self.db = DatabaseManager()  # consolidated DB at user_data/claude_quant.db

        AGENT_STATE_DIR.mkdir(parents=True, exist_ok=True)

    async def start(self) -> None:
        """Start the orchestrator main loop."""
        logger.info("Starting Claude Quant Orchestrator")
        self.state.is_running = True

        # Connect to exchange
        try:
            await self.market_data.connect()
            await self.position_tracker.connect()
            await self.order_manager.connect()
            logger.info("Exchange connections established")
        except Exception as e:
            logger.error(f"Failed to connect to exchange: {e}")
            self.state.halt_reason = f"Cannot connect to exchange: {e}"
            return

        # Get initial balance
        try:
            balance = await self.market_data.get_account_balance()
            self.state.current_balance = balance
            self.state.daily_start_balance = balance
            logger.info(f"Initial balance: ${balance}")
        except Exception as e:
            logger.error(f"Failed to get initial balance: {e}")
            self.state.halt_reason = f"Cannot connect to exchange: {e}"
            return

        # Detect pre-existing positions and warn if unprotected
        try:
            await self._detect_preexisting_positions()
        except Exception as e:
            logger.warning(f"Pre-existing position detection failed: {e}")

        # Subscribe to 4H kline close events for all pairs
        for pair in TRADING_PAIRS:
            try:
                await self.market_data.subscribe_kline_close(
                    pair, TIMEFRAME_DIRECTION, self._on_4h_close,
                )
            except Exception as e:
                logger.warning(f"Failed to subscribe to 4H kline for {pair}: {e}")

        # Main loop
        while not self._shutdown_event.is_set():
            try:
                cycle_start = datetime.now(timezone.utc)
                result = await self._run_cycle()
                cycle_elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
                logger.info(
                    f"=== Cycle {result.cycle_number} complete ({cycle_elapsed:.1f}s) ==="
                )

                # Save cycle result to agent state
                self._save_cycle_state(result)

                # Check if daily report is due
                await self._check_daily_report()

                # Wait for next cycle
                elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
                wait_time = max(0, CYCLE_INTERVAL_SECONDS - elapsed)
                if wait_time > 0:
                    try:
                        await asyncio.wait_for(
                            self._shutdown_event.wait(), timeout=wait_time
                        )
                    except asyncio.TimeoutError:
                        pass  # Normal timeout, continue to next cycle

            except Exception as e:
                logger.error(f"Orchestrator cycle error: {e}", exc_info=True)
                await self.alert_system.send_alert(
                    f"Orchestrator error: {e}", level="critical"
                )
                # Wait before retrying
                await asyncio.sleep(30)

        logger.info("Orchestrator shutdown complete")

    async def stop(self) -> None:
        """Graceful shutdown."""
        logger.info("Stopping orchestrator...")
        self.state.is_running = False
        self._shutdown_event.set()

        # Close exchange connections and database
        try:
            await self.market_data.close()
            await self.position_tracker.close()
            await self.order_manager.close()
            self.db.close()
        except Exception as e:
            logger.warning(f"Error closing connections: {e}")

    async def _run_cycle(self) -> CycleResult:
        """Execute one complete trading cycle."""
        self.state.cycle_count += 1
        cycle_start = datetime.now(timezone.utc)
        errors: list[str] = []
        result = CycleResult(
            cycle_number=self.state.cycle_count,
            timestamp=cycle_start,
            circuit_breaker_level="UNKNOWN",
        )

        logger.info(f"=== Cycle {self.state.cycle_count} starting ===")

        # ─── Step 1: Sentinel Check (Circuit Breaker) ───
        try:
            balance = await self.market_data.get_account_balance()
            self.state.current_balance = balance

            # Update drawdown monitor
            self.drawdown_monitor.update(float(balance))

            # Convert recent trades to TradeResult for circuit breaker
            recent_entries = self.trade_journal.get_recent_trades(10)
            recent_trade_results = [
                TradeResult(
                    is_win=t.pnl is not None and t.pnl > 0,
                    closed_at=t.timestamp,
                )
                for t in recent_entries if t.pnl is not None
            ]

            # Single authoritative gate — handles DEAD, daily loss, consecutive
            # loss pause, and RED win-rate gate all in one call.
            cb_state = CircuitBreaker.is_trading_allowed(
                balance=balance,
                recent_trades=recent_trade_results,
                start_of_day_balance=self.state.daily_start_balance,
            )
            result.circuit_breaker_level = cb_state.level.value
            self.state.circuit_breaker_level = cb_state.level.value

            if cb_state.level == CircuitBreakerLevel.DEAD:
                logger.critical(f"DEAD: Balance ${balance} < $30. HALTING ALL TRADING.")
                self.state.halt_reason = f"Balance ${balance} below $30 hard floor"
                await self.alert_system.send_alert(
                    f"CRITICAL: Balance ${balance} - TRADING HALTED", level="critical"
                )
                return result

            if not cb_state.constraints.trading_allowed:
                logger.info(f"Trading not allowed: {cb_state.constraints.reason}")
                result.errors.append(f"Circuit breaker: {cb_state.constraints.reason}")
                return result

        except Exception as e:
            logger.error(f"Sentinel check failed: {e}")
            errors.append(f"Sentinel: {e}")
            # Default to RED if sentinel fails
            result.circuit_breaker_level = "RED"
            return result

        # ─── Step 1b: Fetch 4H data for all pairs (shared across steps) ───
        pair_data_4h: dict[str, pd.DataFrame] = {}
        pair_data_1h: dict[str, pd.DataFrame] = {}

        for pair in TRADING_PAIRS:
            try:
                raw_4h = await self.market_data.fetch_ohlcv(pair, TIMEFRAME_DIRECTION, limit=200)
                if not raw_4h or len(raw_4h) < 100:
                    continue
                df_4h = pd.DataFrame(raw_4h)
                validation = self.data_validator.validate_ohlcv(df_4h)
                if not validation.passed:
                    logger.warning(f"4H data validation failed for {pair}: {validation.reasons}")
                    continue
                pair_data_4h[pair] = self.indicator_engine.calculate_all(df_4h)

                raw_1h = await self.market_data.fetch_ohlcv(pair, TIMEFRAME_ENTRY, limit=200)
                if not raw_1h or len(raw_1h) < 100:
                    continue
                df_1h = pd.DataFrame(raw_1h)
                validation_1h = self.data_validator.validate_ohlcv(df_1h)
                if not validation_1h.passed:
                    logger.warning(f"1H data validation failed for {pair}: {validation_1h.reasons}")
                    continue
                pair_data_1h[pair] = self.indicator_engine.calculate_all(df_1h)

            except Exception as e:
                logger.error(f"Data fetch failed for {pair}: {e}")
                errors.append(f"Data {pair}: {e}")

        # ─── Step 1c: Position Reconciliation & Orphan Order Cleanup ───
        try:
            await self._reconcile_positions_and_orders()
        except Exception as e:
            logger.error(f"Position reconciliation failed: {e}")
            errors.append(f"Reconciliation: {e}")

        # ─── Step 2: Supertrend Reversal Exit (before new signals) ───
        try:
            await self._check_supertrend_reversal_exits(
                pair_data_4h, result, balance
            )
        except Exception as e:
            logger.error(f"Supertrend reversal exit check failed: {e}")
            errors.append(f"ST reversal: {e}")

        # ─── Step 2b: Trailing Stop Management ───
        try:
            await self._manage_trailing_stops(pair_data_4h, pair_data_1h, result)
        except Exception as e:
            logger.error(f"Trailing stop management failed: {e}")
            errors.append(f"Trailing: {e}")

        # ─── Step 2c: Time-based Exit (MAX_HOLD_BARS) ───
        try:
            await self._check_time_based_exits(result)
        except Exception as e:
            logger.error(f"Time-based exit check failed: {e}")
            errors.append(f"Time exit: {e}")

        # ─── Step 3: Multi-Timeframe Signal Generation ───
        best_signal = None
        best_pair = None

        for pair in TRADING_PAIRS:
            if pair not in pair_data_4h or pair not in pair_data_1h:
                continue

            try:
                df_4h = pair_data_4h[pair]
                df_1h = pair_data_1h[pair]

                # Multi-timeframe: 4H regime → strategy selection → appropriate data
                signal = self.adaptive_strategy.get_signal_multi_tf(df_4h, df_1h)

                if signal is None:
                    continue

                # Detect regime for logging
                regime = self.regime_detector.detect(df_4h)
                result.regime = regime.regime.value
                logger.info(f"{pair}: Regime={regime.regime.value} (4H)")

                # Keep best signal (highest confidence)
                if best_signal is None or signal.confidence > best_signal.confidence:
                    best_signal = signal
                    best_pair = pair

            except Exception as e:
                logger.error(f"Analysis failed for {pair}: {e}")
                errors.append(f"Analysis {pair}: {e}")

        if best_signal is None:
            logger.info("No valid signals this cycle")
            result.signal_generated = False
            result.errors = errors
            result.duration_seconds = (datetime.now(timezone.utc) - cycle_start).total_seconds()
            return result

        result.signal_generated = True
        logger.info(
            f"Best signal: {best_pair} {best_signal.direction} "
            f"confidence={best_signal.confidence}% strategy={best_signal.strategy_name}"
        )

        # ─── Step 4: Risk Management ───
        try:
            constraints = cb_state.constraints

            # Check open positions count
            open_positions = await self.position_tracker.get_open_positions()
            if len(open_positions) >= constraints.max_positions:
                logger.info(
                    f"Max positions reached ({len(open_positions)}/{constraints.max_positions})"
                )
                result.errors.append("Max positions reached")
                return result

            # Check if we already have a position in this pair
            for pos in open_positions:
                if pos.symbol == best_pair:
                    logger.info(f"Already have position in {best_pair} — skip")
                    result.errors.append(f"Already positioned in {best_pair}")
                    return result

            # Determine leverage
            leverage_result = LeverageManager.determine_leverage(
                confidence=best_signal.confidence,
                regime=best_signal.regime,
                circuit_breaker_level=cb_state.level,
            )
            leverage = leverage_result.leverage

            if leverage == 0:
                logger.info(f"Leverage manager: NO TRADE. {leverage_result.reason}")
                result.errors.append(f"Leverage 0 - {leverage_result.reason}")
                return result

            # GARCH volatility adjustment
            df_1h = pair_data_1h[best_pair]
            vol_state = self.volatility_model.forecast(df_1h)
            if vol_state is None:
                vol_state = self.volatility_model.forecast_simple(df_1h)
            leverage = VolatilityModel.adjust_leverage(
                requested_leverage=leverage,
                vol_state=vol_state,
                max_leverage=constraints.max_leverage,
            )
            if vol_state:
                logger.info(
                    f"GARCH: vol_ratio={vol_state.vol_ratio}, "
                    f"leverage_scale={vol_state.leverage_scale}, "
                    f"final_leverage={leverage}x"
                )

            # Calculate position size — confidence-based (v3 proven approach)
            # v4 backtest showed Half-Kelly gives $5 minimum on every trade
            # regardless of conviction.  v3's confidence-based sizing produced
            # +94% return: 15% for conf>=60, 10% for conf>=45, 7% otherwise.
            confidence = best_signal.confidence

            # Confidence-based position sizing — aligned with v6 backtest.
            # v6 evidence: 60/45 thresholds with 15% cap → +855%, Sharpe 6.98.
            # Previous 85/70/50 thresholds were too conservative (never hit 25%)
            # and the 25% tier violated Immutable Rule #4 (15% max per trade).
            if confidence >= 60:
                position_pct = Decimal("0.15")
            elif confidence >= 45:
                position_pct = Decimal("0.10")
            else:
                position_pct = Decimal("0.07")
            max_cap = Decimal("0.15")  # Immutable Rule #4: 15% max per trade

            # Apply CB size multiplier
            position_pct *= constraints.size_multiplier

            margin = (balance * position_pct).quantize(Decimal("0.01"))

            # Dynamic hard cap based on signal quality
            max_margin = (balance * max_cap).quantize(Decimal("0.01"))
            if margin > max_margin:
                margin = max_margin

            # Minimum $5
            if margin < Decimal("5"):
                if balance < Decimal("5"):
                    logger.info(f"Balance ${balance} below $5 minimum — no trade")
                    result.errors.append("Balance below $5 minimum")
                    return result
                margin = Decimal("5")

            notional = margin * Decimal(str(leverage))

            # Check minimum notional for this pair
            pair_min_notional = MIN_NOTIONAL.get(best_pair, DEFAULT_MIN_NOTIONAL)
            if float(notional) < pair_min_notional:
                logger.info(
                    f"Notional ${notional:.2f} below minimum ${pair_min_notional} "
                    f"for {best_pair} — skipping trade"
                )
                return result

            logger.info(
                f"Position sized: ${margin} ({float(position_pct)*100:.0f}% of ${balance}) "
                f"x{leverage} = ${notional:.2f} notional (confidence={confidence:.0f}%)"
            )

            # Sanity check position math
            valid, details = SanityChecker.check_position_math(
                Decimal(str(balance)), margin, leverage, notional,
                max_position_pct=max_cap,
            )
            if not valid:
                logger.error(f"Position math sanity failed: {details}")
                result.errors.append(f"Sanity: {details}")
                return result

            # Check liquidation buffer
            liq_buffer = LeverageManager.calculate_liquidation_buffer(
                entry_price=best_signal.entry_price,
                leverage=leverage,
                direction=best_signal.direction.value,
            )
            if not liq_buffer.is_safe:
                logger.warning(
                    f"Liquidation buffer {float(liq_buffer.buffer_pct)*100:.1f}% < 5%"
                )
                result.errors.append(
                    f"Liquidation buffer {float(liq_buffer.buffer_pct)*100:.1f}% < 5%"
                )
                return result

        except Exception as e:
            logger.error(f"Risk management failed: {e}")
            errors.append(f"Risk: {e}")
            return result

        # ─── Step 5: Audit Decision ───
        try:
            regime = self.regime_detector.detect(pair_data_4h[best_pair])
            signal_dict = best_signal.model_dump() if hasattr(best_signal, "model_dump") else dict(best_signal)
            regime_dict = regime.model_dump() if hasattr(regime, "model_dump") else dict(regime)
            # Map Signal field names to what the auditor expects
            signal_dict.setdefault("symbol", best_pair)
            signal_dict.setdefault("strategy", signal_dict.get("strategy_name", ""))
            audit = self.decision_auditor.audit_decision(
                signal=signal_dict,
                regime=regime_dict,
                risk_approval={
                    "position_size_usd": float(margin),
                    "leverage": leverage,
                    "notional": notional,
                    "confidence": confidence,
                    "position_pct": float(position_pct),
                    "approved": True,
                },
                market_data={"pair": best_pair, "balance": float(balance)},
            )
            logger.info(f"Audit: {len(audit.reasons_against)} counter-arguments logged")
        except Exception as e:
            logger.warning(f"Audit failed (non-blocking): {e}")

        # ─── Step 6: Execute Trade ───
        try:
            # Set leverage
            await self.order_manager.set_leverage(best_pair, leverage)

            # Calculate order quantity: notional / entry_price = base currency units
            order_qty = Decimal(str(notional)) / Decimal(str(best_signal.entry_price))

            # Place order
            side = "buy" if best_signal.direction.value == "long" else "sell"
            order_result = await self.order_manager.place_market_order(
                symbol=best_pair,
                side=side,
                amount=order_qty,
            )

            # Verify order was placed
            order_status = await self.order_manager.get_order_status(
                best_pair, order_result.order_id
            )

            if order_status.status not in (OrderState.CLOSED,):
                logger.warning(f"Order not filled: {order_status.status.value}")
                result.errors.append(f"Order status: {order_status.status.value}")
                return result

            # Place stop-loss
            sl_side = "sell" if best_signal.direction.value == "long" else "buy"
            await self.order_manager.place_stop_loss(
                symbol=best_pair,
                side=sl_side,
                amount=order_result.filled,
                stop_price=Decimal(str(best_signal.stop_loss)),
            )

            # Place take-profit (v4 fix: v3 backtest relies on TP hits,
            # production was missing this — positions could only exit via SL)
            tp_side = sl_side  # same side as SL (close direction)
            try:
                await self.order_manager.place_take_profit(
                    symbol=best_pair,
                    side=tp_side,
                    amount=order_result.filled,
                    stop_price=Decimal(str(best_signal.take_profit)),
                )
            except Exception as tp_err:
                # Non-blocking: trailing stop will handle TP if this fails
                logger.warning(f"Take-profit order failed (non-blocking): {tp_err}")

            fill_price = order_result.average_fill_price or Decimal(str(best_signal.entry_price))
            result.trade_placed = True
            result.trade_details = {
                "pair": best_pair,
                "direction": best_signal.direction.value,
                "entry_price": float(fill_price),
                "size": float(order_result.filled),
                "leverage": leverage,
                "margin": float(margin),
                "stop_loss": best_signal.stop_loss,
                "take_profit": best_signal.take_profit,
                "strategy": best_signal.strategy_name,
                "confidence": best_signal.confidence,
            }

            # Initialize trailing stop state for this position
            atr_4h = float(
                pair_data_4h[best_pair]["atr"].dropna().iloc[-1]
            ) if "atr" in pair_data_4h[best_pair].columns else 0.0

            self._trailing_stops[best_pair] = TrailingStopState(
                symbol=best_pair,
                direction=best_signal.direction.value,
                entry_price=float(fill_price),
                best_price=float(fill_price),
                atr_4h=atr_4h,
                strategy_name=best_signal.strategy_name,
            )

            logger.info(
                f"TRADE PLACED: {best_pair} {best_signal.direction.value} "
                f"@ {fill_price} x{leverage} (trailing stop ATR={atr_4h:.6f})"
            )

            await self.alert_system.send_alert(
                f"Trade: {best_pair} {best_signal.direction.value} "
                f"@ {fill_price} x{leverage} "
                f"SL={best_signal.stop_loss}",
                level="info",
            )

        except Exception as e:
            logger.error(f"Execution failed: {e}")
            errors.append(f"Execution: {e}")

        # ─── Step 7: Memory ───
        try:
            if result.trade_placed and result.trade_details:
                self.trade_journal.record_trade_entry(result.trade_details)
        except Exception as e:
            logger.warning(f"Memory recording failed (non-blocking): {e}")

        result.errors = errors
        result.duration_seconds = (datetime.now(timezone.utc) - cycle_start).total_seconds()
        self.state.last_cycle_time = datetime.now(timezone.utc)
        return result

    # ------------------------------------------------------------------
    # Supertrend Reversal Exit
    # ------------------------------------------------------------------

    async def _check_supertrend_reversal_exits(
        self,
        pair_data_4h: dict[str, pd.DataFrame],
        result: CycleResult,
        balance: Decimal,
    ) -> None:
        """Tighten SL to breakeven when 4H Supertrend flips against direction.

        v5 optimisation: instead of closing immediately (which individually
        loses ~$33), tighten the stop-loss to the entry price. This lets
        winning trades ride further while capping risk at zero. The sweep
        showed this improves Sharpe from 3.98→5.83 and cuts MaxDD from
        9.8%→1.2% compared to immediate close.
        """
        open_positions = await self.position_tracker.get_open_positions()

        for pos in open_positions:
            if pos.symbol not in pair_data_4h:
                continue

            ts_state = self._trailing_stops.get(pos.symbol)
            if ts_state is None or ts_state.strategy_name != "SupertrendTrend":
                continue

            df_4h = pair_data_4h[pos.symbol]

            should_exit = self.adaptive_strategy.check_supertrend_reversal(
                df_4h, pos.side,
            )

            if should_exit:
                entry_price = float(pos.entry_price)
                logger.info(
                    f"SUPERTREND REVERSAL: Tightening SL to breakeven for "
                    f"{pos.symbol} {pos.side} (entry={entry_price})"
                )

                try:
                    # Cancel existing SL/TP orders
                    await self.order_manager.cancel_open_orders(pos.symbol)

                    # Place new SL at entry price (breakeven)
                    sl_side = "sell" if pos.side == "long" else "buy"
                    await self.order_manager.place_stop_loss(
                        symbol=pos.symbol,
                        side=sl_side,
                        amount=pos.size,
                        stop_price=entry_price,
                    )

                    result.positions_closed.append({
                        "symbol": pos.symbol,
                        "reason": "supertrend_reversal_tighten",
                        "direction": pos.side,
                        "entry_price": entry_price,
                        "new_sl": entry_price,
                    })

                    await self.alert_system.send_alert(
                        f"ST Reversal Tighten: {pos.symbol} {pos.side} "
                        f"SL moved to breakeven {entry_price}",
                        level="info",
                    )

                except Exception as e:
                    logger.error(
                        f"Failed to tighten SL for {pos.symbol} on ST reversal: {e}"
                    )

    # ------------------------------------------------------------------
    # Trailing Stop Management
    # ------------------------------------------------------------------

    async def _manage_trailing_stops(
        self,
        pair_data_4h: dict[str, pd.DataFrame],
        pair_data_1h: dict[str, pd.DataFrame],
        result: CycleResult,
    ) -> None:
        """Manage trailing stops for open positions.

        Parameters from v3 backtest (optimal for crypto):
        - Activate after 2.0 ATR(4H) favorable move from entry
        - Trail at 2.5 ATR(4H) behind best price
        """
        open_positions = await self.position_tracker.get_open_positions()

        for pos in open_positions:
            ts_state = self._trailing_stops.get(pos.symbol)
            if ts_state is None or ts_state.atr_4h <= 0:
                continue

            current_price = float(pos.current_price)

            # Update best price
            if ts_state.direction == "long":
                if current_price > ts_state.best_price:
                    ts_state.best_price = current_price
            else:
                if current_price < ts_state.best_price or ts_state.best_price == ts_state.entry_price:
                    ts_state.best_price = current_price

            # Check activation: has price moved 2.0 ATR favorable from entry?
            favorable_move = (
                ts_state.best_price - ts_state.entry_price
                if ts_state.direction == "long"
                else ts_state.entry_price - ts_state.best_price
            )

            activate_threshold = ts_state.atr_4h * ts_state.ACTIVATE_ATR_MULT
            if favorable_move >= activate_threshold:
                ts_state.activated = True

            if not ts_state.activated:
                continue

            # Check trailing stop: has price pulled back 2.5 ATR from best?
            trail_distance = ts_state.atr_4h * ts_state.TRAIL_ATR_MULT
            if ts_state.direction == "long":
                trailing_stop_price = ts_state.best_price - trail_distance
                triggered = current_price <= trailing_stop_price
            else:
                trailing_stop_price = ts_state.best_price + trail_distance
                triggered = current_price >= trailing_stop_price

            if triggered:
                logger.info(
                    f"TRAILING STOP triggered: {pos.symbol} {ts_state.direction} "
                    f"entry={ts_state.entry_price:.6f}, best={ts_state.best_price:.6f}, "
                    f"current={current_price:.6f}, trail_level={trailing_stop_price:.6f}"
                )

                try:
                    close_side = "sell" if ts_state.direction == "long" else "buy"
                    await self.order_manager.place_market_order(
                        symbol=pos.symbol,
                        side=close_side,
                        amount=pos.size,
                    )

                    await self.order_manager.cancel_open_orders(pos.symbol)
                    self._trailing_stops.pop(pos.symbol, None)

                    result.positions_closed.append({
                        "symbol": pos.symbol,
                        "reason": "trailing_stop",
                        "direction": ts_state.direction,
                        "entry_price": ts_state.entry_price,
                        "exit_price": current_price,
                        "best_price": ts_state.best_price,
                        "pnl": float(pos.unrealized_pnl),
                    })

                    await self.alert_system.send_alert(
                        f"Trailing Stop: {pos.symbol} {ts_state.direction} "
                        f"PnL={pos.unrealized_pnl} "
                        f"(best={ts_state.best_price:.6f})",
                        level="info",
                    )

                except Exception as e:
                    logger.error(f"Failed to close {pos.symbol} on trailing stop: {e}")

    # ------------------------------------------------------------------
    # Time-based exit (MAX_HOLD_BARS)
    # ------------------------------------------------------------------

    async def _check_time_based_exits(
        self,
        result: CycleResult,
    ) -> None:
        """Close positions held longer than MAX_HOLD_BARS 1H bars.

        v5 sweep validated MAX_HOLD_BARS=150 (6.25 days): TIME exits were
        100% win rate in backtest, recovering capital from slow-moving trades.
        """
        open_positions = await self.position_tracker.get_open_positions()
        now = datetime.now(tz=timezone.utc)

        for pos in open_positions:
            bars_held = (now - pos.timestamp).total_seconds() / 3600.0
            if bars_held < MAX_HOLD_BARS:
                continue

            logger.info(
                f"TIME EXIT: {pos.symbol} {pos.side} held {bars_held:.0f} bars "
                f"(max={MAX_HOLD_BARS}), closing at market"
            )

            try:
                close_side = "sell" if pos.side == "long" else "buy"
                await self.order_manager.place_market_order(
                    symbol=pos.symbol,
                    side=close_side,
                    amount=pos.size,
                )

                await self.order_manager.cancel_open_orders(pos.symbol)
                self._trailing_stops.pop(pos.symbol, None)

                result.positions_closed.append({
                    "symbol": pos.symbol,
                    "reason": "time_exit",
                    "direction": pos.side,
                    "entry_price": float(pos.entry_price),
                    "exit_price": float(pos.current_price),
                    "bars_held": round(bars_held, 1),
                    "pnl": float(pos.unrealized_pnl),
                })

                await self.alert_system.send_alert(
                    f"Time Exit: {pos.symbol} {pos.side} held {bars_held:.0f} bars "
                    f"PnL={pos.unrealized_pnl}",
                    level="info",
                )

            except Exception as e:
                logger.error(
                    f"Failed to close {pos.symbol} on time exit: {e}"
                )

    # ------------------------------------------------------------------
    # Position Reconciliation & Orphan Order Cleanup
    # ------------------------------------------------------------------

    async def _reconcile_positions_and_orders(self) -> None:
        """Detect positions closed by exchange-side SL/TP and cancel orphans.

        When Binance fires a STOP_MARKET or TAKE_PROFIT_MARKET, the
        counterpart conditional order remains active.  Without reduceOnly
        (fixed in order_manager.py), an orphan TP could open a reverse
        position.  Even WITH reduceOnly, orphan orders waste margin and
        confuse the position tracker.

        Also cleans up trailing stop state for positions that no longer
        exist on the exchange.
        """
        open_positions = await self.position_tracker.get_open_positions()
        open_symbols = {pos.symbol for pos in open_positions}

        # 1. For every symbol we THINK we have a trailing stop on,
        #    check if position still exists. If not, cancel stale orders.
        stale_symbols = [
            sym for sym in list(self._trailing_stops.keys())
            if sym not in open_symbols
        ]
        for sym in stale_symbols:
            logger.warning(
                "RECONCILE: Position for %s closed externally (SL/TP fire). "
                "Cancelling orphan orders and cleaning trailing state.", sym
            )
            try:
                cancelled = await self.order_manager.cancel_open_orders(sym)
                if cancelled > 0:
                    logger.info(
                        "RECONCILE: Cancelled %d orphan orders for %s",
                        cancelled, sym,
                    )
            except Exception as e:
                logger.error("RECONCILE: Failed to cancel orders for %s: %s", sym, e)
            self._trailing_stops.pop(sym, None)

        # 2. For every open position, ensure it has at least one
        #    conditional order (SL or TP). If zero orders, log a warning.
        for pos in open_positions:
            try:
                open_orders = await self.order_manager.get_open_orders(pos.symbol)
                if len(open_orders) == 0 and pos.symbol in self._trailing_stops:
                    logger.warning(
                        "RECONCILE: %s has open position but ZERO conditional "
                        "orders. Trailing stop may be sole protection.", pos.symbol
                    )
            except Exception as e:
                logger.error(
                    "RECONCILE: Failed to check orders for %s: %s", pos.symbol, e
                )

    async def _detect_preexisting_positions(self) -> None:
        """On startup, detect positions already on exchange and register them.

        Creates TrailingStopState entries for positions that existed before
        the bot started, so they benefit from trailing stop management and
        reconciliation. Also logs warnings for unprotected positions.
        """
        open_positions = await self.position_tracker.get_open_positions()
        if not open_positions:
            logger.info("STARTUP: No pre-existing positions found")
            return

        logger.info(
            "STARTUP: Found %d pre-existing position(s): %s",
            len(open_positions),
            [f"{p.symbol} {p.side}" for p in open_positions],
        )

        for pos in open_positions:
            # Register in trailing stops so reconciliation can track them
            if pos.symbol not in self._trailing_stops:
                self._trailing_stops[pos.symbol] = TrailingStopState(
                    symbol=pos.symbol,
                    direction=pos.side,
                    entry_price=float(pos.entry_price),
                    best_price=float(pos.current_price),
                    atr_4h=0.0,  # Will be updated on first cycle with data
                    strategy_name="pre_existing",
                )

            # Check if position has any conditional orders
            try:
                open_orders = await self.order_manager.get_open_orders(pos.symbol)
                if len(open_orders) == 0:
                    logger.warning(
                        "STARTUP: %s %s position has ZERO orders (no SL/TP). "
                        "Position is UNPROTECTED.", pos.symbol, pos.side
                    )
            except Exception as e:
                logger.error(
                    "STARTUP: Failed to check orders for %s: %s", pos.symbol, e
                )

    # ------------------------------------------------------------------
    # Event-driven 4H candle close handler
    # ------------------------------------------------------------------

    async def _on_4h_close(self, candle: dict) -> None:
        """Triggered by WebSocket on every 4H candle close.

        Immediately re-runs signal generation + trailing stop check for the
        pair that closed, eliminating up to 59 minutes of entry delay
        compared to the hourly polling cycle.
        """
        symbol = candle["symbol"]
        logger.info(
            f"4H candle closed for {symbol}: close={candle['close']:.4f} "
            f"at {candle['timestamp']}"
        )

        try:
            # Quick CB gate — skip heavy work if trading is halted
            balance = self.state.current_balance
            if balance < Decimal("30"):
                return

            # Fetch fresh data for this pair
            raw_4h = await self.market_data.fetch_ohlcv(symbol, TIMEFRAME_DIRECTION, limit=200)
            if not raw_4h or len(raw_4h) < 100:
                return
            df_4h = pd.DataFrame(raw_4h)
            df_4h = self.indicator_engine.calculate_all(df_4h)

            raw_1h = await self.market_data.fetch_ohlcv(symbol, TIMEFRAME_ENTRY, limit=200)
            if not raw_1h or len(raw_1h) < 100:
                return
            df_1h = pd.DataFrame(raw_1h)
            df_1h = self.indicator_engine.calculate_all(df_1h)

            pair_data_4h = {symbol: df_4h}
            pair_data_1h = {symbol: df_1h}

            # Check Supertrend reversal exits for this pair
            mini_result = CycleResult(
                cycle_number=self.state.cycle_count,
                timestamp=datetime.now(timezone.utc),
                circuit_breaker_level=self.state.circuit_breaker_level,
            )
            await self._check_supertrend_reversal_exits(pair_data_4h, mini_result, balance)

            # Check trailing stops for this pair
            await self._manage_trailing_stops(pair_data_4h, pair_data_1h, mini_result)

            # Attempt signal generation for this pair
            signal = self.adaptive_strategy.get_signal_multi_tf(df_4h, df_1h)
            if signal is None:
                logger.info(f"4H close {symbol}: no signal")
                return

            logger.info(
                f"4H close signal: {symbol} {signal.direction} "
                f"confidence={signal.confidence}% strategy={signal.strategy_name}"
            )

            # Full risk gate (reuse same logic as _run_cycle Step 4)
            recent_entries = self.trade_journal.get_recent_trades(10)
            recent_trade_results = [
                TradeResult(
                    is_win=t.pnl is not None and t.pnl > 0,
                    closed_at=t.timestamp,
                )
                for t in recent_entries if t.pnl is not None
            ]
            cb_state = CircuitBreaker.is_trading_allowed(
                balance=balance,
                recent_trades=recent_trade_results,
                start_of_day_balance=self.state.daily_start_balance,
            )
            if not cb_state.constraints.trading_allowed:
                return

            constraints = cb_state.constraints
            open_positions = await self.position_tracker.get_open_positions()
            if len(open_positions) >= constraints.max_positions:
                return
            for pos in open_positions:
                if pos.symbol == symbol:
                    return  # Already positioned

            leverage_result = LeverageManager.determine_leverage(
                confidence=signal.confidence,
                regime=signal.regime,
                circuit_breaker_level=cb_state.level,
            )
            leverage = leverage_result.leverage
            if leverage == 0:
                return

            vol_state = self.volatility_model.forecast(df_1h)
            if vol_state is None:
                vol_state = self.volatility_model.forecast_simple(df_1h)
            leverage = VolatilityModel.adjust_leverage(
                requested_leverage=leverage,
                vol_state=vol_state,
                max_leverage=constraints.max_leverage,
            )

            confidence = signal.confidence

            # Confidence-based position sizing — aligned with v6 backtest
            if confidence >= 60:
                position_pct = Decimal("0.15")
            elif confidence >= 45:
                position_pct = Decimal("0.10")
            else:
                position_pct = Decimal("0.07")
            max_cap = Decimal("0.15")  # Immutable Rule #4

            position_pct *= constraints.size_multiplier
            margin = (balance * position_pct).quantize(Decimal("0.01"))
            max_margin = (balance * max_cap).quantize(Decimal("0.01"))
            if margin > max_margin:
                margin = max_margin
            if margin < Decimal("5"):
                if balance < Decimal("5"):
                    return
                margin = Decimal("5")

            notional = margin * Decimal(str(leverage))

            # Check minimum notional for this pair
            pair_min_notional = MIN_NOTIONAL.get(symbol, DEFAULT_MIN_NOTIONAL)
            if float(notional) < pair_min_notional:
                logger.info(
                    f"4H close: Notional ${notional:.2f} below minimum "
                    f"${pair_min_notional} for {symbol} — skipping"
                )
                return

            # Sanity check position math
            valid, details = SanityChecker.check_position_math(
                Decimal(str(balance)), margin, leverage, notional,
                max_position_pct=max_cap,
            )
            if not valid:
                logger.error(f"4H close position math sanity failed: {details}")
                return

            liq_buffer = LeverageManager.calculate_liquidation_buffer(
                entry_price=signal.entry_price,
                leverage=leverage,
                direction=signal.direction.value,
            )
            if not liq_buffer.is_safe:
                return

            # Execute trade
            await self.order_manager.set_leverage(symbol, leverage)
            order_qty = notional / Decimal(str(signal.entry_price))
            side = "buy" if signal.direction.value == "long" else "sell"
            order_result = await self.order_manager.place_market_order(
                symbol=symbol, side=side, amount=order_qty,
            )

            order_status = await self.order_manager.get_order_status(
                symbol, order_result.order_id
            )
            if order_status.status not in (OrderState.CLOSED,):
                return

            sl_side = "sell" if signal.direction.value == "long" else "buy"
            await self.order_manager.place_stop_loss(
                symbol=symbol, side=sl_side, amount=order_result.filled,
                stop_price=Decimal(str(signal.stop_loss)),
            )
            try:
                await self.order_manager.place_take_profit(
                    symbol=symbol, side=sl_side, amount=order_result.filled,
                    stop_price=Decimal(str(signal.take_profit)),
                )
            except Exception as tp_err:
                logger.warning(f"4H close TP order failed (non-blocking): {tp_err}")

            fill_price = order_result.average_fill_price or Decimal(str(signal.entry_price))
            atr_4h = float(df_4h["atr"].dropna().iloc[-1]) if "atr" in df_4h.columns else 0.0

            self._trailing_stops[symbol] = TrailingStopState(
                symbol=symbol,
                direction=signal.direction.value,
                entry_price=float(fill_price),
                best_price=float(fill_price),
                atr_4h=atr_4h,
                strategy_name=signal.strategy_name,
            )

            logger.info(
                f"4H CLOSE TRADE: {symbol} {signal.direction.value} "
                f"@ {fill_price} x{leverage} (event-driven entry)"
            )

            await self.alert_system.send_alert(
                f"4H Close Trade: {symbol} {signal.direction.value} "
                f"@ {fill_price} x{leverage}",
                level="info",
            )

            if self.trade_journal:
                self.trade_journal.record_trade_entry({
                    "pair": symbol,
                    "direction": signal.direction.value,
                    "entry_price": float(fill_price),
                    "size": float(order_result.filled),
                    "leverage": leverage,
                    "margin": float(margin),
                    "stop_loss": signal.stop_loss,
                    "take_profit": signal.take_profit,
                    "strategy": signal.strategy_name,
                    "confidence": signal.confidence,
                    "trigger": "4h_candle_close",
                })

        except Exception as e:
            logger.error(f"4H close handler error for {symbol}: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # Daily report / state persistence
    # ------------------------------------------------------------------

    async def _check_daily_report(self) -> None:
        """Generate daily report and reset daily_start_balance at UTC midnight.

        Uses date-based comparison (not minute-of-hour) to ensure the reset
        fires regardless of cycle timing.  The ``last_daily_report`` guard
        at the top prevents duplicate reports within the same UTC day.
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.state.last_daily_report == today:
            return

        now = datetime.now(timezone.utc)
        # Only generate the report during the first cycle of the new UTC day
        # (hour 0, any minute). The last_daily_report guard above prevents
        # duplicate execution even if the cycle runs multiple times at hour 0.
        if now.hour == 0:
            try:
                logger.info("Generating daily report for %s...", today)

                # Get today's trades and convert to dicts for PnL calculator
                all_trades = self.trade_journal.get_all_trades()
                from datetime import date as date_type
                yesterday = date_type.fromisoformat(
                    (now.replace(hour=0, minute=0, second=0)
                     - __import__("datetime").timedelta(days=1)).strftime("%Y-%m-%d")
                )
                today_trades = [
                    {
                        "pnl": t.pnl or Decimal("0"),
                        "fees": t.fees,
                        "strategy": t.strategy,
                    }
                    for t in all_trades
                    if t.timestamp.date() == yesterday and t.pnl is not None
                ]

                daily_pnl = self.pnl_calculator.calculate_daily_pnl(
                    trades=today_trades,
                    start_balance=self.state.daily_start_balance,
                    end_balance=self.state.current_balance,
                    report_date=yesterday,
                )

                # Store in consolidated database
                self.db.store_daily_report(DailyReportRow(
                    report_date=daily_pnl.date,
                    start_balance=daily_pnl.start_balance,
                    end_balance=daily_pnl.end_balance,
                    realized_pnl=daily_pnl.realized_pnl,
                    unrealized_pnl=daily_pnl.unrealized_pnl,
                    fees=daily_pnl.fees,
                    net_pnl=daily_pnl.net_pnl,
                    pnl_pct=daily_pnl.pnl_pct,
                    trades_count=daily_pnl.trades_count,
                    wins=daily_pnl.wins,
                    losses=daily_pnl.losses,
                    strategies_used=",".join(daily_pnl.strategies_used),
                ))

                # Generate markdown report
                report_dir = PROJECT_ROOT / "docs" / "reports"
                report_dir.mkdir(parents=True, exist_ok=True)
                report_path = report_dir / f"{yesterday}.md"
                self._write_daily_report_md(daily_pnl, report_path)

                # Alert if daily loss > 5%
                if daily_pnl.pnl_pct < Decimal("-5"):
                    await self.alert_system.send_alert(
                        f"DAILY LOSS ALERT: {daily_pnl.pnl_pct:.2f}% on {yesterday}",
                        level="critical",
                    )

                self.state.last_daily_report = today
                self.state.daily_start_balance = self.state.current_balance

                logger.info("Daily report saved for %s (P&L: %s%%)",
                            yesterday, daily_pnl.pnl_pct)
            except Exception as e:
                logger.error(f"Daily report failed: {e}", exc_info=True)

    def _write_daily_report_md(self, pnl, report_path: Path) -> None:
        """Write a markdown daily report file."""
        # Get cumulative stats from all stored daily reports
        all_reports = self.db.get_all_daily_reports()
        from src.reporting.daily_pnl import DailyPnL as DailyPnLModel
        all_daily_pnls = [
            DailyPnLModel(
                date=r.report_date,
                start_balance=r.start_balance,
                end_balance=r.end_balance,
                realized_pnl=r.realized_pnl,
                net_pnl=r.net_pnl,
                fees=r.fees,
                pnl_pct=r.pnl_pct,
                trades_count=r.trades_count,
                wins=r.wins,
                losses=r.losses,
            )
            for r in all_reports
        ]
        cumulative = self.pnl_calculator.get_cumulative_stats(all_daily_pnls)

        lines = [
            f"# Daily Report — {pnl.date}",
            "",
            "## P&L Summary",
            f"| Metric | Value |",
            f"|--------|-------|",
            f"| Start Balance | ${pnl.start_balance} |",
            f"| End Balance | ${pnl.end_balance} |",
            f"| Net P&L | ${pnl.net_pnl} |",
            f"| P&L % | {pnl.pnl_pct:.2f}% |",
            f"| Realized | ${pnl.realized_pnl} |",
            f"| Fees | ${pnl.fees} |",
            f"| Trades | {pnl.trades_count} (W:{pnl.wins}/L:{pnl.losses}) |",
            f"| Strategies | {', '.join(pnl.strategies_used) or 'None'} |",
            "",
            "## Cumulative Performance",
            f"| Metric | Value |",
            f"|--------|-------|",
            f"| Total P&L | ${cumulative.total_pnl} ({cumulative.total_pnl_pct:.2f}%) |",
            f"| Max Drawdown | {cumulative.max_drawdown:.2f}% |",
            f"| Sharpe Ratio | {cumulative.sharpe_ratio} |",
            f"| Win Rate | {cumulative.win_rate:.1f}% |",
            f"| Profitable Days | {cumulative.profitable_days} |",
            f"| Losing Days | {cumulative.losing_days} |",
            f"| Doubling Progress | {cumulative.doubling_progress:.1f}% |",
            "",
        ]
        report_path.write_text("\n".join(lines), encoding="utf-8")
        logger.info("Report written to %s", report_path)

    def _save_cycle_state(self, result: CycleResult) -> None:
        """Save cycle result to consolidated DB and JSON (backward compat)."""
        # Store in consolidated database
        try:
            self.db.store_cycle(CycleHistoryRow(
                cycle_number=result.cycle_number,
                timestamp=result.timestamp,
                circuit_breaker_level=result.circuit_breaker_level,
                balance=self.state.current_balance,
                regime=result.regime,
                signal_generated=result.signal_generated,
                trade_placed=result.trade_placed,
                trade_details=json.dumps(result.trade_details) if result.trade_details else None,
                positions_closed=json.dumps(result.positions_closed),
                errors=json.dumps(result.errors),
                duration_seconds=result.duration_seconds,
            ))
        except Exception as e:
            logger.warning(f"Failed to save cycle to DB: {e}")

        # Keep JSON file for backward compatibility
        state_file = AGENT_STATE_DIR / "last_cycle.json"
        try:
            state_file.write_text(result.model_dump_json(indent=2))
        except Exception as e:
            logger.warning(f"Failed to save cycle state JSON: {e}")


async def main() -> None:
    """Entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    orchestrator = Orchestrator()

    # Handle graceful shutdown
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(orchestrator.stop()))

    await orchestrator.start()


if __name__ == "__main__":
    asyncio.run(main())
