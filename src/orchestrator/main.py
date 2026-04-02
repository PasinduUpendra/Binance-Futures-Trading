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
from pydantic import BaseModel, Field, model_validator

load_dotenv()

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.data.market_data import MarketDataClient
from src.data.indicator_engine import IndicatorEngine
from src.data.data_validator import DataValidator
from src.data.database import DatabaseManager, CycleHistoryRow, DailyReportRow
from src.strategies.base_strategy import SignalDirection, calculate_rr_ratio
from src.strategies.regime_detector import RegimeDetector
from src.strategies.adaptive_strategy import AdaptiveStrategy
from src.risk.circuit_breaker import CircuitBreaker, CircuitBreakerLevel, TradeResult
from src.risk.position_sizer import PositionSizer
from src.risk.leverage_manager import LeverageManager
from src.risk.volatility_model import VolatilityModel
from src.risk.drawdown_monitor import DrawdownMonitor
from src.execution.order_manager import OrderManager, OrderState
from src.execution.position_tracker import Position, PositionTracker
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
    model_config = {"frozen": False}

    symbol: str
    direction: str  # 'long' or 'short'
    entry_price: float
    best_price: float  # Best price since entry (high for long, low for short)
    atr_4h: float  # ATR(4H) at time of entry
    activated: bool = False  # True once price moved 2.0 ATR favorable
    strategy_name: str = ""
    take_profit: float = 0.0  # Original TP price for re-placement after ST reversal
    tp_pending: bool = False  # True when TP placement failed and needs retry

    # Trailing stop parameters (from v3 backtest)
    ACTIVATE_ATR_MULT: float = 2.0
    TRAIL_ATR_MULT: float = 2.5

    @model_validator(mode="after")
    def _validate_tp_direction(self) -> "TrailingStopState":
        """Reject take_profit on wrong side of entry_price."""
        if self.take_profit == 0.0:
            return self  # 0.0 means "no TP set"
        if self.direction == "long" and self.take_profit <= self.entry_price:
            raise ValueError(
                f"LONG TP {self.take_profit} must be above entry {self.entry_price}"
            )
        if self.direction == "short" and self.take_profit >= self.entry_price:
            raise ValueError(
                f"SHORT TP {self.take_profit} must be below entry {self.entry_price}"
            )
        return self


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

        # Serialize position-check → execution to prevent _run_cycle and
        # _on_4h_close from opening duplicate/excess positions concurrently.
        self._execution_lock = asyncio.Lock()

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
        self.fee_calculator = FeeCalculator(use_bnb_discount=False)
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
        self.alert_system.log_channel_status()
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

        # Get initial balance — use margin balance (equity) for consistency
        # with _run_cycle() which also uses get_margin_balance().
        try:
            balance = await self.market_data.get_margin_balance()
            self.state.current_balance = balance
            logger.info(f"Initial balance (equity): ${balance}")
        except Exception as e:
            logger.error(f"Failed to get initial balance: {e}")
            self.state.halt_reason = f"Cannot connect to exchange: {e}"
            return

        # Log ALL assets on the account (USDT, USDC, BTC, etc.)
        try:
            all_assets = await self.market_data.get_all_assets()
            self._configure_fee_calculator(all_assets)
            logger.info(
                "=== Full Account Assets (%d non-zero) ===", len(all_assets),
            )
            for asset in all_assets:
                logger.info(
                    "  %s: wallet=%s  margin=%s  upnl=%s",
                    asset.asset,
                    asset.wallet_balance,
                    asset.margin_balance,
                    asset.unrealized_pnl,
                )
            # Note: Only USDT is used as margin for USDT-M Futures
            # (Multi-Asset Mode is OFF). Other assets are visible
            # but not tradeable without enabling Multi-Asset Mode.
            non_usdt = [a for a in all_assets if a.asset != "USDT"]
            if non_usdt:
                logger.info(
                    "Non-USDT assets detected (not usable for USDT-M margin "
                    "unless Multi-Asset Mode is enabled): %s",
                    ", ".join(
                        f"{a.asset}={a.wallet_balance}" for a in non_usdt
                    ),
                )
        except Exception as e:
            logger.warning(f"Failed to fetch full asset list: {e}")

        # Restore persisted daily state (survives restarts)
        self._load_daily_state(balance)
        self._load_trailing_stop_state()

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
        self._persist_trailing_stop_state()
        self._persist_daily_state()

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
            balance = await self.market_data.get_margin_balance()
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
                peak_balance=self.drawdown_monitor.peak_balance,
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
                # Drop last row: Binance returns the in-progress candle which
                # can cause false Supertrend flips on incomplete data.
                df_4h = df_4h.iloc[:-1].reset_index(drop=True)
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

        # ─── Steps 4-7: Risk, Audit, Execute, Memory — delegated to shared method ───
        trade_result = await self._execute_signal(
            signal=best_signal,
            symbol=best_pair,
            df_4h=pair_data_4h[best_pair],
            df_1h=pair_data_1h[best_pair],
            cb_state=cb_state,
            trigger="hourly_cycle",
        )
        if trade_result is not None:
            result.trade_placed = True
            result.trade_details = trade_result
        else:
            result.signal_generated = True  # signal existed, but execution rejected

        result.errors = errors
        result.duration_seconds = (datetime.now(timezone.utc) - cycle_start).total_seconds()
        self.state.last_cycle_time = datetime.now(timezone.utc)
        return result

    # ------------------------------------------------------------------
    # Shared execution (lock-protected) — used by _run_cycle & _on_4h_close
    # ------------------------------------------------------------------

    async def _execute_signal(
        self,
        signal: Any,
        symbol: str,
        df_4h: pd.DataFrame,
        df_1h: pd.DataFrame,
        cb_state: Any,
        trigger: str = "hourly_cycle",
    ) -> dict | None:
        """Risk-check, size, and execute a signal under the execution lock.

        Returns trade_details dict on success, or None if the trade was
        rejected or failed.  The ``_execution_lock`` ensures only ONE
        signal is being checked-and-executed at a time, preventing the
        TOCTOU race between ``_run_cycle`` and ``_on_4h_close``.
        """
        async with self._execution_lock:
            # Fetch FRESH balance (fixes stale-balance bug in _on_4h_close)
            try:
                balance = await self.market_data.get_margin_balance()
                self.state.current_balance = balance
            except Exception as e:
                logger.error(f"[{trigger}] Balance fetch failed: {e}")
                return None

            # Re-evaluate circuit breaker with FRESH balance inside lock.
            # The cb_state passed in was computed before acquiring the lock
            # and may be stale if another signal executed between then and now.
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
                peak_balance=self.drawdown_monitor.peak_balance,
            )
            if not cb_state.constraints.trading_allowed:
                logger.info(
                    f"[{trigger}] CB re-check rejected: {cb_state.constraints.reason}"
                )
                return None

            constraints = cb_state.constraints

            # ── Position count gate ──
            open_positions = await self.position_tracker.get_open_positions()
            if len(open_positions) >= constraints.max_positions:
                # Try smart swap: close worst position if new signal is much better
                swap_pos = await self._find_swap_candidate(
                    new_confidence=signal.confidence,
                    open_positions=open_positions,
                )
                if swap_pos is None:
                    logger.info(
                        f"[{trigger}] Max positions reached "
                        f"({len(open_positions)}/{constraints.max_positions})"
                    )
                    return None

                swapped = await self._close_position_for_swap(
                    swap_pos, symbol, trigger,
                )
                if not swapped:
                    logger.info(
                        f"[{trigger}] Max positions reached and swap failed"
                    )
                    return None

            for pos in open_positions:
                if pos.symbol == symbol:
                    logger.info(f"[{trigger}] Already positioned in {symbol}")
                    return None

            # ── Leverage ──
            leverage_result = LeverageManager.determine_leverage(
                confidence=signal.confidence,
                regime=signal.regime,
                circuit_breaker_level=cb_state.level,
            )
            leverage = leverage_result.leverage
            if leverage == 0:
                logger.info(f"[{trigger}] Leverage 0: {leverage_result.reason}")
                return None

            # GARCH volatility adjustment
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
                    f"[{trigger}] GARCH: vol_ratio={vol_state.vol_ratio}, "
                    f"leverage_scale={vol_state.leverage_scale}, "
                    f"final_leverage={leverage}x"
                )

            # ── Confidence-based position sizing (v6 backtest) ──
            # Base tier from confidence, then optionally capped by Kelly
            # criterion if sufficient trade history exists (R/R aware sizing).
            confidence = signal.confidence
            if confidence >= 60:
                position_pct = Decimal("0.15")
            elif confidence >= 45:
                position_pct = Decimal("0.10")
            else:
                position_pct = Decimal("0.07")
            max_cap = Decimal("0.15")  # Immutable Rule #4

            # R/R-aware Kelly ceiling: if trade history is sufficient,
            # use Kelly-optimal size as an upper bound on position_pct.
            # This rewards higher R/R signals and penalizes marginal ones.
            try:
                rr = calculate_rr_ratio(
                    signal.entry_price, signal.stop_loss, signal.take_profit,
                )
                journal_trades = self.trade_journal.get_all_trades()
                if len(journal_trades) >= 10:
                    wins = sum(
                        1 for t in journal_trades
                        if t.pnl is not None and t.pnl > 0
                    )
                    closed = sum(
                        1 for t in journal_trades if t.pnl is not None
                    )
                    win_rate = wins / closed if closed > 0 else 0.5
                    kelly_size = self.position_sizer.calculate_size(
                        balance=balance,
                        win_rate=win_rate,
                        rr_ratio=rr,
                        circuit_breaker_state=cb_state,
                        requested_leverage=leverage,
                    )
                    kelly_pct = kelly_size.pct_of_balance
                    if Decimal("0") < kelly_pct < position_pct:
                        logger.info(
                            f"[{trigger}] Kelly ceiling: {float(kelly_pct)*100:.1f}% "
                            f"< confidence tier {float(position_pct)*100:.0f}% "
                            f"(WR={win_rate:.2f}, R/R={rr:.1f})"
                        )
                        position_pct = kelly_pct
            except Exception as e:
                logger.debug(f"[{trigger}] Kelly sizing skipped: {e}")

            position_pct *= constraints.size_multiplier
            margin = (balance * position_pct).quantize(Decimal("0.01"))
            max_margin = (balance * max_cap).quantize(Decimal("0.01"))
            if margin > max_margin:
                margin = max_margin
            if margin < Decimal("5"):
                if balance < Decimal("5"):
                    logger.info(f"[{trigger}] Balance ${balance} below $5 minimum")
                    return None
                margin = Decimal("5")

            notional = margin * Decimal(str(leverage))

            # Minimum notional per pair
            pair_min_notional = MIN_NOTIONAL.get(symbol, DEFAULT_MIN_NOTIONAL)
            if float(notional) < pair_min_notional:
                logger.info(
                    f"[{trigger}] Notional ${notional:.2f} below minimum "
                    f"${pair_min_notional} for {symbol}"
                )
                return None

            logger.info(
                f"[{trigger}] Sized: ${margin} ({float(position_pct)*100:.0f}% of "
                f"${balance}) x{leverage} = ${notional:.2f} notional "
                f"(confidence={confidence:.0f}%)"
            )

            # Sanity check position math
            valid, details = SanityChecker.check_position_math(
                Decimal(str(balance)), margin, leverage, notional,
                max_position_pct=max_cap,
            )
            if not valid:
                logger.error(f"[{trigger}] Position math sanity failed: {details}")
                return None

            # Liquidation buffer
            liq_buffer = LeverageManager.calculate_liquidation_buffer(
                entry_price=signal.entry_price,
                leverage=leverage,
                direction=signal.direction.value,
            )
            if not liq_buffer.is_safe:
                logger.warning(
                    f"[{trigger}] Liquidation buffer "
                    f"{float(liq_buffer.buffer_pct)*100:.1f}% < 5%"
                )
                return None

            # ── Audit (non-blocking) ──
            try:
                regime = self.regime_detector.detect(df_4h)
                signal_dict = signal.model_dump() if hasattr(signal, "model_dump") else dict(signal)
                regime_dict = regime.model_dump() if hasattr(regime, "model_dump") else dict(regime)
                signal_dict.setdefault("symbol", symbol)
                signal_dict.setdefault("strategy", signal_dict.get("strategy_name", ""))
                self.decision_auditor.audit_decision(
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
                    market_data={"pair": symbol, "balance": float(balance)},
                )
            except Exception as e:
                logger.warning(f"[{trigger}] Audit failed (non-blocking): {e}")

            # ── Execute ──
            try:
                await self.order_manager.set_leverage(symbol, leverage)

                order_qty = Decimal(str(notional)) / Decimal(str(signal.entry_price))
                side = "buy" if signal.direction.value == "long" else "sell"
                order_result = await self.order_manager.place_market_order(
                    symbol=symbol, side=side, amount=order_qty,
                )

                order_status = await self.order_manager.get_order_status(
                    symbol, order_result.order_id
                )
                if order_status.status not in (OrderState.CLOSED,):
                    logger.warning(
                        f"[{trigger}] Order not filled: {order_status.status.value}"
                    )
                    return None

                sl_side = "sell" if signal.direction.value == "long" else "buy"
                await self.order_manager.place_stop_loss(
                    symbol=symbol,
                    side=sl_side,
                    amount=order_result.filled,
                    stop_price=Decimal(str(signal.stop_loss)),
                )

                try:
                    await self.order_manager.place_take_profit(
                        symbol=symbol,
                        side=sl_side,
                        amount=order_result.filled,
                        stop_price=Decimal(str(signal.take_profit)),
                    )
                except Exception as tp_err:
                    logger.warning(
                        f"[{trigger}] TP order failed (non-blocking): {tp_err}"
                    )

                fill_price = (
                    order_result.average_fill_price
                    or Decimal(str(signal.entry_price))
                )

                trade_details: dict[str, Any] = {
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
                    "trigger": trigger,
                }

                # Trailing stop state
                atr_4h = (
                    float(df_4h["atr"].dropna().iloc[-1])
                    if "atr" in df_4h.columns
                    else 0.0
                )
                self._trailing_stops[symbol] = TrailingStopState(
                    symbol=symbol,
                    direction=signal.direction.value,
                    entry_price=float(fill_price),
                    best_price=float(fill_price),
                    atr_4h=atr_4h,
                    strategy_name=signal.strategy_name,
                    take_profit=signal.take_profit,
                )

                logger.info(
                    f"[{trigger}] TRADE PLACED: {symbol} {signal.direction.value} "
                    f"@ {fill_price} x{leverage} (ATR={atr_4h:.6f})"
                )
                await self.alert_system.send_alert(
                    f"Trade: {symbol} {signal.direction.value} "
                    f"@ {fill_price} x{leverage} SL={signal.stop_loss}",
                    level="info",
                )

                # Memory
                try:
                    self.trade_journal.record_trade_entry(trade_details)
                except Exception as e:
                    logger.warning(f"[{trigger}] Memory recording failed: {e}")

                return trade_details

            except Exception as e:
                logger.error(f"[{trigger}] Execution failed: {e}")
                return None

    # ------------------------------------------------------------------
    # Trade Exit Recording Helper
    # ------------------------------------------------------------------

    def _configure_fee_calculator(self, all_assets: list[Any]) -> None:
        """Enable BNB fee discount only when a positive BNB balance is verified."""
        bnb_asset = next((asset for asset in all_assets if asset.asset == "BNB"), None)
        use_bnb_discount = bool(bnb_asset and bnb_asset.wallet_balance > Decimal("0"))
        self.fee_calculator = FeeCalculator(use_bnb_discount=use_bnb_discount)

        if use_bnb_discount:
            logger.info(
                "BNB fee discount verified from account assets: BNB=%s",
                bnb_asset.wallet_balance,
            )
        else:
            logger.warning(
                "BNB fee discount disabled: no positive BNB balance verified on account"
            )

    def _record_trade_exit(
        self,
        symbol: str,
        exit_price: float,
        pnl: float,
        entry_price: float,
        reason: str,
        duration_hours: float | None = None,
    ) -> None:
        """Record trade exit in journal so win/loss tracking works.

        Computes pnl_pct from pnl / (entry_price * size) and delegates to
        ``TradeJournal.update_trade_exit()``.  Failures are logged but
        never propagate — exit recording must not block position management.
        """
        try:
            # pnl_pct: approximate as pnl / margin (entry_price is a proxy)
            pnl_pct = (pnl / entry_price * 100) if entry_price else 0.0
            self.trade_journal.update_trade_exit(
                symbol=symbol,
                exit_price=Decimal(str(exit_price)),
                pnl=Decimal(str(pnl)),
                pnl_pct=Decimal(str(round(pnl_pct, 4))),
                duration=duration_hours,
                reason=reason,
            )
        except Exception as e:
            logger.warning(
                "Failed to record trade exit for %s: %s", symbol, e
            )

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
                        stop_price=Decimal(str(entry_price)),
                    )

                    # Re-place TP (cancel_open_orders removed it too).
                    # Backtest keeps TP intact; production must match.
                    if ts_state.take_profit and ts_state.take_profit > 0:
                        # Validate TP direction before placement
                        tp_valid = (
                            (ts_state.direction == "long" and ts_state.take_profit > ts_state.entry_price)
                            or (ts_state.direction == "short" and ts_state.take_profit < ts_state.entry_price)
                        )
                        if not tp_valid:
                            logger.error(
                                "ST reversal: TP %.6f invalid for %s %s (entry=%.6f) — clearing TP",
                                ts_state.take_profit, pos.symbol, ts_state.direction, ts_state.entry_price,
                            )
                            ts_state.take_profit = 0.0
                            self._persist_trailing_stop_state()
                        else:
                            try:
                                await self.order_manager.place_take_profit(
                                    symbol=pos.symbol,
                                    side=sl_side,
                                    amount=pos.size,
                                    stop_price=Decimal(str(ts_state.take_profit)),
                                )
                            except Exception as tp_err:
                                logger.warning(
                                    f"ST reversal: TP re-placement failed for "
                                    f"{pos.symbol} (non-blocking): {tp_err}"
                                )
                                ts_state.tp_pending = True
                                self._persist_trailing_stop_state()

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
            if ts_state is None:
                continue

            state_changed = False

            # Recover atr_4h for pre-existing positions (set to 0 on restart)
            if ts_state.atr_4h <= 0 and pos.symbol in pair_data_4h:
                df = pair_data_4h[pos.symbol]
                if "atr" in df.columns:
                    atr_val = float(df["atr"].dropna().iloc[-1])
                    if atr_val > 0:
                        ts_state.atr_4h = atr_val
                        state_changed = True
                        logger.info(
                            "Recovered ATR(4H)=%.6f for pre-existing %s",
                            atr_val, pos.symbol,
                        )

            if ts_state.atr_4h <= 0:
                continue

            current_price = float(pos.current_price)

            # Update best price
            if ts_state.direction == "long":
                if current_price > ts_state.best_price:
                    ts_state.best_price = current_price
                    state_changed = True
            else:
                if current_price < ts_state.best_price or ts_state.best_price == ts_state.entry_price:
                    ts_state.best_price = current_price
                    state_changed = True

            # Check activation: has price moved 2.0 ATR favorable from entry?
            favorable_move = (
                ts_state.best_price - ts_state.entry_price
                if ts_state.direction == "long"
                else ts_state.entry_price - ts_state.best_price
            )

            activate_threshold = ts_state.atr_4h * ts_state.ACTIVATE_ATR_MULT
            if favorable_move >= activate_threshold:
                if not ts_state.activated:
                    ts_state.activated = True
                    state_changed = True

            if not ts_state.activated:
                if state_changed:
                    self._persist_trailing_stop_state()
                continue

            if state_changed:
                self._persist_trailing_stop_state()

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
                    self._persist_trailing_stop_state()

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

                    # Record exit in trade journal for win/loss tracking
                    self._record_trade_exit(
                        symbol=pos.symbol,
                        exit_price=current_price,
                        pnl=float(pos.unrealized_pnl),
                        entry_price=ts_state.entry_price,
                        reason="trailing_stop",
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
                self._persist_trailing_stop_state()

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

                # Record exit in trade journal for win/loss tracking
                self._record_trade_exit(
                    symbol=pos.symbol,
                    exit_price=float(pos.current_price),
                    pnl=float(pos.unrealized_pnl),
                    entry_price=float(pos.entry_price),
                    reason="time_exit",
                    duration_hours=bars_held,
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
            ts_state = self._trailing_stops.get(sym)
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

            # Record exit in trade journal — use last known price from
            # trailing state. Exact fill comes from exchange trade history
            # but this is sufficient for win/loss streak tracking.
            if ts_state is not None:
                try:
                    current_price = await self.market_data.get_current_price(sym)
                    exit_px = float(current_price)
                    entry_px = ts_state.entry_price
                    direction = ts_state.direction
                    if direction == "long":
                        pnl_approx = exit_px - entry_px
                    else:
                        pnl_approx = entry_px - exit_px
                    self._record_trade_exit(
                        symbol=sym,
                        exit_price=exit_px,
                        pnl=pnl_approx,
                        entry_price=entry_px,
                        reason="sl_tp_fire",
                    )
                except Exception as e:
                    logger.warning(
                        "RECONCILE: Failed to record exit for %s: %s", sym, e
                    )

            self._trailing_stops.pop(sym, None)
            self._persist_trailing_stop_state()

        # 2. For every open position, ensure it's tracked and has protective orders.
        #    Cache orders per symbol to avoid duplicate API calls in step 3.
        _orders_cache: dict[str, list] = {}
        for pos in open_positions:
            # Register untracked positions so reconciliation covers them
            if pos.symbol not in self._trailing_stops:
                logger.warning(
                    "RECONCILE: %s %s not in trailing_stops — registering",
                    pos.symbol, pos.side,
                )
                self._trailing_stops[pos.symbol] = TrailingStopState(
                    symbol=pos.symbol,
                    direction=pos.side,
                    entry_price=float(pos.entry_price),
                    best_price=float(pos.current_price),
                    atr_4h=0.0,
                    strategy_name="reconciled",
                )
                self._persist_trailing_stop_state()

            try:
                open_orders = await self.order_manager.get_open_orders(
                    pos.symbol,
                    conditional_only=True,
                )
                _orders_cache[pos.symbol] = open_orders

                # Discriminate SL vs TP by order type
                has_sl = any(
                    "stop" in o.order_type.lower()
                    and "profit" not in o.order_type.lower()
                    for o in open_orders
                )
                has_tp = any(
                    "profit" in o.order_type.lower()
                    for o in open_orders
                )

                # Treat tp_pending as missing TP (retry failed placements)
                ts_state = self._trailing_stops.get(pos.symbol)
                if ts_state and ts_state.tp_pending:
                    has_tp = False

                if not has_sl:
                    logger.warning(
                        "RECONCILE: %s missing SL order — placing emergency SL.",
                        pos.symbol,
                    )
                    await self._place_emergency_stop_loss(pos)
                if not has_tp:
                    logger.warning(
                        "RECONCILE: %s missing TP order — placing emergency TP.",
                        pos.symbol,
                    )
                    await self._place_emergency_take_profit(pos)
            except Exception as e:
                logger.error(
                    "RECONCILE: Failed to check orders for %s: %s", pos.symbol, e
                )

        # 3. Excess position handling: close most vulnerable positions
        #    to bring count under circuit breaker max.
        try:
            recent_entries = self.trade_journal.get_recent_trades(10)
            recent_trade_results = [
                TradeResult(
                    is_win=t.pnl is not None and t.pnl > 0,
                    closed_at=t.timestamp,
                )
                for t in recent_entries if t.pnl is not None
            ]
            cb_state = CircuitBreaker.is_trading_allowed(
                balance=self.state.current_balance,
                recent_trades=recent_trade_results,
                start_of_day_balance=self.state.daily_start_balance,
                peak_balance=self.drawdown_monitor.peak_balance,
            )
            max_pos = cb_state.constraints.max_positions
            if len(open_positions) > max_pos:
                excess = len(open_positions) - max_pos
                logger.warning(
                    "RECONCILE: %d positions exceeds CB max %d — "
                    "closing %d most vulnerable.",
                    len(open_positions), max_pos, excess,
                )
                # Gather order counts per position (use cache from step 2)
                pos_vulnerability: list[tuple[Position, int]] = []
                for pos in open_positions:
                    cached = _orders_cache.get(pos.symbol)
                    if cached is not None:
                        pos_vulnerability.append((pos, len(cached)))
                    else:
                        try:
                            orders = await self.order_manager.get_open_orders(
                                pos.symbol, conditional_only=True,
                            )
                            pos_vulnerability.append((pos, len(orders)))
                        except Exception:
                            pos_vulnerability.append((pos, 0))

                # Sort: fewest orders first, then lowest unrealized PnL
                pos_vulnerability.sort(
                    key=lambda x: (x[1], float(x[0].unrealized_pnl))
                )

                for i in range(excess):
                    close_pos, n_orders = pos_vulnerability[i]
                    close_side = "sell" if close_pos.side == "long" else "buy"
                    logger.warning(
                        "RECONCILE: Closing excess position %s %s "
                        "(orders=%d, PnL=%s)",
                        close_pos.symbol, close_pos.side,
                        n_orders, close_pos.unrealized_pnl,
                    )
                    try:
                        await self.order_manager.cancel_open_orders(
                            close_pos.symbol,
                        )
                        await self.order_manager.place_market_order(
                            symbol=close_pos.symbol,
                            side=close_side,
                            amount=close_pos.size,
                        )
                        self._trailing_stops.pop(close_pos.symbol, None)
                        self._persist_trailing_stop_state()
                    except Exception as e:
                        logger.error(
                            "RECONCILE: Failed to close excess %s: %s",
                            close_pos.symbol, e,
                        )
        except Exception as e:
            logger.error("RECONCILE: Excess position check failed: %s", e)

    async def _place_emergency_stop_loss(self, pos: Position) -> None:
        """Place emergency stop-loss for unprotected positions.

        Uses 3×ATR(4H) from trailing stop state if available. Falls back to
        entry price (breakeven) when ATR is unavailable.
        """
        try:
            entry_price = float(pos.entry_price)
            sl_price = entry_price  # default: breakeven

            ts_state = self._trailing_stops.get(pos.symbol)
            if ts_state and ts_state.atr_4h > 0:
                SL_ATR_MULT = 3.0
                if pos.side == "long":
                    sl_price = entry_price - (SL_ATR_MULT * ts_state.atr_4h)
                else:
                    sl_price = entry_price + (SL_ATR_MULT * ts_state.atr_4h)

            sl_side = "sell" if pos.side == "long" else "buy"
            result = await self.order_manager.place_stop_loss(
                symbol=pos.symbol,
                side=sl_side,
                amount=pos.size,
                stop_price=Decimal(str(sl_price)),
            )
            if result:
                sl_type = "ATR-based" if (ts_state and ts_state.atr_4h > 0) else "breakeven"
                logger.info(
                    "RECONCILE: Emergency SL placed for %s at %.6f (%s)",
                    pos.symbol, sl_price, sl_type,
                )
            else:
                logger.error(
                    "RECONCILE: Emergency SL returned None for %s", pos.symbol,
                )
        except Exception as e:
            logger.error(
                "RECONCILE: Emergency SL placement failed for %s: %s",
                pos.symbol, e,
            )

    async def _place_emergency_take_profit(self, pos: Position) -> None:
        """Place emergency take-profit for unprotected positions.

        Uses stored TP from trailing stop state if valid, otherwise computes
        from 6×ATR(4H). If ATR is unavailable, sets tp_pending for retry.
        """
        ts_state = self._trailing_stops.get(pos.symbol)
        try:
            entry_price = float(pos.entry_price)
            tp_price = 0.0

            # Try stored TP (validate direction)
            if ts_state and ts_state.take_profit > 0:
                if pos.side == "long" and ts_state.take_profit > entry_price:
                    tp_price = ts_state.take_profit
                elif pos.side == "short" and ts_state.take_profit < entry_price:
                    tp_price = ts_state.take_profit

            # Fall back to 6×ATR computation
            if tp_price <= 0 and ts_state and ts_state.atr_4h > 0:
                TP_ATR_MULT = 6.0
                if pos.side == "long":
                    tp_price = entry_price + (TP_ATR_MULT * ts_state.atr_4h)
                else:
                    tp_price = entry_price - (TP_ATR_MULT * ts_state.atr_4h)

            if tp_price <= 0:
                logger.error(
                    "RECONCILE: Cannot compute TP for %s — no valid TP or ATR. "
                    "Setting tp_pending=True", pos.symbol,
                )
                if ts_state:
                    ts_state.tp_pending = True
                    self._persist_trailing_stop_state()
                return

            tp_side = "sell" if pos.side == "long" else "buy"
            result = await self.order_manager.place_take_profit(
                symbol=pos.symbol,
                side=tp_side,
                amount=pos.size,
                stop_price=Decimal(str(tp_price)),
            )
            if result:
                logger.info(
                    "RECONCILE: Emergency TP placed for %s at %.6f",
                    pos.symbol, tp_price,
                )
                if ts_state:
                    ts_state.take_profit = tp_price
                    ts_state.tp_pending = False
                    self._persist_trailing_stop_state()
            else:
                logger.error(
                    "RECONCILE: Emergency TP returned None for %s", pos.symbol,
                )
                if ts_state:
                    ts_state.tp_pending = True
                    self._persist_trailing_stop_state()
        except Exception as e:
            logger.error(
                "RECONCILE: Emergency TP placement failed for %s: %s",
                pos.symbol, e,
            )
            if ts_state:
                ts_state.tp_pending = True
                self._persist_trailing_stop_state()

    async def _find_swap_candidate(
        self,
        new_confidence: float,
        open_positions: list[Position],
    ) -> Position | None:
        """Find the best swap candidate among open positions.

        Returns the position to close for swap, or None if no suitable candidate.
        Requirements:
        - New signal confidence >= 70%
        - Position has NEGATIVE unrealized PnL
        - Trailing stop NOT activated
        - New confidence exceeds entry confidence by >= 20 points
        """
        if new_confidence < 70:
            return None

        candidates: list[tuple[Position, float]] = []

        # Look up entry confidence from trade journal
        recent_trades = self.trade_journal.get_recent_trades(50)

        for pos in open_positions:
            if float(pos.unrealized_pnl) >= 0:
                continue

            ts_state = self._trailing_stops.get(pos.symbol)
            if ts_state and ts_state.activated:
                continue

            entry_confidence = 0.0
            for trade in recent_trades:
                if trade.symbol == pos.symbol and trade.exit_price is None:
                    entry_confidence = float(trade.confidence)
                    break

            if new_confidence - entry_confidence >= 20:
                candidates.append((pos, entry_confidence))

        if not candidates:
            return None

        # Sort by worst PnL first (most negative = best swap target)
        candidates.sort(key=lambda x: float(x[0].unrealized_pnl))
        return candidates[0][0]

    async def _close_position_for_swap(
        self,
        pos: Position,
        new_symbol: str,
        trigger: str,
    ) -> bool:
        """Close a position to make room for a better trade.

        Returns True on success.
        """
        try:
            close_side = "sell" if pos.side == "long" else "buy"
            await self.order_manager.cancel_open_orders(pos.symbol)
            result = await self.order_manager.place_market_order(
                symbol=pos.symbol,
                side=close_side,
                amount=pos.size,
            )
            if result:
                logger.info(
                    "[%s] SWAP: Closed %s %s (PnL=%s) to open %s",
                    trigger, pos.symbol, pos.side, pos.unrealized_pnl, new_symbol,
                )

                self._record_trade_exit(
                    symbol=pos.symbol,
                    exit_price=float(pos.current_price),
                    pnl=float(pos.unrealized_pnl),
                    entry_price=float(pos.entry_price),
                    reason="swap_out",
                )

                self._trailing_stops.pop(pos.symbol, None)
                self._persist_trailing_stop_state()
                return True
            else:
                logger.error(
                    "[%s] SWAP: Market close returned None for %s",
                    trigger, pos.symbol,
                )
                return False
        except Exception as e:
            logger.error(
                "[%s] SWAP: Failed to close %s: %s", trigger, pos.symbol, e,
            )
            return False

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

        open_symbols = {pos.symbol for pos in open_positions}
        stale_symbols = [
            symbol for symbol in list(self._trailing_stops.keys())
            if symbol not in open_symbols
        ]
        for symbol in stale_symbols:
            self._trailing_stops.pop(symbol, None)

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
            else:
                ts_state = self._trailing_stops[pos.symbol]
                ts_state.direction = pos.side
                ts_state.entry_price = float(pos.entry_price)
                if ts_state.best_price <= 0:
                    ts_state.best_price = float(pos.current_price)

            # Check if position has any conditional orders
            try:
                open_orders = await self.order_manager.get_open_orders(
                    pos.symbol,
                    conditional_only=True,
                )
                if len(open_orders) == 0:
                    logger.warning(
                        "STARTUP: %s %s position has ZERO orders (no SL/TP). "
                        "Position is UNPROTECTED.", pos.symbol, pos.side
                    )
            except Exception as e:
                logger.error(
                    "STARTUP: Failed to check orders for %s: %s", pos.symbol, e
                )

        self._persist_trailing_stop_state()

    # ------------------------------------------------------------------
    # Event-driven 4H candle close handler
    # ------------------------------------------------------------------

    async def _on_4h_close(self, candle: dict) -> None:
        """Triggered by WebSocket on every 4H candle close.

        Immediately re-runs signal generation + trailing stop check for the
        pair that closed, eliminating up to 59 minutes of entry delay
        compared to the hourly polling cycle.

        Execution delegates to ``_execute_signal()`` (lock-protected) to
        prevent race conditions with the hourly ``_run_cycle``.
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
            # Drop last row: after a 4H close Binance immediately starts the
            # next candle — remove it so Supertrend only uses complete data.
            df_4h = df_4h.iloc[:-1].reset_index(drop=True)
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

            # CB gate (uses fresh balance fetched inside _execute_signal)
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
                peak_balance=self.drawdown_monitor.peak_balance,
            )
            if not cb_state.constraints.trading_allowed:
                return

            # Delegate to shared lock-protected execution
            await self._execute_signal(
                signal=signal,
                symbol=symbol,
                df_4h=df_4h,
                df_1h=df_1h,
                cb_state=cb_state,
                trigger="4h_candle_close",
            )

        except Exception as e:
            logger.error(f"4H close handler error for {symbol}: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # Daily report / state persistence
    # ------------------------------------------------------------------

    async def _check_daily_report(self) -> None:
        """Generate daily report and reset daily_start_balance at UTC midnight.

        Fires on the first cycle of each new UTC day.  The
        ``last_daily_report`` date-guard prevents duplicate execution.
        No hour==0 gate — if the bot was down at midnight, the report
        and daily_start_balance reset will fire on the first cycle after
        restart.
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.state.last_daily_report == today:
            return

        now = datetime.now(timezone.utc)
        try:
            logger.info("Generating daily report for %s...", today)

            # Get today's trades and convert to dicts for PnL calculator
            all_trades = self.trade_journal.get_all_trades()
            from datetime import timedelta as _timedelta
            yesterday = (now.date() - _timedelta(days=1))
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
            self._persist_daily_state()

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

    # ------------------------------------------------------------------
    # Daily state persistence (survives restarts)
    # ------------------------------------------------------------------
    _DAILY_STATE_FILE = AGENT_STATE_DIR / "daily_state.json"
    _TRAILING_STATE_FILE = AGENT_STATE_DIR / "trailing_stops.json"

    def _load_daily_state(self, current_balance: Decimal) -> None:
        """Restore daily_start_balance and last_daily_report from disk.

        If the persisted date matches today, use the persisted
        start-of-day balance.  Otherwise fall back to current_balance
        (new day or first-ever run).
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            if self._DAILY_STATE_FILE.exists():
                raw = json.loads(
                    self._DAILY_STATE_FILE.read_text(encoding="utf-8")
                )
                persisted_date = raw.get("date", "")
                if persisted_date == today:
                    self.state.daily_start_balance = Decimal(
                        str(raw["start_of_day_balance"])
                    )
                    self.state.last_daily_report = raw.get(
                        "last_daily_report"
                    )
                    logger.info(
                        "Restored daily state for %s: "
                        "start_balance=$%.2f, last_report=%s",
                        today,
                        self.state.daily_start_balance,
                        self.state.last_daily_report,
                    )
                    return
        except (json.JSONDecodeError, KeyError, Exception) as exc:
            logger.warning("Could not load daily state: %s", exc)

        # Fallback: new day or missing/corrupt file
        self.state.daily_start_balance = current_balance
        logger.info(
            "No persisted daily state for %s — using current balance $%.2f",
            today, current_balance,
        )

    def _persist_daily_state(self) -> None:
        """Atomically write daily_start_balance and last_daily_report."""
        data = {
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "start_of_day_balance": str(self.state.daily_start_balance),
            "last_daily_report": self.state.last_daily_report,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            tmp = self._DAILY_STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.rename(self._DAILY_STATE_FILE)  # atomic on POSIX
        except OSError as exc:
            logger.error("Failed to persist daily state: %s", exc)

    def _load_trailing_stop_state(self) -> None:
        """Restore trailing stop state from database so restarts keep progress.

        Falls back to the legacy JSON file if the database table is empty
        (one-time migration path from v6.8 JSON persistence).
        """
        try:
            rows = self.db.get_all_trailing_stops()
            if rows:
                loaded: dict[str, TrailingStopState] = {}
                for symbol, data in rows.items():
                    try:
                        loaded[symbol] = TrailingStopState(
                            symbol=symbol,
                            direction=data["direction"],
                            entry_price=data["entry_price"],
                            best_price=data["best_price"],
                            atr_4h=data["atr_4h"],
                            activated=data["activated"],
                            strategy_name=data.get("strategy_name", ""),
                            take_profit=data.get("take_profit", 0.0),
                            tp_pending=data.get("tp_pending", False),
                        )
                    except (ValueError, KeyError) as entry_exc:
                        logger.error(
                            "Skipping corrupted trailing stop for %s: %s",
                            symbol, entry_exc,
                        )
                self._trailing_stops = loaded
                logger.info(
                    "Restored %d trailing stop state(s) from database",
                    len(self._trailing_stops),
                )
                return
        except Exception as exc:
            logger.warning("Could not load trailing stops from DB: %s", exc)

        # Fallback: migrate from legacy JSON file
        try:
            if not self._TRAILING_STATE_FILE.exists():
                return

            raw = json.loads(
                self._TRAILING_STATE_FILE.read_text(encoding="utf-8")
            )
            loaded_json: dict[str, TrailingStopState] = {}
            for symbol, state in raw.items():
                try:
                    loaded_json[symbol] = TrailingStopState(**state)
                except (ValueError, KeyError) as entry_exc:
                    logger.error(
                        "Skipping corrupted trailing stop for %s (JSON): %s",
                        symbol, entry_exc,
                    )
            self._trailing_stops = loaded_json
            # Migrate to DB immediately
            self._persist_trailing_stop_state()
            logger.info(
                "Migrated %d trailing stop state(s) from JSON to database",
                len(self._trailing_stops),
            )
        except (json.JSONDecodeError, TypeError, ValueError, OSError) as exc:
            logger.warning("Could not load trailing stop state from JSON: %s", exc)

    def _persist_trailing_stop_state(self) -> None:
        """Persist trailing stop state to database (ACID-safe).

        Also writes the legacy JSON file for backward compatibility.
        """
        try:
            # Write each state to the database
            current_symbols = set(self._trailing_stops.keys())
            db_symbols = set(self.db.get_all_trailing_stops().keys())

            # Upsert current states
            for symbol, state in self._trailing_stops.items():
                self.db.upsert_trailing_stop(
                    symbol=symbol,
                    direction=state.direction,
                    entry_price=state.entry_price,
                    best_price=state.best_price,
                    atr_4h=state.atr_4h,
                    activated=state.activated,
                    strategy_name=state.strategy_name,
                    take_profit=state.take_profit,
                    tp_pending=state.tp_pending,
                )

            # Delete stale entries (positions closed since last persist)
            for symbol in db_symbols - current_symbols:
                self.db.delete_trailing_stop(symbol)

        except Exception as exc:
            logger.error("Failed to persist trailing stops to DB: %s", exc)

        # Legacy JSON for backward compatibility
        try:
            serialized = {
                symbol: state.model_dump()
                for symbol, state in self._trailing_stops.items()
            }
            tmp = self._TRAILING_STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(serialized, indent=2), encoding="utf-8")
            tmp.rename(self._TRAILING_STATE_FILE)
        except OSError as exc:
            logger.warning("Failed to persist trailing stop JSON (non-critical): %s", exc)


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
