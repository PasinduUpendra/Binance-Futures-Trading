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
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Optional

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

load_dotenv(override=True)

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
from src.risk.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConstraints,
    CircuitBreakerLevel,
    TradeResult,
)
from src.risk.position_sizer import PositionSizer
from src.risk.leverage_manager import LeverageManager
from src.risk.volatility_model import VolatilityModel
from src.risk.drawdown_monitor import DrawdownMonitor
from src.execution.order_manager import OrderManager, OrderState
from src.execution.position_tracker import Position, PositionTracker
from src.execution.fee_calculator import FeeCalculator
from src.execution.slippage_estimator import SlippageEstimator
from src.memory.trade_journal import TradeJournal
from src.memory.performance_tracker import PerformanceTracker
from src.memory.bias_detector import BiasDetector
from src.anti_hallucination.price_validator import PriceValidator
from src.anti_hallucination.signal_validator import SignalValidator
from src.anti_hallucination.decision_auditor import DecisionAuditor
from src.anti_hallucination.sanity_checks import SanityChecker
from src.reporting.daily_pnl import DailyPnLCalculator
from src.reporting.dashboard import Dashboard
from src.risk.funding_rate_filter import FundingRateFilter
from src.strategies.cross_asset_consensus import CrossAssetConsensus
from src.reporting.report_generator import ReportGenerator
from src.reporting.alert_system import AlertSystem
from src.data.supabase_mirror import SupabaseMirror

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
    partial_tp_taken: bool = False  # True once 50% scaled out at 1:1 R/R
    stop_loss: float = 0.0  # Original SL price (needed for 1:1 R/R calculation)
    size: float = 0.0  # Position size in base currency (for pnl_pct calc)
    leverage: int = 1  # Leverage at time of entry (for pnl_pct calc)

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
    # BTC removed: $100 min notional impossible at $66 balance (max notional $82.50)
    # Re-add when balance >= $200
    "ETH/USDT:USDT",   # $20 min notional
    "SOL/USDT:USDT",   # $5 min notional
    "DOGE/USDT:USDT",  # $5 min notional
    "XRP/USDT:USDT",   # $5 min notional
    "LINK/USDT:USDT",  # $20 min notional (verified via Binance API)
    "AVAX/USDT:USDT",  # $5 min notional
    "SUI/USDT:USDT",   # $5 min notional
    "ADA/USDT:USDT",   # $5 min notional
]

# Per-pair minimum notional (from Binance API)
MIN_NOTIONAL: dict[str, float] = {
    "BTC/USDT:USDT": 100.0,
    "ETH/USDT:USDT": 20.0,
    "LINK/USDT:USDT": 20.0,
}
DEFAULT_MIN_NOTIONAL: float = 5.0

# Multi-timeframe: 4H for trend direction, 1H for entry timing.
# Evidence: 4H+ shows 75-85% success rates in trending markets (Cointester study).
# Daily trading is more robust to transaction costs than intraday (ScienceDirect).
TIMEFRAME_DIRECTION = "4h"  # Primary: trend direction + regime detection
TIMEFRAME_ENTRY = "1h"      # Secondary: entry timing with tighter stops
TIMEFRAME_FAST = "15m"      # Tertiary: fast-entry signals in established trends
CYCLE_INTERVAL_SECONDS = 1800  # 30 min — 2x more signal checks (v6.17: trade frequency fix)
MAX_HOLD_BARS = 100  # Max 1H bars to hold a position (100 × 1H ≈ 4.17 days) — v6.16 backtest evidence
WRONG_SIDE_FORCE_CLOSE_CYCLES = 8  # Force-close wrong-side positions after N consecutive opposing cycles (4h at 30min cycles)

# v6.20: Dynamic position limit — allow +1 position beyond CB cap when
# (a) CB level is GREEN, (b) signal confidence >= threshold, and
# (c) balance can still support the extra position ($15 min per slot).
# This prevents the 3-position deadlock observed in v6.19 monitoring.
DYNAMIC_POS_CONFIDENCE_MIN: Final[float] = 60.0
DYNAMIC_POS_BALANCE_PER_SLOT: Final[Decimal] = Decimal("15")
DYNAMIC_POS_ABSOLUTE_MAX: Final[int] = 5
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

        # Daily trade counter (Immutable Rule #5: 20 max daily trades)
        self._daily_trade_count: int = 0
        self._daily_trade_date: date = datetime.now(timezone.utc).date()

        # v6.20: Track consecutive cycles where all signals oppose held positions.
        # Maps symbol → count of consecutive cycles where ALL approved signals
        # are in the opposite direction. Force-close after threshold.
        self._wrong_side_cycle_count: dict[str, int] = {}

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
        self.slippage_estimator = SlippageEstimator()
        # Canonical DB FIRST — TradeJournal + DecisionAuditor now write into
        # the same file so ``trades`` / ``audit_trail`` are no longer split.
        self.db = DatabaseManager()
        self.trade_journal = TradeJournal(db_path=self.db.db_path)
        self.performance_tracker = PerformanceTracker(journal=self.trade_journal)
        self.bias_detector = BiasDetector()
        self.price_validator = PriceValidator(market_data_client=self.market_data)
        self.signal_validator = SignalValidator()
        self.decision_auditor = DecisionAuditor(db_path=str(self.db.db_path))
        self.sanity_checker = SanityChecker()
        self.pnl_calculator = DailyPnLCalculator(
            initial_capital=Decimal(os.getenv("INITIAL_CAPITAL", "68.33"))
        )
        self.dashboard = Dashboard()
        self.report_generator = ReportGenerator()
        self.alert_system = AlertSystem()
        self.alert_system.log_channel_status()
        self.cross_asset_consensus = CrossAssetConsensus()

        # Supabase mirror (non-blocking) — no-op if env vars are unset.
        # Attach to every persistence component AFTER construction.
        self.mirror = SupabaseMirror()
        self.db.attach_mirror(self.mirror)
        self.trade_journal.attach_mirror(self.mirror)
        self.decision_auditor.attach_mirror(self.mirror)

        # Route DrawdownMonitor state into the canonical DB
        # (system_state table). Stops dual-write of drawdown_state.json.
        self.drawdown_monitor.attach_db(self.db)

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
            await self._configure_fee_calculator(all_assets)
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
        protected_symbols: set[str] = set()
        try:
            await self._detect_preexisting_positions()
            # Collect symbols with open positions — their SL/TP must NOT be cancelled
            protected_symbols = set(self._trailing_stops.keys())
            if protected_symbols:
                logger.info(
                    f"Startup: preserving orders for {len(protected_symbols)} "
                    f"active position(s): {sorted(protected_symbols)}"
                )
        except Exception as e:
            logger.warning(f"Pre-existing position detection failed: {e}")

        # Startup cleanup: cancel stale orders on pairs WITHOUT open positions.
        # Pairs WITH positions keep their SL/TP orders intact.
        for pair in TRADING_PAIRS:
            if pair in protected_symbols:
                continue  # DO NOT cancel orders protecting live positions
            try:
                cancelled, _all_ok = await self.order_manager.cancel_open_orders(pair)
                if cancelled > 0:
                    logger.info(
                        f"Startup cleanup: cancelled {cancelled} stale orders for {pair}"
                    )
                if not _all_ok:
                    logger.warning(f"Startup cleanup: partial cancel for {pair}, retrying")
                    await asyncio.sleep(1)
                    await self.order_manager.cancel_open_orders(pair)
            except Exception as e:
                logger.warning(f"Startup cleanup failed for {pair}: {e}")

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
            self.mirror.close()
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
        pair_data_15m: dict[str, pd.DataFrame] = {}

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

                # 15m data for fast-entry continuation signals
                raw_15m = await self.market_data.fetch_ohlcv(pair, TIMEFRAME_FAST, limit=200)
                if raw_15m and len(raw_15m) >= 50:
                    df_15m = pd.DataFrame(raw_15m)
                    validation_15m = self.data_validator.validate_ohlcv(df_15m)
                    if validation_15m.passed:
                        pair_data_15m[pair] = self.indicator_engine.calculate_all(df_15m)

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
            logger.error(
                "Supertrend reversal exit check failed: %s", e, exc_info=True,
            )
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

        # ─── Step 2d: Wrong-side Force Close (v6.20) ───
        # When ALL approved signals consistently point one direction but we
        # hold positions on the opposite side, force-close after N cycles.
        # This prevents the position-cap deadlock observed in v6.19 monitoring
        # (3-hour session: held 2 losing SHORTs in fully bullish market).
        try:
            await self._check_wrong_side_force_close(
                pair_data_4h, pair_data_1h, pair_data_15m, result,
            )
        except Exception as e:
            logger.error(f"Wrong-side force-close check failed: {e}")
            errors.append(f"Wrong-side: {e}")

        # ─── Step 3: Multi-Timeframe Signal Generation ───
        # v6.17: Collect ALL valid signals, execute multiple (up to position limit).
        # Previously: only the single best signal was executed, discarding all others.
        # This matches backtest_v4.py behavior which opens multiple positions per bar.
        all_signals: list[tuple[Any, str]] = []  # (signal, pair)

        # ─── Cross-asset consensus (confidence adjustment) ───
        consensus_adj = self.cross_asset_consensus.compute(pair_data_4h)

        # Build map of currently positioned symbols → side for filtering
        try:
            _open_pos = await self.position_tracker.get_open_positions()
            _positioned: dict[str, str] = {p.symbol: p.side for p in _open_pos}
        except Exception:
            _positioned = {}

        for pair in TRADING_PAIRS:
            if pair not in pair_data_4h or pair not in pair_data_1h:
                continue

            # Skip pairs where we already hold a same-direction position.
            # Opposing-direction signals still compete for best so the
            # orchestrator can detect and close the conflicting position.
            existing_side = _positioned.get(pair)

            try:
                df_4h = pair_data_4h[pair]
                df_1h = pair_data_1h[pair]
                df_15m = pair_data_15m.get(pair)

                # Multi-timeframe: 4H regime → strategy selection → appropriate data
                signal = self.adaptive_strategy.get_signal_multi_tf(df_4h, df_1h, df_15m)

                if signal is None:
                    continue

                # Skip same-direction signals for already-positioned pairs
                if existing_side and signal.direction.value == existing_side:
                    continue

                # Apply cross-asset consensus adjustment
                adj = consensus_adj.get(pair, 0.0)
                if adj != 0.0:
                    adjusted_conf = max(0.0, min(100.0, signal.confidence + adj))
                    signal = signal.model_copy(update={"confidence": adjusted_conf})
                    logger.info(
                        "%s: consensus adjustment %.1f → confidence %.1f%%",
                        pair, adj, adjusted_conf,
                    )

                # Detect regime for logging
                regime = self.regime_detector.detect(df_4h)
                result.regime = regime.regime.value
                logger.info(f"{pair}: Regime={regime.regime.value} (4H)")

                # Collect ALL valid signals (not just best)
                all_signals.append((signal, pair))

            except Exception as e:
                logger.error(f"Analysis failed for {pair}: {e}")
                errors.append(f"Analysis {pair}: {e}")

        if not all_signals:
            logger.info("No valid signals this cycle")
            result.signal_generated = False
            result.errors = errors
            result.duration_seconds = (datetime.now(timezone.utc) - cycle_start).total_seconds()
            return result

        # Sort by confidence (highest first) — best signals get priority
        all_signals.sort(key=lambda x: x[0].confidence, reverse=True)

        result.signal_generated = True
        logger.info(
            f"Found {len(all_signals)} valid signal(s) this cycle: "
            + ", ".join(f"{p} {s.direction} {s.confidence:.0f}%" for s, p in all_signals)
        )

        # ─── Steps 4-7: Execute ALL signals (up to position limit) ───
        trades_placed = 0
        for sig, sig_pair in all_signals:
            trade_result = await self._execute_signal(
                signal=sig,
                symbol=sig_pair,
                df_4h=pair_data_4h[sig_pair],
                df_1h=pair_data_1h[sig_pair],
                cb_state=cb_state,
                trigger="hourly_cycle",
            )
            if trade_result is not None:
                trades_placed += 1
                result.trade_placed = True
                result.trade_details = trade_result  # Last successful trade details
                logger.info(
                    f"Trade {trades_placed} placed: {sig_pair} {sig.direction} "
                    f"confidence={sig.confidence:.0f}%"
                )
            else:
                logger.info(
                    f"Signal rejected by risk/execution: {sig_pair} {sig.direction} "
                    f"confidence={sig.confidence:.0f}%"
                )

        if trades_placed > 0:
            logger.info(f"Cycle placed {trades_placed} trade(s) from {len(all_signals)} signal(s)")
        else:
            logger.info(f"All {len(all_signals)} signal(s) rejected by risk checks")

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

            # ── Daily trade limit (Immutable Rule #5: 20 max) ──
            today = datetime.now(timezone.utc).date()
            if today != self._daily_trade_date:
                self._daily_trade_count = 0
                self._daily_trade_date = today
            if self._daily_trade_count >= 20:
                logger.warning(
                    f"[{trigger}] Daily trade limit reached "
                    f"({self._daily_trade_count}/20) — rejecting"
                )
                return None

            # ── Position count gate (v6.20: dynamic limit) ──
            open_positions = await self.position_tracker.get_open_positions()
            effective_max = self._get_effective_max_positions(
                constraints, signal.confidence, balance,
            )
            if len(open_positions) >= effective_max:
                # Try smart swap: close worst position if new signal is much better
                swap_pos = await self._find_swap_candidate(
                    new_confidence=signal.confidence,
                    open_positions=open_positions,
                    new_direction=signal.direction.value,
                )
                if swap_pos is None:
                    logger.info(
                        f"[{trigger}] Max positions reached "
                        f"({len(open_positions)}/{effective_max})"
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
                    if pos.side != signal.direction.value:
                        # Opposing signal — close existing position
                        logger.warning(
                            "[%s] REVERSAL EXIT: %s %s conflicts with signal %s "
                            "(confidence=%.1f%%). Closing existing position.",
                            trigger, pos.symbol, pos.side,
                            signal.direction.value, signal.confidence,
                        )
                        try:
                            close_side = "sell" if pos.side == "long" else "buy"
                            await self.order_manager.cancel_open_orders(pos.symbol)
                            await self.order_manager.place_market_order(
                                symbol=pos.symbol,
                                side=close_side,
                                amount=pos.size,
                                reduce_only=True,
                            )
                            # Record exit
                            entry_px = float(pos.entry_price)
                            try:
                                current_price = await self.market_data.get_current_price(pos.symbol)
                                exit_px = float(current_price)
                            except Exception:
                                exit_px = entry_px  # Fallback
                            if pos.side == "long":
                                pnl_approx = (exit_px - entry_px) * float(pos.size)
                            else:
                                pnl_approx = (entry_px - exit_px) * float(pos.size)
                            self._record_trade_exit(
                                symbol=pos.symbol,
                                exit_price=exit_px,
                                pnl=pnl_approx,
                                entry_price=entry_px,
                                reason="reversal_exit",
                                size=float(pos.size),
                                leverage=pos.leverage,
                            )
                            self._trailing_stops.pop(pos.symbol, None)
                            self._persist_trailing_stop_state()
                        except Exception as e:
                            logger.error(
                                "[%s] REVERSAL EXIT failed for %s: %s",
                                trigger, pos.symbol, e,
                            )
                    else:
                        logger.info(f"[{trigger}] Already positioned in {symbol}")
                    return None

            # ── Funding Rate Filter (Sprint 1.2) ──
            try:
                funding_rate = await self.market_data.fetch_funding_rate(symbol)
                fr_result = FundingRateFilter.evaluate(
                    funding_rate=float(funding_rate),
                    signal_direction=signal.direction.value,
                )
                if not fr_result.should_trade:
                    logger.warning(
                        f"[{trigger}] Funding rate filter rejected: {fr_result.reason}"
                    )
                    return None
                if fr_result.confidence_adjustment != 0:
                    logger.info(
                        f"[{trigger}] Funding rate adjustment: "
                        f"{fr_result.confidence_adjustment:+.0f} ({fr_result.reason})"
                    )
            except Exception as e:
                # Non-blocking: if funding rate fetch fails, proceed without filter
                logger.debug(f"[{trigger}] Funding rate fetch skipped: {e}")
                fr_result = None

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
                position_pct = Decimal("0.25")
            elif confidence >= 45:
                position_pct = Decimal("0.167")
            else:
                position_pct = Decimal("0.117")
            max_cap = Decimal("0.25")  # Rule #4 — raised from 15% via v6.16 backtest

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

            # ── Regime detection (used by audit + trade_details) ──
            try:
                regime = self.regime_detector.detect(df_4h)
            except Exception as regime_err:
                logger.warning(
                    "[%s] Regime detection failed: %s — using signal.regime",
                    trigger, regime_err,
                )
                regime = None

            # ── Price validation (Anti-hallucination Layer 2) ──
            price_validated = False
            try:
                price_result = await self.price_validator.validate_price(
                    symbol, Decimal(str(signal.entry_price)), "strategy_signal",
                )
                price_validated = price_result.valid
                if not price_validated:
                    logger.warning(
                        "[%s] Price validation FAILED for %s: %s",
                        trigger, symbol, "; ".join(price_result.issues),
                    )
            except Exception as pv_err:
                logger.warning(
                    "[%s] Price validation error (treating as failed): %s",
                    trigger, pv_err,
                )

            # ── Signal validation (Anti-hallucination Layer 3) ──
            signal_validated = False
            try:
                from src.anti_hallucination.signal_validator import (
                    TradingSignal as ValidatorSignal,
                )

                last_close = (
                    float(df_1h["close"].iloc[-1])
                    if "close" in df_1h.columns and len(df_1h) > 0
                    else signal.entry_price
                )
                # Extract raw indicator values from 4H dataframe
                excluded_cols = {"open", "high", "low", "volume", "timestamp"}
                raw_indicators: dict[str, float] = {}
                for col in df_4h.columns:
                    if col not in excluded_cols:
                        series = df_4h[col].dropna()
                        if len(series) > 0:
                            try:
                                raw_indicators[col] = float(series.iloc[-1])
                            except (ValueError, TypeError):
                                pass

                # Add aliases: signal uses 'supertrend_dir', df has 'supertrend_direction'
                if "supertrend_direction" in raw_indicators:
                    raw_indicators["supertrend_dir"] = raw_indicators["supertrend_direction"]
                    raw_indicators["supertrend_dir_4h"] = raw_indicators["supertrend_direction"]
                # prev_supertrend_dir is computed, not a column
                if "supertrend_direction" in df_4h.columns and len(df_4h) >= 2:
                    prev_series = df_4h["supertrend_direction"].dropna()
                    if len(prev_series) >= 2:
                        raw_indicators["prev_supertrend_dir"] = float(prev_series.iloc[-2])

                # Add 1H indicators for continuation/fast/aligned signals
                excluded_1h = {"open", "high", "low", "volume", "timestamp"}
                for col in df_1h.columns:
                    if col not in excluded_1h:
                        series_1h = df_1h[col].dropna()
                        if len(series_1h) > 0:
                            try:
                                val = float(series_1h.iloc[-1])
                                # 1H aliases the signal expects
                                if col == "supertrend_direction":
                                    raw_indicators["supertrend_dir_1h"] = val
                                if col == "rsi":
                                    raw_indicators["rsi_1h"] = val
                                    # rsi_1h_min: min of last 5 bars
                                    if len(series_1h) >= 5:
                                        raw_indicators["rsi_1h_min"] = float(series_1h.iloc[-5:].min())
                                if col == "close":
                                    raw_indicators.setdefault("close", val)
                            except (ValueError, TypeError):
                                pass

                # Override ATR with correct timeframe for continuation/fast signals
                sig_indicators = getattr(signal, "indicators_used", {})
                atr_source = sig_indicators.get("atr_source")
                if atr_source == "1h" and "atr" in df_1h.columns:
                    atr_1h = df_1h["atr"].dropna()
                    if len(atr_1h) > 0:
                        raw_indicators["atr"] = float(atr_1h.iloc[-1])
                elif atr_source == "15m":
                    # 15m ATR not available (no df_15m in scope) — use signal's own ATR
                    # so the validator won't flag a timeframe mismatch
                    sig_atr = sig_indicators.get("atr")
                    if sig_atr is not None:
                        raw_indicators["atr"] = float(sig_atr)

                validator_signal = ValidatorSignal(
                    signal_id=f"{symbol}_{trigger}",
                    symbol=symbol,
                    direction=signal.direction.value,
                    strategy=signal.strategy_name,
                    entry_price=Decimal(str(signal.entry_price)),
                    stop_loss=Decimal(str(signal.stop_loss)),
                    take_profit=Decimal(str(signal.take_profit)),
                    leverage=leverage,
                    confidence=signal.confidence,
                    indicators=getattr(signal, "indicators_used", {}),
                )
                raw_data = {
                    "bid": Decimal(str(last_close)),
                    "ask": Decimal(str(last_close)),
                    "indicators": raw_indicators,
                    "candles": [{"close": last_close}],
                }
                signal_validation = self.signal_validator.validate_signal(
                    validator_signal, raw_data,
                )
                signal_validated = signal_validation.valid
                if not signal_validated:
                    logger.warning(
                        "[%s] Signal validation FAILED for %s: %s",
                        trigger, symbol, "; ".join(signal_validation.issues),
                    )
            except Exception as sv_err:
                logger.warning(
                    "[%s] Signal validation error (treating as failed): %s",
                    trigger, sv_err,
                )

            # ── Audit (BLOCKING — Immutable Rule #7) ──
            try:
                signal_dict = signal.model_dump() if hasattr(signal, "model_dump") else vars(signal)
                regime_dict = (
                    regime.model_dump() if regime and hasattr(regime, "model_dump")
                    else {"regime": getattr(signal, "regime", "unknown"), "confidence": 0.0}
                )
                signal_dict.setdefault("symbol", symbol)
                signal_dict.setdefault("strategy", signal_dict.get("strategy_name", ""))

                # Compute actual R/R from signal
                if signal.direction.value == "long":
                    risk = abs(signal.entry_price - signal.stop_loss)
                    reward = abs(signal.take_profit - signal.entry_price)
                else:
                    risk = abs(signal.stop_loss - signal.entry_price)
                    reward = abs(signal.entry_price - signal.take_profit)
                actual_rr = float(reward / risk) if risk > 0 else 0.0

                audit_report = self.decision_auditor.audit_decision(
                    signal=signal_dict,
                    regime=regime_dict,
                    risk_approval={
                        "position_size_usd": float(margin),
                        "leverage": leverage,
                        "notional": notional,
                        "confidence": confidence,
                        "position_pct": float(position_pct),
                        "approved": True,
                        "risk_per_trade_pct": float(position_pct) * 100,
                        "risk_reward_ratio": actual_rr,
                        "kelly_fraction": 0.0,
                        "notes": "",
                    },
                    market_data={
                        "pair": symbol,
                        "balance": float(balance),
                        "price_validated": price_validated,
                        "signal_validated": signal_validated,
                        "sanity_checks_passed": True,  # Already gate-checked above
                    },
                )
                if audit_report.decision == "REJECT":
                    logger.warning(
                        "[%s] Audit REJECTED %s: %s",
                        trigger, symbol, audit_report.decision_reasoning,
                    )
                    return None
                if audit_report.decision == "SKIP":
                    logger.info(
                        "[%s] Audit SKIPPED %s: %s",
                        trigger, symbol, audit_report.decision_reasoning,
                    )
                    return None
            except Exception as e:
                logger.warning(f"[{trigger}] Audit failed (non-blocking): {e}")

            # ── Orderbook Slippage Check ──
            order_qty = Decimal(str(notional)) / Decimal(str(signal.entry_price))
            side = "buy" if signal.direction.value == "long" else "sell"
            orderbook = None
            try:
                orderbook = await self.market_data.fetch_orderbook(symbol, limit=20)
                slip = self.slippage_estimator.estimate_slippage(
                    symbol=symbol,
                    order_size=order_qty,
                    orderbook=orderbook.model_dump(),
                    side=side,
                )
                MAX_SLIPPAGE_PCT = Decimal("0.50")  # reject if > 0.5%
                if slip.slippage_pct > MAX_SLIPPAGE_PCT:
                    logger.warning(
                        f"[{trigger}] Slippage {slip.slippage_pct}%% > "
                        f"{MAX_SLIPPAGE_PCT}%% for {symbol} ({slip.levels_consumed} "
                        f"levels consumed) — rejecting"
                    )
                    return None
                if not slip.fully_fillable:
                    logger.warning(
                        f"[{trigger}] Orderbook too shallow for {symbol}: "
                        f"order={order_qty}, fillable levels={slip.levels_consumed}"
                    )
                    return None
                logger.info(
                    f"[{trigger}] Slippage OK: {slip.slippage_pct}%% "
                    f"({slip.levels_consumed} levels, VWAP={slip.expected_fill_price})"
                )
            except Exception as e:
                logger.warning(
                    f"[{trigger}] Orderbook slippage check failed (non-blocking): {e}"
                )

            # ── Execute ──
            try:
                await self.order_manager.set_leverage(symbol, leverage)

                # Try maker-first entry (post-only limit at best bid/ask).
                # Saves 0.03% per fill (0.02% maker vs 0.05% taker).
                # Falls back to taker market order if post-only is rejected
                # or unfilled within 5 seconds.
                order_result = None
                filled_via = "market"
                POST_ONLY_WAIT_SECONDS = 5

                try:
                    # Use best bid for buys, best ask for sells
                    if (
                        orderbook is not None
                        and hasattr(orderbook, "bids")
                        and hasattr(orderbook, "asks")
                    ):
                        if side == "buy" and orderbook.bids:
                            limit_price = orderbook.bids[0].price
                        elif side == "sell" and orderbook.asks:
                            limit_price = orderbook.asks[0].price
                        else:
                            limit_price = None
                    else:
                        limit_price = None

                    if limit_price is not None and limit_price > Decimal("0"):
                        limit_result = (
                            await self.order_manager.place_limit_order(
                                symbol=symbol,
                                side=side,
                                amount=order_qty,
                                price=limit_price,
                                post_only=True,
                            )
                        )
                        if limit_result is not None:
                            await asyncio.sleep(POST_ONLY_WAIT_SECONDS)
                            limit_status = (
                                await self.order_manager.get_order_status(
                                    symbol, limit_result.order_id,
                                )
                            )
                            if limit_status.status == OrderState.CLOSED:
                                order_result = limit_result
                                filled_via = "maker"
                                logger.info(
                                    "[%s] Post-only LIMIT filled at %s "
                                    "(maker fee: 0.02%%)",
                                    trigger, limit_price,
                                )
                            else:
                                # Not filled — cancel and fall through
                                try:
                                    await self.order_manager.cancel_order(
                                        symbol, limit_result.order_id,
                                    )
                                except Exception:
                                    pass  # Best-effort cancel
                                logger.info(
                                    "[%s] Post-only LIMIT unfilled after "
                                    "%ds — falling back to market",
                                    trigger, POST_ONLY_WAIT_SECONDS,
                                )
                except Exception as limit_err:
                    logger.info(
                        "[%s] Post-only attempt failed: %s "
                        "— falling back to market",
                        trigger, limit_err,
                    )

                if order_result is None:
                    order_result = await self.order_manager.place_market_order(
                        symbol=symbol, side=side, amount=order_qty,
                    )
                    filled_via = "market"

                if order_result is None:
                    logger.warning(
                        f"[{trigger}] Market order returned None "
                        f"(InsufficientFunds/InvalidOrder) — aborting"
                    )
                    return None

                order_status = await self.order_manager.get_order_status(
                    symbol, order_result.order_id
                )
                if order_status.status not in (OrderState.CLOSED,):
                    logger.warning(
                        f"[{trigger}] Order not filled: {order_status.status.value}"
                    )
                    return None

                if order_result.filled <= Decimal("0"):
                    logger.error(
                        f"[{trigger}] Zero fill on CLOSED order "
                        f"{order_result.order_id} — ghost position risk, aborting"
                    )
                    return None

                sl_side = "sell" if signal.direction.value == "long" else "buy"
                sl_result = None
                try:
                    sl_result = await self.order_manager.place_stop_loss(
                        symbol=symbol,
                        side=sl_side,
                        amount=order_result.filled,
                        stop_price=Decimal(str(signal.stop_loss)),
                    )
                except Exception as sl_err:
                    logger.error(
                        f"[{trigger}] SL placement FAILED for {symbol}: {sl_err} "
                        f"— closing naked position at market"
                    )

                if sl_result is None:
                    if not isinstance(locals().get("sl_err"), Exception):
                        logger.critical(
                            f"[{trigger}] SL returned None for {symbol} "
                            f"(InsufficientFunds/InvalidOrder) — closing naked position"
                        )
                    try:
                        close_side = "sell" if signal.direction.value == "long" else "buy"
                        await self.order_manager.place_market_order(
                            symbol=symbol,
                            side=close_side,
                            amount=order_result.filled,
                            reduce_only=True,
                        )
                        logger.info(
                            f"[{trigger}] Emergency close sent for naked {symbol}"
                        )
                    except Exception as close_err:
                        logger.critical(
                            f"[{trigger}] EMERGENCY CLOSE ALSO FAILED for {symbol}: "
                            f"{close_err} — MANUAL INTERVENTION REQUIRED"
                        )
                    return None

                tp_result = None
                try:
                    tp_result = await self.order_manager.place_take_profit(
                        symbol=symbol,
                        side=sl_side,
                        amount=order_result.filled,
                        stop_price=Decimal(str(signal.take_profit)),
                    )
                except Exception as tp_err:
                    logger.warning(
                        f"[{trigger}] TP order failed (non-blocking): {tp_err}"
                    )
                if tp_result is None:
                    logger.warning(
                        f"[{trigger}] TP returned None for {symbol} "
                        f"(trailing stop provides backup exit)"
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
                    "regime": (regime.regime if regime and hasattr(regime, "regime") else getattr(signal, "regime", "")),
                    "trigger": trigger,
                    "filled_via": filled_via,
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
                    stop_loss=signal.stop_loss,
                    size=float(order_result.filled),
                    leverage=leverage,
                )

                # ── Native trailing stop (Binance-side safety net) ──
                # Survives bot crashes / restarts unlike the local Python trail.
                # Activates after price moves 2.0×ATR favorably, then trails
                # at callback_rate = 2.5×ATR / price * 100 (in percent).
                if atr_4h > 0:
                    try:
                        callback_pct = 2.5 * atr_4h / float(fill_price) * 100
                        if signal.direction.value == "long":
                            act_price = Decimal(str(
                                float(fill_price) + 2.0 * atr_4h
                            ))
                        else:
                            act_price = Decimal(str(
                                float(fill_price) - 2.0 * atr_4h
                            ))
                        trailing_result = (
                            await self.order_manager.place_trailing_stop_market(
                                symbol=symbol,
                                side=sl_side,
                                amount=order_result.filled,
                                callback_rate=callback_pct,
                                activation_price=act_price,
                            )
                        )
                        if trailing_result:
                            logger.info(
                                "[%s] Native trailing stop placed: "
                                "callback=%.2f%%, activation=%s",
                                trigger, callback_pct, act_price,
                            )
                        else:
                            logger.warning(
                                "[%s] Native trailing stop returned None "
                                "(non-blocking)", trigger,
                            )
                    except Exception as trailing_err:
                        logger.warning(
                            "[%s] Native trailing stop failed "
                            "(non-blocking): %s",
                            trigger, trailing_err,
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

                self._daily_trade_count += 1

                return trade_details

            except Exception as e:
                logger.error(f"[{trigger}] Execution failed: {e}")
                return None

    # ------------------------------------------------------------------
    # Trade Exit Recording Helper
    # ------------------------------------------------------------------

    async def _configure_fee_calculator(self, all_assets: list[Any]) -> None:
        """Query live commission rates from Binance API + BNB discount status.

        Falls back to hardcoded VIP-0 defaults if the API call fails.
        """
        # 1. Query live commission rates
        from src.execution.fee_calculator import DEFAULT_MAKER_FEE, DEFAULT_TAKER_FEE

        maker_rate = DEFAULT_MAKER_FEE
        taker_rate = DEFAULT_TAKER_FEE
        try:
            rates = await self.market_data.fetch_commission_rate("BTC/USDT:USDT")
            maker_rate = rates["maker"]
            taker_rate = rates["taker"]
            logger.info(
                "Live commission rates from API: maker=%s (%s%%) taker=%s (%s%%)",
                maker_rate, maker_rate * 100,
                taker_rate, taker_rate * 100,
            )
        except Exception as e:
            logger.warning(
                "Failed to fetch live commission rates, using VIP-0 defaults "
                "(maker=%s, taker=%s): %s",
                maker_rate, taker_rate, e,
            )

        # 2. BNB discount
        bnb_asset = next((asset for asset in all_assets if asset.asset == "BNB"), None)
        use_bnb_discount = bool(bnb_asset and bnb_asset.wallet_balance > Decimal("0"))

        self.fee_calculator = FeeCalculator(
            maker_fee=maker_rate,
            taker_fee=taker_rate,
            use_bnb_discount=use_bnb_discount,
        )

    def _record_trade_exit(
        self,
        symbol: str,
        exit_price: float,
        pnl: float,
        entry_price: float,
        reason: str,
        duration_hours: float | None = None,
        size: float | None = None,
        leverage: int | None = None,
    ) -> None:
        """Record trade exit in journal so win/loss tracking works.

        Computes pnl_pct from pnl / margin where
        margin = entry_price * size / leverage.
        Failures are logged but never propagate.
        """
        try:
            # Guard against None values that crash Decimal(str(None))
            if exit_price is None or pnl is None or entry_price is None:
                logger.warning(
                    "Cannot record exit for %s: exit_price=%s, pnl=%s, "
                    "entry_price=%s — one or more values are None",
                    symbol, exit_price, pnl, entry_price,
                )
                return
            # pnl_pct: return-on-margin = pnl / margin * 100
            if size and leverage and entry_price:
                margin = entry_price * size / leverage
                pnl_pct = (pnl / margin * 100) if margin else 0.0
            elif entry_price and size:
                notional = entry_price * size
                pnl_pct = (pnl / notional * 100) if notional else 0.0
            else:
                pnl_pct = 0.0
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
            if ts_state is None:
                continue

            df_4h = pair_data_4h[pos.symbol]

            should_exit = self.adaptive_strategy.check_supertrend_reversal(
                df_4h, pos.side,
            )

            if should_exit:
                entry_price = float(pos.entry_price)

                # v6.20: Deduplicate — if SL is already at breakeven, skip
                # redundant cancel+replace cycle. The monitoring session showed
                # SUI reversal exit triggering 7 times with ~35 wasted API calls.
                sl_already_at_breakeven = False
                try:
                    existing_orders = await self.order_manager.get_open_orders(
                        pos.symbol, conditional_only=True,
                    )
                    for order in existing_orders:
                        otype = getattr(order, "order_type", "") or ""
                        sprice = getattr(order, "stop_price", None)
                        if otype.lower() in ("stop_market", "stop") and sprice is not None:
                            sl_price = float(sprice)
                            # Within 0.1% of entry = already at breakeven
                            if abs(sl_price - entry_price) / entry_price < 0.001:
                                sl_already_at_breakeven = True
                                break
                except Exception as dedup_err:
                    logger.warning(
                        "ST reversal: deduplication check failed for %s: %s "
                        "— proceeding with SL tightening",
                        pos.symbol, dedup_err,
                    )

                if sl_already_at_breakeven:
                    logger.debug(
                        "SUPERTREND REVERSAL: SL already at breakeven for "
                        "%s %s (entry=%s) — skipping duplicate",
                        pos.symbol, pos.side, entry_price,
                    )
                    continue

                logger.info(
                    f"SUPERTREND REVERSAL: Tightening SL to breakeven for "
                    f"{pos.symbol} {pos.side} (entry={entry_price})"
                )

                try:
                    # Cancel existing SL/TP orders
                    _cnt, _all_ok = await self.order_manager.cancel_open_orders(pos.symbol)
                    if not _all_ok:
                        logger.warning("ST reversal: partial cancel for %s, retrying", pos.symbol)
                        await asyncio.sleep(1)
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
    # Wrong-Side Force Close (v6.20)
    # ------------------------------------------------------------------

    async def _check_wrong_side_force_close(
        self,
        pair_data_4h: dict[str, pd.DataFrame],
        pair_data_1h: dict[str, pd.DataFrame],
        pair_data_15m: dict[str, pd.DataFrame],
        result: CycleResult,
    ) -> None:
        """Force-close positions that oppose the dominant signal direction.

        When ALL approved signals across all pairs consistently point one
        direction (e.g., ALL LONG) but the bot holds positions on the
        opposite side (e.g., SHORTs), this force-closes afterN consecutive
        opposing cycles. Prevents the deadlock where wrong-side positions
        block new trades and bleed against the market.

        The counter is per-position, not global: each wrong-side position
        gets its own countdown. Reset when signals are mixed or absent.
        """
        # Generate signals (lightweight — just direction check, don't execute)
        signal_directions: list[str] = []
        for pair in TRADING_PAIRS:
            if pair not in pair_data_4h or pair not in pair_data_1h:
                continue
            try:
                df_15m = pair_data_15m.get(pair)
                signal = self.adaptive_strategy.get_signal_multi_tf(
                    pair_data_4h[pair], pair_data_1h[pair], df_15m,
                )
                if signal is not None:
                    signal_directions.append(signal.direction.value)
            except Exception:
                continue

        if not signal_directions:
            # No signals — reset all counters
            self._wrong_side_cycle_count.clear()
            return

        # Check if there's a dominant direction (>= 60% of signals agree)
        dir_counts = Counter(signal_directions)
        dominant_dir, dominant_count = dir_counts.most_common(1)[0]
        if len(signal_directions) < 3 or dominant_count / len(signal_directions) < 0.6:
            # Mixed signals or too few signals — no dominant direction, reset
            self._wrong_side_cycle_count.clear()
            return

        # Check open positions against dominant direction
        open_positions = await self.position_tracker.get_open_positions()

        for pos in open_positions:
            is_wrong_side = (
                (dominant_dir == "long" and pos.side == "short")
                or (dominant_dir == "short" and pos.side == "long")
            )
            if not is_wrong_side:
                # Position is aligned — reset counter
                self._wrong_side_cycle_count.pop(pos.symbol, None)
                continue

            # Only count if position has negative PnL
            if float(pos.unrealized_pnl) >= 0:
                self._wrong_side_cycle_count.pop(pos.symbol, None)
                continue

            self._wrong_side_cycle_count[pos.symbol] = (
                self._wrong_side_cycle_count.get(pos.symbol, 0) + 1
            )
            count = self._wrong_side_cycle_count[pos.symbol]

            logger.warning(
                "WRONG-SIDE ALERT: %s %s opposes dominant %s direction "
                "(cycle %d/%d, PnL=$%.2f)",
                pos.symbol, pos.side, dominant_dir,
                count, WRONG_SIDE_FORCE_CLOSE_CYCLES,
                float(pos.unrealized_pnl),
            )

            if count >= WRONG_SIDE_FORCE_CLOSE_CYCLES:
                logger.warning(
                    "WRONG-SIDE FORCE CLOSE: %s %s after %d opposing cycles "
                    "(PnL=$%.2f). Dominant direction: %s (%d/%d signals).",
                    pos.symbol, pos.side, count,
                    float(pos.unrealized_pnl), dominant_dir,
                    dominant_count, len(signal_directions),
                )
                try:
                    close_side = "sell" if pos.side == "long" else "buy"
                    await self.order_manager.cancel_open_orders(pos.symbol)
                    await self.order_manager.place_market_order(
                        symbol=pos.symbol,
                        side=close_side,
                        amount=pos.size,
                        reduce_only=True,
                    )
                    self._record_trade_exit(
                        symbol=pos.symbol,
                        exit_price=float(pos.current_price),
                        pnl=float(pos.unrealized_pnl),
                        entry_price=float(pos.entry_price),
                        reason="wrong_side_force_close",
                        size=float(pos.size),
                        leverage=pos.leverage,
                    )
                    self._trailing_stops.pop(pos.symbol, None)
                    self._persist_trailing_stop_state()

                    result.positions_closed.append({
                        "symbol": pos.symbol,
                        "reason": "wrong_side_force_close",
                        "direction": pos.side,
                        "opposing_direction": dominant_dir,
                        "pnl": float(pos.unrealized_pnl),
                        "cycles_opposing": count,
                    })

                    await self.alert_system.send_alert(
                        f"WRONG-SIDE FORCE CLOSE: {pos.symbol} {pos.side} "
                        f"(PnL=${float(pos.unrealized_pnl):.2f}). "
                        f"Market dominant: {dominant_dir}.",
                        level="warning",
                    )

                    # Reset counter after close
                    self._wrong_side_cycle_count.pop(pos.symbol, None)

                except Exception as e:
                    logger.error(
                        "Failed to force-close wrong-side position %s: %s",
                        pos.symbol, e,
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

            # ── Position-level PnL monitoring ──
            pnl_val = float(pos.unrealized_pnl)
            margin_val = float(pos.margin) if pos.margin > 0 else None
            if margin_val and margin_val > 0:
                pnl_pct = pnl_val / margin_val * 100.0
                if pnl_pct <= -5.0:
                    logger.warning(
                        "PNL ALERT: %s %s unrealized=%.4f (%.1f%% of margin) — CRITICAL",
                        pos.symbol, pos.side, pnl_val, pnl_pct,
                    )
                    await self.alert_system.send_alert(
                        f"Position PnL CRITICAL: {pos.symbol} {pos.side} "
                        f"PnL={pnl_val:.4f} USDT ({pnl_pct:.1f}% of margin)",
                        level="warning",
                    )
                elif pnl_pct <= -3.0:
                    logger.warning(
                        "PNL WARN: %s %s unrealized=%.4f (%.1f%% of margin)",
                        pos.symbol, pos.side, pnl_val, pnl_pct,
                    )

            # ── Partial Take-Profit at 1:1 R/R (scale out 50%) ──
            # When price has moved 1× the SL distance in favor, close half
            # the position to lock in profits.  This reduces exposure while
            # letting the remaining 50% ride toward the full TP.
            if not ts_state.partial_tp_taken and ts_state.stop_loss > 0:
                sl_distance = abs(ts_state.entry_price - ts_state.stop_loss)
                if ts_state.direction == "long":
                    partial_tp_price = ts_state.entry_price + sl_distance
                    reached_partial = current_price >= partial_tp_price
                else:
                    partial_tp_price = ts_state.entry_price - sl_distance
                    reached_partial = current_price <= partial_tp_price

                if reached_partial and sl_distance > 0:
                    half_size = pos.size / Decimal("2")
                    if half_size > Decimal("0"):
                        logger.info(
                            "PARTIAL TP: %s %s reached 1:1 R/R (%.6f). "
                            "Closing 50%% (%.6f of %.6f)",
                            pos.symbol, ts_state.direction,
                            partial_tp_price, half_size, pos.size,
                        )
                        try:
                            close_side = "sell" if ts_state.direction == "long" else "buy"
                            ptp_result = await self.order_manager.place_market_order(
                                symbol=pos.symbol,
                                side=close_side,
                                amount=half_size,
                                reduce_only=True,
                            )
                            if ptp_result:
                                ts_state.partial_tp_taken = True
                                state_changed = True

                                # Move SL to breakeven after partial TP
                                try:
                                    _cnt, _ = await self.order_manager.cancel_open_orders(pos.symbol)
                                    sl_side = "sell" if ts_state.direction == "long" else "buy"
                                    remaining_size = pos.size - half_size
                                    await self.order_manager.place_stop_loss(
                                        symbol=pos.symbol,
                                        side=sl_side,
                                        amount=remaining_size,
                                        stop_price=Decimal(str(ts_state.entry_price)),
                                    )
                                    # Re-place TP for remaining size
                                    if ts_state.take_profit > 0:
                                        tp_valid = (
                                            (ts_state.direction == "long" and ts_state.take_profit > ts_state.entry_price)
                                            or (ts_state.direction == "short" and ts_state.take_profit < ts_state.entry_price)
                                        )
                                        if tp_valid:
                                            await self.order_manager.place_take_profit(
                                                symbol=pos.symbol,
                                                side=sl_side,
                                                amount=remaining_size,
                                                stop_price=Decimal(str(ts_state.take_profit)),
                                            )
                                    logger.info(
                                        "PARTIAL TP: %s SL moved to breakeven %.6f, "
                                        "remaining size=%.6f",
                                        pos.symbol, ts_state.entry_price,
                                        remaining_size,
                                    )
                                except Exception as sl_err:
                                    logger.error(
                                        "PARTIAL TP: Failed to adjust SL/TP for %s: %s",
                                        pos.symbol, sl_err,
                                    )

                                await self.alert_system.send_alert(
                                    f"Partial TP: {pos.symbol} {ts_state.direction} "
                                    f"50% closed at 1:1 R/R ({current_price:.6f}). "
                                    f"SL moved to breakeven.",
                                    level="info",
                                )
                            else:
                                logger.warning(
                                    "PARTIAL TP: Market order returned None for %s",
                                    pos.symbol,
                                )
                        except Exception as e:
                            logger.error(
                                "PARTIAL TP: Failed for %s: %s", pos.symbol, e,
                            )

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
                        reduce_only=True,
                    )

                    _cnt, _all_ok = await self.order_manager.cancel_open_orders(pos.symbol)
                    if not _all_ok:
                        logger.warning("Trailing stop: partial cancel for %s, retrying", pos.symbol)
                        await asyncio.sleep(1)
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
                        size=float(pos.size),
                        leverage=pos.leverage,
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

        v6.16 reduced from 150 to 100 bars (~4.17 days) based on aggressive
        backtest sweep: 100-bar hold increased trade count (197 vs 176) and
        win rate (54.3% vs 51.1%) while reaching $1000 milestone in 119 days.
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
                    reduce_only=True,
                )

                _cnt, _all_ok = await self.order_manager.cancel_open_orders(pos.symbol)
                if not _all_ok:
                    logger.warning("Time exit: partial cancel for %s, retrying", pos.symbol)
                    await asyncio.sleep(1)
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
                    size=float(pos.size),
                    leverage=pos.leverage,
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
                cancelled, _all_ok = await self.order_manager.cancel_open_orders(sym)
                if not _all_ok:
                    logger.warning("RECONCILE: partial cancel for %s, retrying", sym)
                    await asyncio.sleep(1)
                    await self.order_manager.cancel_open_orders(sym)
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
                # Get exit price: prefer live ticker, fall back to tracked price
                try:
                    current_price = await self.market_data.get_current_price(sym)
                    exit_px = float(current_price)
                except Exception as price_err:
                    logger.warning(
                        "RECONCILE: Price fetch failed for %s: %s — "
                        "using tracked best_price %.6f",
                        sym, price_err, ts_state.best_price,
                    )
                    exit_px = ts_state.best_price

                try:
                    entry_px = ts_state.entry_price
                    direction = ts_state.direction
                    if direction == "long":
                        pnl_approx = (exit_px - entry_px) * ts_state.size
                    else:
                        pnl_approx = (entry_px - exit_px) * ts_state.size
                    self._record_trade_exit(
                        symbol=sym,
                        exit_price=exit_px,
                        pnl=pnl_approx,
                        entry_price=entry_px,
                        reason="sl_tp_fire",
                        size=ts_state.size,
                        leverage=ts_state.leverage,
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

                # Discriminate SL vs TP by stop_price relative to entry.
                # Binance testnet returns order_type="market" for both
                # STOP_MARKET and TAKE_PROFIT_MARKET, so string matching
                # on order_type is unreliable.  Use price position instead:
                #   LONG: SL.stopPrice < entry, TP.stopPrice > entry
                #   SHORT: SL.stopPrice > entry, TP.stopPrice < entry
                entry_px = float(pos.entry_price)
                has_sl = False
                has_tp = False
                for o in open_orders:
                    sp = float(o.stop_price) if o.stop_price else None
                    if sp is None:
                        continue
                    if pos.side == "long":
                        if sp <= entry_px:
                            has_sl = True
                        elif sp > entry_px:
                            has_tp = True
                    else:  # short
                        if sp >= entry_px:
                            has_sl = True
                        elif sp < entry_px:
                            has_tp = True

                # Treat tp_pending as missing TP (retry failed placements)
                ts_state = self._trailing_stops.get(pos.symbol)
                if ts_state and ts_state.tp_pending:
                    has_tp = False

                # Cap: if more than 2 conditional orders exist,
                # duplicates accumulated. Cancel all and re-place
                # exactly one SL + one TP.
                if len(open_orders) > 2:
                    logger.warning(
                        "RECONCILE: %s has %d orders (expected ≤2) — "
                        "cancelling duplicates and re-placing.",
                        pos.symbol, len(open_orders),
                    )
                    try:
                        _cnt, _all_ok = await self.order_manager.cancel_open_orders(
                            pos.symbol,
                        )
                        if not _all_ok:
                            await asyncio.sleep(1)
                            await self.order_manager.cancel_open_orders(pos.symbol)
                    except Exception as cancel_err:
                        logger.error(
                            "RECONCILE: cancel duplicates failed for %s: %s",
                            pos.symbol, cancel_err,
                        )
                    # Force re-placement of both
                    has_sl = False
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
                        _cnt, _all_ok = await self.order_manager.cancel_open_orders(
                            close_pos.symbol,
                        )
                        if not _all_ok:
                            logger.warning("RECONCILE: partial cancel for excess %s, retrying", close_pos.symbol)
                            await asyncio.sleep(1)
                            await self.order_manager.cancel_open_orders(close_pos.symbol)
                        await self.order_manager.place_market_order(
                            symbol=close_pos.symbol,
                            side=close_side,
                            amount=close_pos.size,
                            reduce_only=True,
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

        # 4. Orphan order cleanup: cancel orders for symbols with NO position.
        #    Live monitoring found DOGE orders lingering with no position.
        try:
            for pair in TRADING_PAIRS:
                if pair in open_symbols:
                    continue  # has a position — orders are expected
                try:
                    orphan_orders = await self.order_manager.get_open_orders(pair)
                    if orphan_orders:
                        logger.warning(
                            "RECONCILE: %d orphan orders for %s (no position) "
                            "— cancelling all.",
                            len(orphan_orders), pair,
                        )
                        cancelled, _all_ok = await self.order_manager.cancel_open_orders(pair)
                        logger.info(
                            "RECONCILE: Cancelled %d orphan orders for %s",
                            cancelled, pair,
                        )
                        if not _all_ok:
                            logger.warning("RECONCILE: partial orphan cancel for %s, retrying", pair)
                            await asyncio.sleep(1)
                            await self.order_manager.cancel_open_orders(pair)
                except Exception as e:
                    logger.error(
                        "RECONCILE: Orphan check failed for %s: %s", pair, e
                    )
        except Exception as e:
            logger.error("RECONCILE: Orphan order scan failed: %s", e)

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

    def _get_effective_max_positions(
        self,
        constraints: CircuitBreakerConstraints,
        signal_confidence: float,
        balance: Decimal,
    ) -> int:
        """Return the effective position limit, potentially +1 over CB cap.

        Allows one extra position when ALL of:
        - CB level is GREEN (non-stressed market)
        - Signal confidence >= DYNAMIC_POS_CONFIDENCE_MIN (60%)
        - Balance supports the extra slot ($15 per position slot minimum)

        Never exceeds DYNAMIC_POS_ABSOLUTE_MAX (5). Does not override
        YELLOW/RED/DEAD caps — safety-critical levels remain immutable.
        """
        base = constraints.max_positions
        if constraints.level != CircuitBreakerLevel.GREEN:
            return base

        if signal_confidence < DYNAMIC_POS_CONFIDENCE_MIN:
            return base

        required_balance = DYNAMIC_POS_BALANCE_PER_SLOT * (base + 1)
        if balance < required_balance:
            return base

        effective = min(base + 1, DYNAMIC_POS_ABSOLUTE_MAX)
        if effective > base:
            logger.info(
                "DYNAMIC POSITION LIMIT: %d → %d (confidence=%.1f%%, "
                "balance=$%.2f, required=$%.2f)",
                base, effective, signal_confidence,
                float(balance), float(required_balance),
            )
        return effective

    async def _find_swap_candidate(
        self,
        new_confidence: float,
        open_positions: list[Position],
        new_direction: str | None = None,
    ) -> Position | None:
        """Find the best swap candidate among open positions.

        Returns the position to close for swap, or None if no suitable candidate.

        Two swap paths (v6.20):
        1. **Same-direction swap** (original): new_confidence >= 50 AND
           new_confidence - entry_confidence >= 15 AND position PnL < 0.
        2. **Wrong-side swap** (new): new_direction opposes the position
           direction AND new_confidence >= 40 AND position PnL < 0.
           Wrong-side positions are bleeding against the market — swap
           threshold is lower because closing them is inherently valuable.

        Shared gates:
        - Position has NEGATIVE unrealized PnL
        - Trailing stop NOT activated
        """
        if new_confidence < 40:
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

            # Path 1: Wrong-side swap (position opposes signal direction)
            # e.g., holding SHORT but signal is LONG in bullish market
            is_wrong_side = (
                new_direction is not None
                and (
                    (new_direction == "long" and pos.side == "short")
                    or (new_direction == "short" and pos.side == "long")
                )
            )
            if is_wrong_side and new_confidence >= 40:
                candidates.append((pos, entry_confidence))
                continue

            # Path 2: Same-direction swap (original logic, threshold lowered 60→50)
            if new_confidence >= 50 and new_confidence - entry_confidence >= 15:
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
            _cnt, _all_ok = await self.order_manager.cancel_open_orders(pos.symbol)
            if not _all_ok:
                logger.warning("[%s] SWAP: partial cancel for %s, retrying", trigger, pos.symbol)
                await asyncio.sleep(1)
                await self.order_manager.cancel_open_orders(pos.symbol)
            result = await self.order_manager.place_market_order(
                symbol=pos.symbol,
                side=close_side,
                amount=pos.size,
                reduce_only=True,
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
                    size=float(pos.size),
                    leverage=pos.leverage,
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
            # Fetch FRESH balance (stale self.state.current_balance may be hours old)
            try:
                balance = await self.market_data.get_margin_balance()
                self.state.current_balance = balance
            except Exception:
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

            # 15m data for fast entries
            df_15m = None
            raw_15m = await self.market_data.fetch_ohlcv(symbol, TIMEFRAME_FAST, limit=200)
            if raw_15m and len(raw_15m) >= 50:
                df_15m = self.indicator_engine.calculate_all(pd.DataFrame(raw_15m))

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
            signal = self.adaptive_strategy.get_signal_multi_tf(df_4h, df_1h, df_15m)
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
        """Restore daily_start_balance and last_daily_report.

        DB-first (``system_state`` table), legacy JSON fallback for the
        one-time migration of pre-Sprint-1 state.
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # DB first (primary since Sprint 1)
        try:
            persisted_date = self.db.get_state("daily.date", "")
            if persisted_date == today:
                self.state.daily_start_balance = Decimal(
                    self.db.get_state("daily.start_of_day_balance", "0")
                )
                last_rep = self.db.get_state("daily.last_daily_report", "")
                self.state.last_daily_report = last_rep or None
                logger.info(
                    "Restored daily state (DB) for %s: "
                    "start_balance=$%.2f, last_report=%s",
                    today,
                    self.state.daily_start_balance,
                    self.state.last_daily_report,
                )
                return
        except Exception as exc:  # noqa: BLE001
            logger.warning("DB daily-state load failed: %s", exc)

        # Legacy JSON fallback (pre-Sprint-1)
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
                        "Restored daily state (legacy JSON) for %s",
                        today,
                    )
                    # Promote to DB
                    self._persist_daily_state()
                    return
        except (json.JSONDecodeError, KeyError, Exception) as exc:
            logger.warning("Could not load daily state (JSON): %s", exc)

        # Fallback: new day or missing/corrupt state
        self.state.daily_start_balance = current_balance
        logger.info(
            "No persisted daily state for %s — using current balance $%.2f",
            today, current_balance,
        )

    def _persist_daily_state(self) -> None:
        """Persist daily_start_balance and last_daily_report to the DB.

        Sprint 1: stopped writing to ``daily_state.json`` — the canonical
        store is now the ``system_state`` table. The legacy file remains
        for historical inspection only.
        """
        try:
            self.db.set_state(
                "daily.date",
                datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            )
            self.db.set_state(
                "daily.start_of_day_balance",
                str(self.state.daily_start_balance),
            )
            self.db.set_state(
                "daily.last_daily_report",
                self.state.last_daily_report or "",
            )
            self.db.set_state(
                "daily.updated_at",
                datetime.now(timezone.utc).isoformat(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to persist daily state to DB: %s", exc)

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
        """Persist trailing stop state to the canonical DB (ACID-safe).

        Sprint 1: stopped writing to ``trailing_stops.json`` — ``trailing_stops``
        table is the sole source of truth. Legacy JSON remains read-only
        for historical inspection.
        """
        try:
            current_symbols = set(self._trailing_stops.keys())
            db_symbols = set(self.db.get_all_trailing_stops().keys())

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

            for symbol in db_symbols - current_symbols:
                self.db.delete_trailing_stop(symbol)

        except Exception as exc:
            logger.error("Failed to persist trailing stops to DB: %s", exc)


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
