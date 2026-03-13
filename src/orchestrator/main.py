"""
Claude Quant Orchestrator - Main Loop

Runs every 5 minutes, coordinating all agents in sequence:
Sentinel → Market Analyst → Strategy Selector → Risk Manager → Execution Agent → Memory Agent
At midnight UTC, triggers Daily Reporter.
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

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.data.market_data import MarketDataClient
from src.data.indicator_engine import IndicatorEngine
from src.data.data_validator import DataValidator
from src.strategies.regime_detector import RegimeDetector
from src.strategies.adaptive_strategy import AdaptiveStrategy
from src.risk.circuit_breaker import CircuitBreaker, CircuitBreakerLevel
from src.risk.position_sizer import PositionSizer
from src.risk.leverage_manager import LeverageManager
from src.risk.drawdown_monitor import DrawdownMonitor
from src.execution.order_manager import OrderManager
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


class CycleResult(BaseModel):
    """Result of a single orchestrator cycle."""
    cycle_number: int
    timestamp: datetime
    circuit_breaker_level: str
    regime: Optional[str] = None
    signal_generated: bool = False
    trade_placed: bool = False
    trade_details: Optional[dict] = None
    errors: list[str] = Field(default_factory=list)
    duration_seconds: float = 0.0


# Symbols to trade (top Binance Futures pairs)
TRADING_PAIRS = [
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
    "BNB/USDT:USDT",
    "XRP/USDT:USDT",
]

TIMEFRAME = "5m"
CYCLE_INTERVAL_SECONDS = 300  # 5 minutes
AGENT_STATE_DIR = PROJECT_ROOT / "user_data" / "agent_state"


class Orchestrator:
    """Main orchestrator that coordinates all trading agents."""

    def __init__(self) -> None:
        self.state = OrchestratorState()
        self._shutdown_event = asyncio.Event()

        # Initialize all components
        self.market_data = MarketDataClient()
        self.indicator_engine = IndicatorEngine()
        self.data_validator = DataValidator()
        self.regime_detector = RegimeDetector()
        self.adaptive_strategy = AdaptiveStrategy()
        self.circuit_breaker = CircuitBreaker()
        self.position_sizer = PositionSizer()
        self.leverage_manager = LeverageManager()
        self.drawdown_monitor = DrawdownMonitor()
        self.order_manager = OrderManager()
        self.position_tracker = PositionTracker()
        self.fee_calculator = FeeCalculator()
        self.trade_journal = TradeJournal()
        self.performance_tracker = PerformanceTracker()
        self.bias_detector = BiasDetector()
        self.price_validator = PriceValidator()
        self.signal_validator = SignalValidator()
        self.decision_auditor = DecisionAuditor()
        self.sanity_checker = SanityChecker()
        self.pnl_calculator = DailyPnLCalculator()
        self.dashboard = Dashboard()
        self.report_generator = ReportGenerator()
        self.alert_system = AlertSystem()

        AGENT_STATE_DIR.mkdir(parents=True, exist_ok=True)

    async def start(self) -> None:
        """Start the orchestrator main loop."""
        logger.info("Starting Claude Quant Orchestrator")
        self.state.is_running = True

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

        # Main loop
        while not self._shutdown_event.is_set():
            try:
                cycle_start = datetime.now(timezone.utc)
                result = await self._run_cycle()

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
            cb_state = self.circuit_breaker.check_level(float(balance))
            result.circuit_breaker_level = cb_state.level.value
            self.state.circuit_breaker_level = cb_state.level.value

            # Update drawdown monitor
            self.drawdown_monitor.update(float(balance))

            if cb_state.level == CircuitBreakerLevel.DEAD:
                logger.critical(f"DEAD: Balance ${balance} < $30. HALTING ALL TRADING.")
                self.state.halt_reason = f"Balance ${balance} below $30 hard floor"
                await self.alert_system.send_alert(
                    f"CRITICAL: Balance ${balance} - TRADING HALTED", level="critical"
                )
                return result

            # Check daily loss limit
            daily_loss_pct = float(
                (self.state.daily_start_balance - balance) / self.state.daily_start_balance
            ) if self.state.daily_start_balance > 0 else 0
            if daily_loss_pct > 0.10:
                logger.warning(f"Daily loss {daily_loss_pct:.1%} > 10%. Halting until next UTC day.")
                result.errors.append("Daily loss limit exceeded")
                return result

            # Check consecutive losses
            consecutive_losses = self.trade_journal.get_consecutive_losses()
            if consecutive_losses >= 5:
                logger.warning(f"{consecutive_losses} consecutive losses. 2-hour cooldown.")
                result.errors.append(f"{consecutive_losses} consecutive losses - cooldown")
                return result

            if not self.circuit_breaker.is_trading_allowed(
                float(balance), self.trade_journal.get_recent_trades(10)
            ):
                logger.info("Trading not allowed by circuit breaker")
                result.errors.append("Circuit breaker: trading not allowed")
                return result

        except Exception as e:
            logger.error(f"Sentinel check failed: {e}")
            errors.append(f"Sentinel: {e}")
            # Default to RED if sentinel fails
            result.circuit_breaker_level = "RED"
            return result

        # ─── Step 2: Market Analysis ───
        best_signal = None
        best_pair = None

        for pair in TRADING_PAIRS:
            try:
                # Fetch OHLCV data
                df = await self.market_data.fetch_ohlcv(pair, TIMEFRAME, limit=200)
                if df is None or len(df) < 100:
                    continue

                # Validate data
                validation = self.data_validator.validate_ohlcv(df)
                if not validation.valid:
                    logger.warning(f"Data validation failed for {pair}: {validation.issues}")
                    continue

                # Calculate indicators
                df = self.indicator_engine.calculate_all(df)

                # Detect regime
                regime = self.regime_detector.detect(df)
                result.regime = regime.regime.value
                logger.info(f"{pair}: Regime={regime.regime.value}, Confidence={regime.confidence}%")

                # Generate signal
                signal = self.adaptive_strategy.get_signal(df)
                if signal is None:
                    continue

                # Validate signal against raw data
                sig_validation = self.signal_validator.validate_signal(signal, df)
                if not sig_validation.valid:
                    logger.warning(f"Signal validation failed for {pair}: {sig_validation.issues}")
                    continue

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
            f"confidence={best_signal.confidence}%"
        )

        # ─── Step 3: Risk Management ───
        try:
            # Get circuit breaker constraints
            constraints = self.circuit_breaker.get_constraints(float(balance))

            # Check open positions count
            open_positions = await self.position_tracker.get_open_positions()
            if len(open_positions) >= constraints.max_positions:
                logger.info(
                    f"Max positions reached ({len(open_positions)}/{constraints.max_positions})"
                )
                result.errors.append("Max positions reached")
                return result

            # Calculate position size
            win_rate = self.trade_journal.get_win_rate(last_n=50) or 0.5
            rr_ratio = best_signal.calculate_rr_ratio() if hasattr(best_signal, 'calculate_rr_ratio') else 2.0
            position = self.position_sizer.calculate_size(
                balance=float(balance),
                win_rate=win_rate,
                rr_ratio=rr_ratio,
                circuit_breaker_state=cb_state,
            )

            if position.usd_amount <= 0:
                logger.info("Position sizer returned 0 - trade rejected")
                result.errors.append("Position size 0")
                return result

            # Determine leverage
            leverage = self.leverage_manager.determine_leverage(
                confidence=best_signal.confidence,
                regime=best_signal.regime,
                circuit_breaker_level=cb_state.level,
            )

            if leverage == 0:
                logger.info("Leverage manager returned 0 - NO TRADE")
                result.errors.append("Leverage 0 - no trade")
                return result

            # Sanity check position math
            notional = float(position.usd_amount) * leverage
            valid, details = SanityChecker.check_position_math(
                float(balance), float(position.usd_amount), leverage, notional
            )
            if not valid:
                logger.error(f"Position math sanity failed: {details}")
                result.errors.append(f"Sanity: {details}")
                return result

            # Check liquidation buffer
            valid_liq = self.leverage_manager.calculate_liquidation_buffer(
                float(best_signal.entry_price), leverage, best_signal.direction
            )
            if not valid_liq:
                logger.warning("Liquidation buffer too small")
                result.errors.append("Liquidation buffer < 5%")
                return result

        except Exception as e:
            logger.error(f"Risk management failed: {e}")
            errors.append(f"Risk: {e}")
            return result

        # ─── Step 4: Audit Decision ───
        try:
            audit = self.decision_auditor.audit_decision(
                signal=best_signal,
                regime=regime,
                risk_approval={
                    "position_usd": float(position.usd_amount),
                    "leverage": leverage,
                    "notional": notional,
                    "win_rate": win_rate,
                    "rr_ratio": rr_ratio,
                },
                market_data={"pair": best_pair, "balance": float(balance)},
            )
            logger.info(f"Audit: {len(audit.devils_advocate)} counter-arguments logged")
        except Exception as e:
            logger.warning(f"Audit failed (non-blocking): {e}")

        # ─── Step 5: Execute Trade ───
        try:
            # Set leverage
            await self.order_manager.set_leverage(best_pair, leverage)

            # Place order
            side = "buy" if best_signal.direction == "long" else "sell"
            order_result = await self.order_manager.place_market_order(
                symbol=best_pair,
                side=side,
                amount=float(position.usd_amount) * leverage / float(best_signal.entry_price),
            )

            # Verify order was placed
            order_status = await self.order_manager.get_order_status(
                best_pair, order_result.order_id
            )

            if order_status.status not in ("filled", "closed"):
                logger.warning(f"Order not filled: {order_status.status}")
                result.errors.append(f"Order status: {order_status.status}")
                return result

            # Place stop-loss
            sl_side = "sell" if best_signal.direction == "long" else "buy"
            await self.order_manager.place_stop_loss(
                symbol=best_pair,
                side=sl_side,
                amount=order_result.filled_amount,
                stop_price=float(best_signal.stop_loss),
            )

            result.trade_placed = True
            result.trade_details = {
                "pair": best_pair,
                "direction": best_signal.direction,
                "entry_price": float(order_result.avg_fill_price),
                "size": float(order_result.filled_amount),
                "leverage": leverage,
                "stop_loss": float(best_signal.stop_loss),
                "take_profit": float(best_signal.take_profit),
                "strategy": best_signal.strategy_name,
                "confidence": best_signal.confidence,
            }

            logger.info(
                f"TRADE PLACED: {best_pair} {best_signal.direction} "
                f"@ {order_result.avg_fill_price} x{leverage}"
            )

            await self.alert_system.send_alert(
                f"Trade: {best_pair} {best_signal.direction} "
                f"@ {order_result.avg_fill_price} x{leverage} "
                f"SL={best_signal.stop_loss}",
                level="info",
            )

        except Exception as e:
            logger.error(f"Execution failed: {e}")
            errors.append(f"Execution: {e}")

        # ─── Step 6: Memory ───
        try:
            if result.trade_placed and result.trade_details:
                self.trade_journal.record_trade_entry(result.trade_details)
        except Exception as e:
            logger.warning(f"Memory recording failed (non-blocking): {e}")

        result.errors = errors
        result.duration_seconds = (datetime.now(timezone.utc) - cycle_start).total_seconds()
        self.state.last_cycle_time = datetime.now(timezone.utc)

        logger.info(
            f"=== Cycle {self.state.cycle_count} complete "
            f"({result.duration_seconds:.1f}s) ==="
        )
        return result

    async def _check_daily_report(self) -> None:
        """Generate daily report at UTC midnight."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.state.last_daily_report == today:
            return

        now = datetime.now(timezone.utc)
        if now.hour == 0 and now.minute < 10:
            try:
                logger.info("Generating daily report...")
                trades = self.trade_journal.get_recent_trades(100)
                daily_pnl = self.pnl_calculator.calculate_daily_pnl(
                    trades=trades,
                    start_balance=float(self.state.daily_start_balance),
                    end_balance=float(self.state.current_balance),
                )
                report = self.report_generator.generate_daily_report(
                    daily_pnl=daily_pnl,
                    trades=trades,
                    strategy_perf={},
                )
                self.report_generator.save_report(report, today)
                await self.alert_system.send_daily_report(report)

                self.state.last_daily_report = today
                # Reset daily start balance
                self.state.daily_start_balance = self.state.current_balance

                logger.info(f"Daily report saved for {today}")
            except Exception as e:
                logger.error(f"Daily report failed: {e}")

    def _save_cycle_state(self, result: CycleResult) -> None:
        """Save cycle result to agent state directory."""
        state_file = AGENT_STATE_DIR / "last_cycle.json"
        try:
            state_file.write_text(result.model_dump_json(indent=2))
        except Exception as e:
            logger.warning(f"Failed to save cycle state: {e}")


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
